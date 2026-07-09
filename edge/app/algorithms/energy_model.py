"""空调系统能耗数学模型（寻优目标函数核心）

核心原理
--------
为 PSO 寻优提供目标函数：给定当前工况 + 一组控制参数，计算系统总能耗。
总能耗按设计文档拆分为五个部件之和：

    总能耗 = 冷水机组 + 冷冻泵 + 冷却泵 + 冷却塔风机 + 末端风机

各部件建模思路（工业近似，非黑箱，全部显性可解释）：

1. 冷水机组（主耗能，占比最大）
   采用「卡诺效率修正」COP 模型：
       COP = eta_chiller * T_evap / (T_cond - T_evap)
   - 提高冷水出水温度 → 蒸发温度升高 → COP 升高 → 机组能耗下降；
   - 提高冷却塔风机频率 / 冷却泵流量 → 冷却水温下降 → 冷凝温度下降
     → COP 升高 → 机组能耗下降。
   机组制冷量 = 蒸发侧换热量；冷凝侧排热 = 制冷量 + 压缩机功耗（能量守恒），
   由于冷却水温反过来依赖排热量，采用少量定点迭代求稳态解。

2. 冷冻泵 / 冷却泵 / 冷却塔风机
   遵循风机水泵「相似定律（affinity law）」：功率 ∝ 频率³。
   降频节能显著，但会削弱换热能力（流量/风量下降），需与机组能耗权衡。

3. 末端风机
   近似恒定负荷，取实测值（无实测时用额定值）。

多机组并联
----------
医院大型冷站多为多机组/多水泵并联，本模型以「等效机组」聚合建模，
额定功率参数可经 settings.yaml 的 energy_model 段按现场装机容量配置。

舒适度耦合
----------
控制参数过于激进（冷水温度过高 / 冷冻泵频率过低）会导致供冷能力不足、
室内温度上行。模型据此预测室内温度，交由约束模块施加舒适度惩罚，
使寻优在“节能”与“达标”之间取得安全平衡。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from app.core.config import get_business_config
from app.services.settings_config import get_merged_business_config
from app.schemas.device import DeviceData

# 物理常数
_KELVIN = 273.15
# 水的比热容近似（kJ/(kg·K)），用于换热量估算
_CP_WATER = 4.187


def _finite(value, default: float) -> float:
    """将任意输入安全转为有限浮点：非数值/NaN/Inf 一律回退默认值。

    能耗模型是寻优目标函数，任何 NaN/Inf 输入若不拦截会污染整条链路
    （PSO 目标值、节能率、下发参数），故在入口统一净化。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


@dataclass
class EnergyModelParams:
    """能耗模型可配置参数（现场按装机容量调整）。

    默认值贴合中型医院冷站等效机组量级，与模拟数据规模一致。
    """

    # 冷水机组卡诺效率修正系数（实际 COP / 卡诺 COP），典型 0.4~0.6
    eta_chiller: float = 0.50
    # 蒸发器换热温差（冷媒蒸发温度低于冷水出水温度，℃）
    evap_approach: float = 2.0
    # 冷凝器换热温差（冷媒冷凝温度高于冷却水温，℃）
    cond_approach: float = 3.0

    # 各部件 50Hz / 满负荷额定功率（kW）
    chilled_pump_rated: float = 7.0
    cooling_pump_rated: float = 7.0
    cooling_tower_fan_rated: float = 4.5
    terminal_fan_default: float = 2.0

    # 冷却塔逼近度（冷却水温相对湿球温度）随风量变化范围（℃）
    tower_approach_min: float = 3.0   # 风机满频时最优逼近度
    tower_approach_max: float = 8.0   # 风机低频时最差逼近度

    # 供冷能力标定：等效机组在“冷水7℃、冷冻泵50Hz”下的额定制冷量（kW）
    design_cooling_capacity: float = 120.0
    # 室内温度对供冷缺口的响应增益（kW/℃），越小表示越敏感
    indoor_gain: float = 25.0
    # 供冷充足时的室内基准温度（℃）
    indoor_base_temp: float = 24.5

    # 频率额定基准（Hz），相似定律归一化用
    freq_rated: float = 50.0
    # 当前参与计算的水泵开启台数与配置总台数
    chilled_pump_count: int = 1
    chilled_pump_total_count: int = 1
    cooling_pump_count: int = 1
    cooling_pump_total_count: int = 1
    # 当前参与计算的冷却塔开启台数
    cooling_tower_count: int = 5
    # 冷水出水温度约束区间（与 SafetyConstraints 一致，供供冷能力归一化）
    chw_temp_min: float = 6.0
    chw_temp_max: float = 12.0


@dataclass
class EnergyBreakdown:
    """能耗分解结果（供寻优/展示复用）。"""

    total_power: float
    chiller_power: float
    chilled_pump_power: float
    cooling_pump_power: float
    cooling_tower_fan_power: float
    terminal_fan_power: float
    cop: float
    cooling_water_temp: float
    predicted_indoor_temp: float
    delivered_cooling: float
    extra: dict[str, Any] = field(default_factory=dict)


class ACEnergyModel:
    """中央空调系统能耗数学模型（实现 IEnergyModel）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else get_merged_business_config()
        em = (cfg.get("energy_model", {}) or {}) if isinstance(cfg, dict) else {}
        # 允许从配置覆盖任意字段，未配置项用默认值
        base = EnergyModelParams()
        for f in EnergyModelParams.__dataclass_fields__:
            if f in em:
                try:
                    setattr(base, f, float(em[f]))
                except (TypeError, ValueError):
                    logger.warning(f"能耗模型参数 {f} 配置非法，使用默认值")
        self.p = base

    # ---------- IEnergyModel 协议实现 ----------

    def calculate(self, data: DeviceData, params: dict) -> float:
        """计算给定控制参数下的系统总能耗（kW）。"""
        return self.predict(data, params).total_power

    # ---------- 详细预测（返回分解结果） ----------

    def predict(self, data: DeviceData, params: dict) -> EnergyBreakdown:
        """计算能耗分解 + 室内温度预测。

        Args:
            data: 当前工况（提供负荷、室外温湿度等边界条件）。
            params: 控制参数（冷水温度、三类频率）。
        """
        chilled_pump_count = (
            None
            if params.get("chilled_pump_count") is None
            else int(_finite(params.get("chilled_pump_count"), 1.0))
        )
        cooling_pump_count = (
            None
            if params.get("cooling_pump_count") is None
            else int(_finite(params.get("cooling_pump_count"), 1.0))
        )
        tower_count = int(_finite(params.get("cooling_tower_count"), 5.0))
        p = params.get("_site_params")
        if not isinstance(p, EnergyModelParams):
            p = self._params_for_site(
                chilled_pump_count=chilled_pump_count,
                cooling_pump_count=cooling_pump_count,
                tower_count=tower_count,
            )

        # --- 读取控制变量（全部净化为有限值；缺省回退到实测值，再回退安全默认） ---
        tchw = _finite(params.get("chilled_water_temp"), _finite(data.chilled_water_temp, 7.0))
        f_chp = _finite(params.get("chilled_pump_freq"), _finite(data.chilled_pump_freq, 35.0))
        f_cp = _finite(params.get("cooling_pump_freq"), _finite(data.cooling_pump_freq, 35.0))
        f_fan = _finite(
            params.get("cooling_tower_fan_freq"), _finite(data.cooling_tower_fan_freq, 30.0)
        )

        # --- 环境边界条件净化 ---
        outdoor_temp = _finite(data.outdoor_temp, 30.0)
        outdoor_humidity = _finite(data.outdoor_humidity, 60.0)

        # --- 供冷需求：Excel 有 indoor_load 优先，否则 0 ---
        demand = min(max(_finite(data.indoor_load, 0.0), 0.0), 1.0e7)

        # --- 供冷能力：冷水越冷、冷冻泵流量越大，供冷能力越强 ---
        # 归一化到系统配置的冷水温度区间 [chw_min, chw_max]；最高水温仍保留约 50% 换热能力
        temp_factor = self._cooling_temp_factor(tchw, p)
        flow_chw_ratio = max(
            (p.chilled_pump_count / max(p.chilled_pump_total_count, 1))
            * (f_chp / p.freq_rated),
            0.0,
        )
        delivered = p.design_cooling_capacity * temp_factor * flow_chw_ratio
        # 实际去除的热量不超过需求（多余供冷能力不额外耗能，由控制维持设定值）
        q_evap = min(delivered, demand)

        # --- 室内温度预测：供冷充足沿用实测室温，不足则按缺口上行 ---
        measured_indoor = _finite(data.indoor_temp, p.indoor_base_temp)
        if demand <= 1e-6 or delivered >= demand * 0.98:
            predicted_indoor = measured_indoor
        else:
            unmet = max(demand - delivered, 0.0)
            predicted_indoor = measured_indoor + unmet / max(p.indoor_gain, 1e-6)

        # --- 湿球温度（冷却塔散热下限） ---
        wet_bulb = self._wet_bulb(outdoor_temp, outdoor_humidity)

        # --- 冷却水温 + 机组能耗：Excel 有实测时按模型比例缩放，否则全物理模型 ---
        cooling_water_temp, cop, chiller_power = self._predict_chiller_power(
            data=data,
            params=params,
            q_evap=q_evap,
            tchw=tchw,
            f_cp=f_cp,
            f_fan=f_fan,
            wet_bulb=wet_bulb,
            p=p,
        )

        # --- 辅机能耗：Excel 有实测则按相似定律缩放，否则用配置额定功率 ---
        chilled_pump_power = self._predict_pump_power(
            data, params, "chilled", f_chp, p.chilled_pump_count, p.chilled_pump_rated, p
        )
        cooling_pump_power = self._predict_pump_power(
            data, params, "cooling", f_cp, p.cooling_pump_count, p.cooling_pump_rated, p
        )
        cooling_tower_fan_power = self._predict_tower_power(
            data, params, f_fan, p.cooling_tower_count, p.cooling_tower_fan_rated, p
        )

        # --- 末端风机：取实测（净化后），无实测用额定 ---
        terminal_measured = _finite(data.terminal_fan_power, 0.0)
        terminal = terminal_measured if terminal_measured > 0 else p.terminal_fan_default

        total = (
            chiller_power
            + chilled_pump_power
            + cooling_pump_power
            + cooling_tower_fan_power
            + terminal
        )

        return EnergyBreakdown(
            total_power=round(total, 4),
            chiller_power=round(chiller_power, 4),
            chilled_pump_power=round(chilled_pump_power, 4),
            cooling_pump_power=round(cooling_pump_power, 4),
            cooling_tower_fan_power=round(cooling_tower_fan_power, 4),
            terminal_fan_power=round(terminal, 4),
            cop=round(cop, 4),
            cooling_water_temp=round(cooling_water_temp, 3),
            predicted_indoor_temp=round(predicted_indoor, 3),
            delivered_cooling=round(delivered, 3),
        )

    # ---------- 内部物理子模型 ----------

    @staticmethod
    def _cooling_temp_factor(tchw: float, p: EnergyModelParams) -> float:
        """冷水出水温度对供冷能力的相对系数（0.5~1.0）。

        现场约束上限（如 15℃）仍具备供冷能力，避免旧版 [6,12] 硬编码导致
        高温设定下模型误判“零供冷”。
        """
        chw_lo = min(p.chw_temp_min, p.chw_temp_max)
        chw_hi = max(p.chw_temp_min, p.chw_temp_max)
        span = max(chw_hi - chw_lo, 1e-6)
        ratio = max((chw_hi - tchw) / span, 0.0)
        return 0.5 + 0.5 * min(ratio, 1.0)

    def _params_for_site(
        self,
        chilled_pump_count: int | None = None,
        cooling_pump_count: int | None = None,
        tower_count: int | None = None,
    ) -> EnergyModelParams:
        """按本地设备配置生成本次计算使用的模型参数。"""
        p = self.p
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            units = equipment_config_service.get_units()
            if chilled_pump_count is None:
                chilled_pump_count = eq.chilled_pump.count
            if cooling_pump_count is None:
                cooling_pump_count = eq.cooling_pump.count
            chilled_pump_count = max(
                0, min(int(chilled_pump_count), eq.chilled_pump.count)
            )
            cooling_pump_count = max(
                0, min(int(cooling_pump_count), eq.cooling_pump.count)
            )
            enabled_towers = [tower for tower in eq.cooling_towers if tower.enabled]
            if tower_count is None:
                tower_count = len(enabled_towers)
            tower_count = max(0, min(int(tower_count), len(enabled_towers)))
            selected_towers = enabled_towers[:tower_count]
            tower_power = sum(tower.motor_power_kw for tower in selected_towers)

            chilled_units = [
                unit
                for unit in units
                if unit.unit_type == "chilled_pump" and unit.enabled
            ]
            cooling_units = [
                unit
                for unit in units
                if unit.unit_type == "cooling_pump" and unit.enabled
            ]
            chilled_rated = sum(
                float(unit.motor_power_kw or 0.0)
                for unit in chilled_units[:chilled_pump_count]
            )
            cooling_rated = sum(
                float(unit.motor_power_kw or 0.0)
                for unit in cooling_units[:cooling_pump_count]
            )
            if chilled_rated <= 0:
                chilled_rated = chilled_pump_count * eq.chilled_pump.motor_power_kw
            if cooling_rated <= 0:
                cooling_rated = cooling_pump_count * eq.cooling_pump.motor_power_kw

            chillers = [
                unit for unit in units if unit.unit_type == "chiller" and unit.enabled
            ]
            design_capacity = sum(
                float(unit.rated_capacity_kw or 0.0)
                * float(unit.max_load_rate or 0.8)
                for unit in chillers
            )
            if design_capacity <= 0:
                design_capacity = (
                    eq.chiller.count
                    * eq.chiller.rated_capacity_kw
                    * eq.chiller.max_load_rate
                )

            p = replace(
                p,
                chilled_pump_rated=chilled_rated,
                cooling_pump_rated=cooling_rated,
                chilled_pump_count=chilled_pump_count,
                chilled_pump_total_count=max(eq.chilled_pump.count, 1),
                cooling_pump_count=cooling_pump_count,
                cooling_pump_total_count=max(eq.cooling_pump.count, 1),
                cooling_tower_fan_rated=tower_power,
                cooling_tower_count=tower_count,
                design_cooling_capacity=design_capacity,
            )
            try:
                from app.algorithms.constraints import SafetyConstraints

                chw_lo, chw_hi = SafetyConstraints().bounds["chilled_water_temp"]
                p = replace(p, chw_temp_min=chw_lo, chw_temp_max=chw_hi)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"读取设备配置失败，能耗模型使用默认参数: {e}")
        return p

    def _predict_pump_power(
        self,
        data: DeviceData,
        params: dict,
        kind: str,
        new_freq: float,
        new_count: int,
        rated_total: float,
        p: EnergyModelParams,
    ) -> float:
        """冷冻/冷却泵功率：Excel 实测优先，按频率³与台数缩放。"""
        from app.services.excel_first_power import scale_measured_component
        from app.services.power_baseline import infer_active_counts

        power_attr = f"{kind}_pump_power"
        freq_attr = f"{kind}_pump_freq"
        count_key = f"{kind}_pump_count"
        measured = _finite(getattr(data, power_attr), 0.0)
        base_freq = _finite(getattr(data, freq_attr), 0.0)
        active_counts = params.get("_active_counts")
        if not isinstance(active_counts, dict):
            active_counts = infer_active_counts(data.model_dump())
        base_count = active_counts.get(count_key, new_count)
        scaled = scale_measured_component(
            measured, base_freq, new_freq, int(base_count), int(new_count)
        )
        if scaled is not None:
            return scaled
        return self._affinity(rated_total, new_freq, p)

    def _predict_tower_power(
        self,
        data: DeviceData,
        params: dict,
        new_freq: float,
        new_count: int,
        rated_total: float,
        p: EnergyModelParams,
    ) -> float:
        """冷却塔功率：Excel 实测优先。"""
        from app.services.excel_first_power import scale_measured_component
        from app.services.power_baseline import infer_active_counts

        measured = _finite(data.cooling_tower_fan_power, 0.0)
        base_freq = _finite(data.cooling_tower_fan_freq, 50.0)
        active_counts = params.get("_active_counts")
        if not isinstance(active_counts, dict):
            active_counts = infer_active_counts(data.model_dump())
        base_count = active_counts.get("cooling_tower_count", new_count)
        scaled = scale_measured_component(
            measured, base_freq, new_freq, int(base_count), int(new_count)
        )
        if scaled is not None:
            return scaled
        return self._affinity(rated_total, new_freq, p)

    def _predict_chiller_power(
        self,
        data: DeviceData,
        params: dict,
        q_evap: float,
        tchw: float,
        f_cp: float,
        f_fan: float,
        wet_bulb: float,
        p: EnergyModelParams,
    ) -> tuple[float, float, float]:
        """机组功率：Excel 有实测时锚定实测，按物理模型比例缩放推荐参数。"""
        from app.services.power_baseline import current_operating_params

        cooling_water_temp, cop, model_new = self._solve_condenser(
            q_evap=q_evap,
            tchw=tchw,
            f_cp=f_cp,
            f_fan=f_fan,
            wet_bulb=wet_bulb,
            p=p,
        )
        measured = _finite(data.chiller_power, 0.0)
        if measured <= 0:
            return cooling_water_temp, cop, model_new

        baseline = params.get("_baseline_params")
        if not isinstance(baseline, dict):
            baseline = current_operating_params(data.model_dump())
        base_p = params.get("_baseline_site_params")
        if not isinstance(base_p, EnergyModelParams):
            base_p = self._params_for_site(
                chilled_pump_count=int(baseline.get("chilled_pump_count", p.chilled_pump_count)),
                cooling_pump_count=int(baseline.get("cooling_pump_count", p.cooling_pump_count)),
                tower_count=int(baseline.get("cooling_tower_count", p.cooling_tower_count)),
            )
        _, _, model_base = self._solve_condenser(
            q_evap=q_evap,
            tchw=_finite(baseline.get("chilled_water_temp"), tchw),
            f_cp=_finite(baseline.get("cooling_pump_freq"), f_cp),
            f_fan=_finite(baseline.get("cooling_tower_fan_freq"), f_fan),
            wet_bulb=wet_bulb,
            p=base_p,
        )
        if model_base > 0:
            return cooling_water_temp, cop, measured * (model_new / model_base)
        return cooling_water_temp, cop, model_new

    def _affinity(self, rated_power: float, freq: float, p: EnergyModelParams | None = None) -> float:
        """风机水泵相似定律：P = P_rated * (f / f_rated)³。"""
        p = p or self.p
        ratio = max(freq, 0.0) / p.freq_rated
        return rated_power * (ratio ** 3)

    def _tower_approach(self, f_fan: float, p: EnergyModelParams | None = None) -> float:
        """冷却塔逼近度：风量越大逼近度越小（冷却水温越接近湿球）。"""
        p = p or self.p
        if p.cooling_tower_count <= 0:
            # 无冷却塔运行时，冷凝侧散热能力极差；不是直接报错，而是让目标函数
            # 通过更高冷凝温度/更低 COP 自然抛弃该方案。
            return p.tower_approach_max + 20.0
        # 定频 50Hz 时视为满风量；兼容旧配置的 [20,45] 变频塔。
        span = 50.0 - 20.0
        ratio = min(max((f_fan - 20.0) / span, 0.0), 1.0)
        base = p.tower_approach_max - (p.tower_approach_max - p.tower_approach_min) * ratio
        # 3 台方案相对 5 台换热面积更小，逼近度略差；5 台为最优逼近度。
        count_penalty = max(0, 5 - p.cooling_tower_count) * 1.2
        return base + count_penalty

    def _solve_condenser(
        self,
        q_evap: float,
        tchw: float,
        f_cp: float,
        f_fan: float,
        wet_bulb: float,
        p: EnergyModelParams | None = None,
    ) -> tuple[float, float, float]:
        """定点迭代求解冷却水温、COP、机组功率。

        冷凝侧排热 Q_reject = Q_evap + W_compressor，而 W_compressor 又依赖
        由排热决定的冷凝温度，故用少量迭代收敛（3~5 次足够稳定）。
        返回 (冷却水温, COP, 机组功率)。
        """
        p = p or self.p
        approach = self._tower_approach(f_fan, p)

        # 冷却泵流量不足会抬高冷凝侧温升（range），以频率比反映
        flow_cp_ratio = max(
            (p.cooling_pump_count / max(p.cooling_pump_total_count, 1))
            * (f_cp / p.freq_rated),
            0.2,
        )  # 下限保护，防止除零/发散

        t_evap_k = (tchw - p.evap_approach) + _KELVIN

        chiller_power = q_evap / 4.0  # 初值：假设 COP≈4
        cooling_water_temp = wet_bulb + approach
        cop = 4.0

        for _ in range(6):
            q_reject = q_evap + chiller_power
            # 冷却水温 = 湿球 + 塔逼近度 + 冷凝器温升（排热/流量，流量越小温升越大）
            capacity = max(p.design_cooling_capacity, 1e-6)
            cond_range = q_reject / (capacity * flow_cp_ratio) * 5.0
            cooling_water_temp = wet_bulb + approach + cond_range
            t_cond_k = (cooling_water_temp + p.cond_approach) + _KELVIN

            denom = max(t_cond_k - t_evap_k, 1.0)  # 防止温差过小导致 COP 爆炸
            cop = p.eta_chiller * t_evap_k / denom
            cop = max(cop, 1.5)  # 工程下限，避免非物理极端值

            new_power = q_evap / cop
            # 收敛判据
            if abs(new_power - chiller_power) < 1e-4:
                chiller_power = new_power
                break
            chiller_power = new_power

        return cooling_water_temp, cop, chiller_power

    @staticmethod
    def _wet_bulb(temp_c: float, rh: float) -> float:
        """湿球温度估算（Stull 2011 经验公式，常温常压适用）。

        Args:
            temp_c: 干球温度（℃）
            rh: 相对湿度（%）
        """
        rh = min(max(rh, 5.0), 99.0)
        t = temp_c
        tw = (
            t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
            + math.atan(t + rh)
            - math.atan(rh - 1.676331)
            + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
            - 4.686035
        )
        # 物理约束：湿球不高于干球
        return min(tw, temp_c)
