"""空调系统能耗数学模型（寻优目标函数核心）

核心原理
--------
为 PSO 寻优提供目标函数：给定当前工况 + 一组控制参数，计算系统总能耗。
总能耗按设计文档拆分为五个部件之和：

    总能耗 = 冷水机组 + 冷冻泵 + 冷却泵 + 冷却塔风机 + 末端风机

各部件建模思路（工业近似，非黑箱，全部显性可解释）：

1. 冷水机组（主耗能，占比最大）
   对齐 EnergyPlus / Modelica Buildings ``Chiller:Electric:EIR``（DOE-2）结构，
   温区敏感性仍由卡诺修正表达（无厂商 biquadratic 曲线时的可解释替代）：

       COP_carnot ≈ eta_chiller * T_evap / (T_cond - T_evap)   # 隐式 EIRFunT
       Q_ava = Q_nominal * capFunT(T_chw, T_cw_enter) * φ_chw   # φ_chw=冷冻水流量比
       PLR1 = min(Q_evap / Q_ava, 1)
       CR   = min(PLR1 / PLRMin, 1)
       PLR2 = max(PLR1, PLRMinUnl)           # 热气旁通卸载地板
       EIRFPLR = 曲线(PLR2) 或 PLR2（关闭曲线时线性恒 COP）
       P    = (Q_ava / COP_carnot) * EIRFPLR * CR

   满负荷时与旧式 ``(Q_evap/COP)*(EIRFPLR/PLR)`` 同构；低负荷不再虚假坍塌功率。
   冷凝侧排热 = 制冷量 + 压缩机功耗，定点迭代求冷却水温。
   有铭牌额定冷量/功率时，将 eta_chiller 校准到设计工况附近。

2. 冷冻泵 / 冷却泵
   按水泵「相似定律」由频率反推功率：P = P_rated × (f / f_rated)³
   （与 Buildings ``Fluid.Movers`` 亲和律一致）。

3. 冷却塔风机
   现场为定频运行：功率取开启台数对应电机额定 kW 之和（或方案定额）。
   逼近度随风量变化，并按 Scheier 思想随冷却水流量比抬升
   （不移植完整 Merkel UA 迭代）。

4. 末端风机
   近似恒定负荷，取实测值（无实测时用额定值）。

对照来源
--------
- Modelica Buildings ``Buildings.Fluid.Chillers.BaseClasses.PartialElectric``
  / EnergyPlus Engineering Reference §14.3.9.2（式 14.234–14.240）
- Cooling tower approach water-flow correction inspired by Scheier UA factors
  in ``Buildings.Fluid.HeatExchangers.CoolingTowers.Merkel``（仅思想，非全模型）

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
import threading
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
# 并行 PSO 下 SQLite 读设备配置可能瞬时失败；缓存最近一次成功结果避免回退到默认 120kW
_site_config_lock = threading.Lock()
_site_config_cache: tuple[Any, list[Any]] | None = None


def _load_site_equipment() -> tuple[Any, list[Any]]:
    """线程安全读取设备配置；失败时回退到最近一次成功缓存。"""
    global _site_config_cache
    from app.services.equipment_config import equipment_config_service

    with _site_config_lock:
        try:
            eq = equipment_config_service.get_config()
            units = equipment_config_service.get_units()
            _site_config_cache = (eq, list(units))
            return eq, list(units)
        except Exception as e:
            if _site_config_cache is not None:
                logger.debug(f"读取设备配置失败，使用缓存: {e}")
                return _site_config_cache
            raise


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
    # 有铭牌额定冷量/电功率时会按设计工况自动校准（仍钳在合理区间）。
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
    # 铭牌额定电功率合计（kW）；>0 时用于校准 eta_chiller
    design_chiller_power: float = 0.0
    # 室内温度对供冷缺口的响应增益（kW/℃），越小表示越敏感
    indoor_gain: float = 25.0
    # 供冷充足时的室内基准温度（℃）
    indoor_base_temp: float = 24.5
    # 室外偏热且供冷偏紧时，室温上浮耦合系数（℃/℃）
    outdoor_indoor_coupling: float = 0.06
    outdoor_stress_ref: float = 29.0
    # 室外高于参考温度时，冷负荷相对增幅（1/℃），抑制“越热越省电”
    outdoor_load_coupling: float = 0.02
    # 已运行主机的最小输入功率占实测总主机功率比例。
    # 防止闭环将模型预测反复当实测锚点后，出现“两台主机合计几十 kW”的非物理结果。
    min_running_chiller_power_ratio: float = 0.65
    # 相对实测功率的最大允许涨幅（允许室外/负荷驱动的真实升功率）
    max_component_power_rise_pct: float = 0.30
    # 部分负荷 EIR 曲线：P/P_full = a + b·PLR + c·PLR² + d·PLR³
    # 默认近似离心机：低负荷时比功率升高（EIRFPLR/PLR > 1）
    plr_eir_a: float = 0.338
    plr_eir_b: float = 0.284
    plr_eir_c: float = 0.378
    plr_eir_d: float = 0.0
    # 是否启用部分负荷效率修正
    enable_part_load_curve: bool = True
    # ElectricEIR：最低运行部分负荷（低于此用循环比 CR 折算平均功率）
    plr_min: float = 0.15
    # ElectricEIR：热气旁通卸载下限（压缩机功率按该 PLR 计 EIR）
    plr_min_unl: float = 0.30
    # 是否启用简化 capFunT（可用容量随冷水出水/冷却水进水修正）
    enable_cap_fun_t: bool = True
    # capFunT：相对设计点每升高 1℃ 冷水出水的容量系数（通常为正，暖出水容量略增）
    cap_fun_t_chw: float = 0.01
    # capFunT：相对设计点每升高 1℃ 冷却水进水的容量跌幅系数
    cap_fun_t_cw: float = 0.02
    cap_fun_t_min: float = 0.70
    cap_fun_t_max: float = 1.15
    # 冷却塔逼近度：水量不足时的抬升系数（℃）；Scheier cWatFra 思想
    tower_approach_water_k: float = 2.0
    # 铭牌校准用的设计工况（冷水出水 / 冷却水进水，℃）
    design_chw_temp: float = 7.0
    design_cw_temp: float = 30.0

    # 频率额定基准（Hz），相似定律归一化用
    freq_rated: float = 50.0
    # 当前参与计算的水泵开启台数与配置总台数
    chilled_pump_count: int = 1
    chilled_pump_total_count: int = 1
    cooling_pump_count: int = 1
    cooling_pump_total_count: int = 1
    # 当前参与计算的冷却塔开启台数 / 已启用（或方案上限）总台数
    cooling_tower_count: int = 5
    cooling_tower_total_count: int = 5
    # 冷水出水温度约束区间（与 SafetyConstraints 查表配置一致，供供冷能力归一化）
    chw_temp_min: float = 8.0
    chw_temp_max: float = 14.0
    # 室内舒适温度区间（与策略配置一致，供室温预测）
    comfort_temp_min: float = 24.0
    comfort_temp_max: float = 26.0


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
    # ElectricEIR PLR1 = Q_evap / Q_ava（供 PLR 甜点等软惩罚）
    plr1: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class ACEnergyModel:
    """中央空调系统能耗数学模型（实现 IEnergyModel）。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else get_merged_business_config()
        em = (cfg.get("energy_model", {}) or {}) if isinstance(cfg, dict) else {}
        # 允许从配置覆盖任意字段，未配置项用默认值
        base = EnergyModelParams()
        bool_fields = {"enable_part_load_curve", "enable_cap_fun_t"}
        for f in EnergyModelParams.__dataclass_fields__:
            if f not in em:
                continue
            try:
                if f in bool_fields:
                    setattr(base, f, bool(em[f]))
                else:
                    setattr(base, f, float(em[f]))
            except (TypeError, ValueError):
                logger.warning(f"能耗模型参数 {f} 配置非法，使用默认值")
        strategy = (cfg.get("strategy", {}) or {}) if isinstance(cfg, dict) else {}
        indoor = (strategy.get("indoor_temp", {}) or {}) if isinstance(strategy, dict) else {}
        constraints = (cfg.get("constraints", {}) or {}) if isinstance(cfg, dict) else {}
        indoor_cfg = indoor or (constraints.get("indoor_temp", {}) or {})
        try:
            lo = float(indoor_cfg.get("min", base.comfort_temp_min))
            hi = float(indoor_cfg.get("max", base.comfort_temp_max))
            if lo > hi:
                lo, hi = hi, lo
            base = replace(base, comfort_temp_min=lo, comfort_temp_max=hi)
        except (TypeError, ValueError):
            pass
        for field_name, key in (
            ("outdoor_indoor_coupling", "outdoor_indoor_coupling"),
            ("outdoor_stress_ref", "outdoor_stress_ref"),
            ("outdoor_load_coupling", "outdoor_load_coupling"),
            ("min_running_chiller_power_ratio", "min_running_chiller_power_ratio"),
            ("max_component_power_rise_pct", "max_component_power_rise_pct"),
            ("plr_eir_a", "plr_eir_a"),
            ("plr_eir_b", "plr_eir_b"),
            ("plr_eir_c", "plr_eir_c"),
            ("plr_eir_d", "plr_eir_d"),
            ("plr_min", "plr_min"),
            ("plr_min_unl", "plr_min_unl"),
            ("cap_fun_t_chw", "cap_fun_t_chw"),
            ("cap_fun_t_cw", "cap_fun_t_cw"),
            ("cap_fun_t_min", "cap_fun_t_min"),
            ("cap_fun_t_max", "cap_fun_t_max"),
            ("tower_approach_water_k", "tower_approach_water_k"),
            ("design_chw_temp", "design_chw_temp"),
            ("design_cw_temp", "design_cw_temp"),
        ):
            if key in em:
                try:
                    base = replace(base, **{field_name: float(em[key])})
                except (TypeError, ValueError):
                    pass
        if "enable_part_load_curve" in em:
            base = replace(base, enable_part_load_curve=bool(em["enable_part_load_curve"]))
        if "enable_cap_fun_t" in em:
            base = replace(base, enable_cap_fun_t=bool(em["enable_cap_fun_t"]))
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
        # 寻优输入可覆盖水泵额定功率/额定频率：P = P_rated × (f/f_rated)³
        p = self._apply_pump_rated_overrides(
            p,
            data,
            chilled_pump_count=chilled_pump_count,
            cooling_pump_count=cooling_pump_count,
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
        # 室外高于参考温度时略增冷负荷，避免“越热总功率反而下降”的失真
        demand = min(max(_finite(data.indoor_load, 0.0), 0.0), 1.0e7)
        outdoor_stress = max(0.0, outdoor_temp - p.outdoor_stress_ref)
        demand *= 1.0 + outdoor_stress * p.outdoor_load_coupling
        demand = min(demand, 1.0e7)

        # --- 供冷能力：末端换热（temp_factor）与主机可用容量（capFunT）取紧约束 ---
        # temp_factor：冷水越低 → 末端供冷越强；capFunT：冷水越低 → 主机可用冷量略降。
        # 二者方向相反，取 min 避免双轨打架；冷冻水流量比两边共用。
        temp_factor = self._cooling_temp_factor(tchw, p)
        flow_chw_ratio = max(
            (p.chilled_pump_count / max(p.chilled_pump_total_count, 1))
            * (f_chp / p.freq_rated),
            0.0,
        )
        load_pct = _finite(params.get("chiller_load_pct"), 0.0)
        if load_pct <= 0:
            load_pct = _finite(data.chiller_load, 0.0)
        if load_pct <= 0:
            load_pct = 80.0
        load_pct = min(max(load_pct, 0.0), 100.0)
        try:
            from app.algorithms.constraints import SafetyConstraints

            load_pct = min(load_pct, SafetyConstraints.max_chiller_load_pct())
        except Exception:
            pass
        load_factor = load_pct / 100.0

        # 湿球与塔逼近度先估冷凝器进水，供 capFunT（与 _solve_condenser 初值一致）
        wet_bulb = self._wet_bulb(outdoor_temp, outdoor_humidity)
        flow_cp_ratio = max(
            (p.cooling_pump_count / max(p.cooling_pump_total_count, 1))
            * (f_cp / p.freq_rated),
            0.05,
        )
        approach0 = self._tower_approach(f_fan, p, flow_cp_ratio=flow_cp_ratio)
        t_cw_enter0 = wet_bulb + approach0
        cap_ft = self._cap_fun_t(tchw, t_cw_enter0, p)

        coil_capacity = p.design_cooling_capacity * temp_factor * flow_chw_ratio
        machine_capacity = p.design_cooling_capacity * cap_ft * max(flow_chw_ratio, 0.0)
        delivered = min(coil_capacity, machine_capacity) * load_factor
        q_evap = min(delivered, demand)

        try:
            from app.algorithms.indoor_temp import control_indoor_temp

            measured_indoor = _finite(
                control_indoor_temp(data), p.indoor_base_temp
            )
        except Exception:
            measured_indoor = _finite(data.indoor_temp, p.indoor_base_temp)
        predicted_indoor = self._predict_indoor_temp(
            measured_indoor=measured_indoor,
            demand=demand,
            delivered_capacity=delivered,
            temp_factor=temp_factor,
            flow_chw_ratio=flow_chw_ratio,
            tchw=tchw,
            outdoor_temp=outdoor_temp,
            p=p,
        )

        # --- 冷却水温 + 机组能耗：Excel 有实测时按模型比例缩放，否则全物理模型 ---
        cooling_water_temp, cop, chiller_power, plr1 = self._predict_chiller_power(
            data=data,
            params=params,
            q_evap=q_evap,
            demand=demand,
            tchw=tchw,
            f_chp=f_chp,
            f_cp=f_cp,
            f_fan=f_fan,
            wet_bulb=wet_bulb,
            flow_chw_ratio=flow_chw_ratio,
            p=p,
        )

        # --- 辅机能耗：冷冻/冷却泵按额定×(f/f_rated)³；冷却塔按方案定额 ---
        chilled_pump_power = self._predict_pump_power(
            data,
            params,
            "chilled",
            f_chp,
            int(chilled_pump_count or p.chilled_pump_count),
            p.chilled_pump_rated,
            p,
        )
        cooling_pump_power = self._predict_pump_power(
            data,
            params,
            "cooling",
            f_cp,
            int(cooling_pump_count or p.cooling_pump_count),
            p.cooling_pump_rated,
            p,
        )
        cooling_tower_fan_power = self._predict_tower_power(
            data, params, f_fan, tower_count, p.cooling_tower_fan_rated, p
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
            plr1=round(plr1, 4),
        )

    # ---------- 内部物理子模型 ----------

    @staticmethod
    def _predict_indoor_temp(
        measured_indoor: float,
        demand: float,
        delivered_capacity: float,
        temp_factor: float,
        flow_chw_ratio: float,
        tchw: float,
        outdoor_temp: float,
        p: EnergyModelParams,
    ) -> float:
        """根据供冷能力与控制参数预测室内温度（℃）。

        舒适区内也会随冷水温度/流量变化；室外偏热且供冷偏紧时上浮室温。
        """
        lo = p.comfort_temp_min
        hi = p.comfort_temp_max
        mid = (lo + hi) / 2.0
        chw_lo = min(p.chw_temp_min, p.chw_temp_max)
        chw_hi = max(p.chw_temp_min, p.chw_temp_max)
        chw_span = max(chw_hi - chw_lo, 1e-6)
        chw_norm = min(max((tchw - chw_lo) / chw_span, 0.0), 1.0)
        control_effect = min(max(temp_factor * flow_chw_ratio, 0.0), 1.0)
        if demand <= 1e-6:
            return measured_indoor

        capacity_ratio = delivered_capacity / max(demand, 1e-6)
        indoor_span = max(hi - lo, 1e-6)
        # 与 settings.yaml comfort_margin.base_from_ceiling=0.5 对齐
        # （完整室外/邻近修正在 SafetyConstraints.effective_comfort_ceiling）
        safety_ceiling = hi - 0.5
        safety_target = max(lo, safety_ceiling - 0.15)
        at_hi = measured_indoor >= hi - 0.05
        above_safety = measured_indoor >= safety_ceiling - 1e-9
        near_hi = measured_indoor >= hi - 0.4

        # 贴近硬上限或已越过安全天花板：有供冷能力时主动拉回安全目标区
        if (at_hi or above_safety) and demand > 1e-6:
            cool_strength = min(
                max(control_effect, 0.0) * min(max(capacity_ratio, 0.0), 1.25),
                1.25,
            )
            pull = min(0.95, 0.55 + 0.40 * cool_strength)
            predicted = measured_indoor - (measured_indoor - safety_target) * pull
            predicted -= max(0.0, 0.5 - chw_norm) * 0.20 * min(cool_strength, 1.0)
            predicted = min(safety_ceiling, max(lo, predicted))
        elif capacity_ratio + 1e-6 < 0.98:
            unmet = max(demand - delivered_capacity, 0.0)
            if lo <= measured_indoor <= hi:
                drift = min(unmet / max(p.indoor_gain, 1e-6), 0.35)
                # 已在安全区内：禁止向 26℃ 爬升；轻微欠供冷也粘住或略漂
                if measured_indoor <= safety_ceiling:
                    if capacity_ratio >= 0.70:
                        predicted = measured_indoor
                    else:
                        soft = min(drift, 0.05) * max(0.0, 0.85 - capacity_ratio)
                        predicted = min(safety_ceiling, measured_indoor + soft)
                    predicted = min(safety_ceiling, max(lo, predicted))
                elif near_hi:
                    relief = min(max(control_effect, 0.0), 1.0) * min(
                        capacity_ratio / 0.98, 1.0
                    )
                    predicted = measured_indoor + drift * (1.0 - 0.90 * relief)
                    if capacity_ratio >= 0.80:
                        predicted -= (measured_indoor - safety_target) * 0.45 * relief
                    predicted = min(safety_ceiling, max(lo, predicted))
                else:
                    predicted = min(safety_ceiling, measured_indoor + drift * 0.2)
                    predicted = min(hi, max(lo, predicted))
            else:
                predicted = measured_indoor + unmet / max(p.indoor_gain, 1e-6)
        else:
            surplus = min(max(capacity_ratio - 1.0, 0.0), 1.0)
            chw_effect = (chw_norm - 0.5) * indoor_span * 0.5
            surplus_effect = -surplus * indoor_span * 0.4 * max(control_effect, 0.1)
            equilibrium = mid + chw_effect + surplus_effect
            # 平衡点也钳在安全天花板以下，避免“舒适带内”预测贴 26
            equilibrium = min(safety_ceiling, max(lo, equilibrium))

            if lo <= measured_indoor <= hi:
                if measured_indoor > safety_target:
                    blend = 0.45 if near_hi else 0.30
                    predicted = (1.0 - blend) * measured_indoor + blend * min(
                        equilibrium, safety_target
                    )
                else:
                    blend = 0.12
                    predicted = (1.0 - blend) * measured_indoor + blend * equilibrium
                predicted = min(safety_ceiling, max(lo - 0.5, predicted))
            else:
                surplus_ratio = min(
                    max((capacity_ratio - 1.0) / max(capacity_ratio, 1.0), 0.0), 1.0
                )
                relief = control_effect * surplus_ratio

                if measured_indoor > hi:
                    predicted = measured_indoor - (measured_indoor - safety_target) * relief
                    predicted = max(lo, predicted)
                elif measured_indoor < lo:
                    predicted = measured_indoor + (mid - measured_indoor) * relief
                    predicted = min(hi, max(lo, predicted))
                else:
                    predicted = measured_indoor

        outdoor_stress = max(0.0, outdoor_temp - p.outdoor_stress_ref)
        if (
            outdoor_stress > 0
            and capacity_ratio < 1.05
            and not at_hi
            and not above_safety
            and not (lo <= measured_indoor <= hi)
        ):
            tightness = min(max(1.05 - capacity_ratio, 0.0) / 0.05, 1.0)
            predicted += (
                outdoor_stress
                * p.outdoor_indoor_coupling
                * tightness
                * (1.0 - 0.5 * control_effect)
            )

        # 有供冷时预测室温不得贴硬上限；强制落在安全距离内
        if capacity_ratio >= 0.75 or control_effect >= 0.35:
            predicted = min(safety_ceiling, max(lo, predicted))
        elif lo <= measured_indoor <= hi:
            predicted = min(hi - 0.15, max(lo - 1.0, predicted))
        else:
            predicted = min(predicted, hi + 1.0)

        return predicted

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

    def _apply_pump_rated_overrides(
        self,
        p: EnergyModelParams,
        data: DeviceData,
        *,
        chilled_pump_count: int | None,
        cooling_pump_count: int | None,
    ) -> EnergyModelParams:
        """用寻优输入中的额定功率/频率覆盖设备配置额定值。"""
        updates: dict = {}
        rated_freq = _finite(getattr(data, "pump_rated_freq", 0.0), 0.0)
        if rated_freq > 0:
            updates["freq_rated"] = rated_freq
        chp_n = max(
            int(chilled_pump_count if chilled_pump_count is not None else p.chilled_pump_count),
            0,
        )
        cwp_n = max(
            int(cooling_pump_count if cooling_pump_count is not None else p.cooling_pump_count),
            0,
        )
        chp_unit = _finite(getattr(data, "chilled_pump_rated_power_kw", 0.0), 0.0)
        cwp_unit = _finite(getattr(data, "cooling_pump_rated_power_kw", 0.0), 0.0)
        if chp_unit > 0 and chp_n > 0:
            updates["chilled_pump_rated"] = chp_unit * chp_n
        if cwp_unit > 0 and cwp_n > 0:
            updates["cooling_pump_rated"] = cwp_unit * cwp_n
        return replace(p, **updates) if updates else p

    def _params_for_site(
        self,
        chilled_pump_count: int | None = None,
        cooling_pump_count: int | None = None,
        tower_count: int | None = None,
    ) -> EnergyModelParams:
        """按本地设备配置生成本次计算使用的模型参数。"""
        p = self.p
        try:
            eq, units = _load_site_equipment()
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
            schemes = sorted(
                {
                    max(0, min(int(s), len(enabled_towers)))
                    for s in (eq.cooling_tower_schemes or [len(enabled_towers)])
                }
            )
            max_scheme = max(schemes) if schemes else len(enabled_towers)
            if tower_count is None:
                tower_count = max_scheme
            tower_count = max(0, min(int(tower_count), max_scheme))
            selected_towers = enabled_towers[: min(len(enabled_towers), tower_count)]
            tower_total = max(len(enabled_towers), max_scheme, 1)
            # 定频塔：额定功率 = 开启台数铭牌电机功率之和（与频率无关）
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
                for unit in chillers
            )
            if design_capacity <= 0:
                design_capacity = (
                    eq.chiller.count
                    * eq.chiller.rated_capacity_kw
                )
            design_power = sum(
                float(unit.rated_power_kw or 0.0) for unit in chillers
            )
            if design_power <= 0:
                design_power = float(getattr(eq.chiller, "rated_power_kw", 0.0) or 0.0) * max(
                    int(getattr(eq.chiller, "count", 1) or 1), 1
                )
            # 铭牌校准与 Q_nom 均用全额额定冷量；max_load_rate 仅作负载% 硬上限
            # （见 SafetyConstraints.max_chiller_load_pct），避免与 BMS 负载% 双重折减。
            nameplate_capacity = design_capacity
            if nameplate_capacity <= 0:
                nameplate_capacity = float(eq.chiller.rated_capacity_kw or 0.0) * max(
                    int(eq.chiller.count or 1), 1
                )

            eta = p.eta_chiller
            if nameplate_capacity > 0 and design_power > 0:
                eta = self._calibrate_eta_from_nameplate(
                    design_capacity=nameplate_capacity,
                    design_power=design_power,
                    p=p,
                )

            p = replace(
                p,
                eta_chiller=eta,
                chilled_pump_rated=chilled_rated,
                cooling_pump_rated=cooling_rated,
                chilled_pump_count=chilled_pump_count,
                chilled_pump_total_count=max(eq.chilled_pump.count, 1),
                cooling_pump_count=cooling_pump_count,
                cooling_pump_total_count=max(eq.cooling_pump.count, 1),
                cooling_tower_fan_rated=tower_power,
                cooling_tower_count=tower_count,
                cooling_tower_total_count=tower_total,
                design_cooling_capacity=design_capacity,
                design_chiller_power=design_power,
            )
            try:
                from app.algorithms.constraints import SafetyConstraints

                constraints = SafetyConstraints()
                # 冷水出水温度区间取查表配置的最小/最大值，供供冷能力归一化使用
                chw_lo, chw_hi = constraints.chilled_water_temp_range()
                comfort_lo, comfort_hi = constraints.indoor_temp_range
                p = replace(
                    p,
                    chw_temp_min=chw_lo,
                    chw_temp_max=chw_hi,
                    comfort_temp_min=comfort_lo,
                    comfort_temp_max=comfort_hi,
                )
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"读取设备配置失败，能耗模型使用默认参数: {e}")
            # 若已有成功缓存，再试一次（并行瞬时失败时常见）
            try:
                eq, units = _load_site_equipment()
                if eq is not None:
                    design_capacity = (
                        eq.chiller.count
                        * eq.chiller.rated_capacity_kw
                    )
                    if design_capacity > 0:
                        chilled_n = (
                            eq.chilled_pump.count
                            if chilled_pump_count is None
                            else max(0, min(int(chilled_pump_count), eq.chilled_pump.count))
                        )
                        cooling_n = (
                            eq.cooling_pump.count
                            if cooling_pump_count is None
                            else max(0, min(int(cooling_pump_count), eq.cooling_pump.count))
                        )
                        enabled_towers = [t for t in eq.cooling_towers if t.enabled]
                        schemes = sorted(
                            {
                                max(0, min(int(s), len(enabled_towers)))
                                for s in (
                                    eq.cooling_tower_schemes or [len(enabled_towers)]
                                )
                            }
                        )
                        max_scheme = max(schemes) if schemes else len(enabled_towers)
                        tower_n = (
                            max_scheme
                            if tower_count is None
                            else max(0, min(int(tower_count), max_scheme))
                        )
                        tower_rated = sum(
                            t.motor_power_kw for t in enabled_towers[:tower_n]
                        )
                        p = replace(
                            p,
                            chilled_pump_rated=chilled_n * eq.chilled_pump.motor_power_kw,
                            cooling_pump_rated=cooling_n * eq.cooling_pump.motor_power_kw,
                            chilled_pump_count=chilled_n,
                            chilled_pump_total_count=max(eq.chilled_pump.count, 1),
                            cooling_pump_count=cooling_n,
                            cooling_pump_total_count=max(eq.cooling_pump.count, 1),
                            cooling_tower_fan_rated=tower_rated,
                            cooling_tower_count=tower_n,
                            cooling_tower_total_count=max(
                                len(enabled_towers), max_scheme, 1
                            ),
                            design_cooling_capacity=design_capacity,
                        )
            except Exception:
                pass
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
        """冷冻/冷却泵功率（合计 kW）：相似定律

        P = P_rated × (f / f_rated)³
        其中 P_rated 为当前开启台数对应的额定功率合计（来自设备配置或输入覆盖）。
        """
        new_count = max(int(new_count), 0)
        if new_count <= 0 or rated_total <= 0:
            return 0.0
        return round(self._affinity(rated_total, new_freq, p), 4)

    @classmethod
    def _tower_nominal_kw(cls, scheme_count: int) -> float:
        """定频冷却塔铭牌合计：按开启台数累加电机功率，与频率无关。"""
        scheme_count = max(int(scheme_count), 0)
        if scheme_count <= 0:
            return 0.0
        try:
            from app.services.equipment_config import equipment_config_service

            enabled = [
                tower
                for tower in equipment_config_service.get_config().cooling_towers
                if tower.enabled
            ]
            if enabled:
                selected = enabled[: min(scheme_count, len(enabled))]
                return round(sum(float(tower.motor_power_kw) for tower in selected), 4)
        except Exception:
            pass
        return 0.0

    def _predict_tower_power(
        self,
        data: DeviceData,
        params: dict,
        new_freq: float,
        scheme_count: int,
        rated_total: float,
        p: EnergyModelParams,
    ) -> float:
        """冷却塔功率：现场定频，只随开启台数取铭牌合计，不按频率三次方、不跟实测比例缩放。"""
        del data, params, new_freq, rated_total, p  # 定频定额，显式不参与
        scheme_count = max(int(scheme_count), 0)
        if scheme_count <= 0:
            return 0.0
        nominal = self._tower_nominal_kw(scheme_count)
        if nominal > 0:
            return nominal
        return 0.0

    def _predict_chiller_power(
        self,
        data: DeviceData,
        params: dict,
        q_evap: float,
        demand: float,
        tchw: float,
        f_chp: float,
        f_cp: float,
        f_fan: float,
        wet_bulb: float,
        flow_chw_ratio: float,
        p: EnergyModelParams,
    ) -> tuple[float, float, float, float]:
        """机组功率：Excel 有实测时锚定实测，按物理模型比例缩放推荐参数。

        model_new 用候选工况的 q_evap；model_base 用基线控制量单独计算的
        q_evap_base，避免负荷/冷水变化时共用候选蒸发负荷导致缩放失真。

        Returns:
            (冷却水温, COP, 机组功率, ElectricEIR PLR1)
        """
        from app.services.power_baseline import current_operating_params

        cooling_water_temp, cop, model_new, plr1 = self._solve_condenser(
            q_evap=q_evap,
            tchw=tchw,
            f_cp=f_cp,
            f_fan=f_fan,
            wet_bulb=wet_bulb,
            flow_chw_ratio=flow_chw_ratio,
            p=p,
        )
        # 多轮仿真可保留首轮现场实测功率作为校准锚点；当前 chiller_power
        # 仍表示上一轮实际/预测运行功率，二者不能互相覆盖。
        measured = _finite(
            getattr(data, "chiller_power_reference", 0.0),
            0.0,
        )
        if measured <= 0:
            measured = _finite(data.chiller_power, 0.0)
        if measured <= 0:
            return cooling_water_temp, cop, model_new, plr1

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
        f_chp_b = _finite(baseline.get("chilled_pump_freq"), f_chp)
        f_cp_b = _finite(baseline.get("cooling_pump_freq"), f_cp)
        f_fan_b = _finite(baseline.get("cooling_tower_fan_freq"), f_fan)
        load_b = _finite(baseline.get("chiller_load_pct"), _finite(data.chiller_load, 80.0))
        if load_b <= 0:
            load_b = 80.0
        load_b = min(max(load_b, 0.0), 100.0)
        reference_outdoor_temp = _finite(
            getattr(data, "chiller_power_reference_outdoor_temp", 0.0),
            0.0,
        )
        reference_outdoor_humidity = _finite(
            getattr(data, "chiller_power_reference_outdoor_humidity", 0.0),
            0.0,
        )
        if reference_outdoor_temp <= 0:
            reference_outdoor_temp = _finite(data.outdoor_temp, 30.0)
        if reference_outdoor_humidity <= 0:
            reference_outdoor_humidity = _finite(data.outdoor_humidity, 60.0)
        # 基线冷水必须用现场/基线运行冷水，不能用查表值替代。
        # 否则实测 8.5℃、查表 9℃ 时，即使控制量不变也会 ratio>1，主机功率被抬高。
        tchw_b = _finite(baseline.get("chilled_water_temp"), 0.0)
        if tchw_b <= 0:
            tchw_b = _finite(data.chilled_water_temp, tchw)
        temp_factor_b = self._cooling_temp_factor(tchw_b, base_p)
        flow_chw_b = max(
            (base_p.chilled_pump_count / max(base_p.chilled_pump_total_count, 1))
            * (f_chp_b / base_p.freq_rated),
            0.0,
        )
        flow_cp_b = max(
            (base_p.cooling_pump_count / max(base_p.cooling_pump_total_count, 1))
            * (f_cp_b / base_p.freq_rated),
            0.05,
        )
        wet_bulb_base = self._wet_bulb(
            reference_outdoor_temp, reference_outdoor_humidity
        )
        approach_b = self._tower_approach(f_fan_b, base_p, flow_cp_ratio=flow_cp_b)
        cap_ft_b = self._cap_fun_t(tchw_b, wet_bulb_base + approach_b, base_p)
        coil_b = base_p.design_cooling_capacity * temp_factor_b * flow_chw_b
        machine_b = base_p.design_cooling_capacity * cap_ft_b * max(flow_chw_b, 0.0)
        delivered_b = min(coil_b, machine_b) * (load_b / 100.0)
        q_evap_base = min(delivered_b, max(demand, 0.0))
        _, _, model_base, _ = self._solve_condenser(
            q_evap=q_evap_base,
            tchw=tchw_b,
            f_cp=f_cp_b,
            f_fan=f_fan_b,
            # 多轮模拟中，基线使用首轮实测室外工况；当前工况使用实时室外温湿度。
            # 因而室外升温会反映为冷凝侧增耗，不会在同一湿球温度比值中相互抵消。
            wet_bulb=wet_bulb_base,
            flow_chw_ratio=flow_chw_b,
            p=base_p,
        )
        if model_base > 0:
            raw_ratio = model_new / model_base
            # 只要有实测主机功率，就限制缩放带宽，但须允许室外/负荷驱动的真实升功率
            has_measured = measured > 1e-6
            load_new = _finite(params.get("chiller_load_pct"), _finite(data.chiller_load, 80.0))
            controls_near_baseline = (
                abs(f_chp - f_chp_b) < 0.6
                and abs(f_cp - f_cp_b) < 0.6
                and abs(tchw - tchw_b) < 0.2
                and abs(load_new - load_b) < 1.5
                and int(params.get("chilled_pump_count", p.chilled_pump_count))
                == int(baseline.get("chilled_pump_count", p.chilled_pump_count))
                and int(params.get("cooling_pump_count", p.cooling_pump_count))
                == int(baseline.get("cooling_pump_count", p.cooling_pump_count))
                and int(params.get("cooling_tower_count", p.cooling_tower_count))
                == int(baseline.get("cooling_tower_count", p.cooling_tower_count))
            )
            max_rise = getattr(self.p, "max_component_power_rise_pct", 0.25)
            if max_rise < 0:
                max_rise = 0.25
            max_rise = min(max(max_rise, 0.05), 0.50)
            min_ratio = min(
                max(float(p.min_running_chiller_power_ratio), 0.0),
                1.0,
            )
            # 仅当控制几乎不变时抬高地板；泵频/水温有调时保留配置下限，
            # 避免 0.85 地板把泵侧变化传导到主机的真实效应抹掉。
            if has_measured and controls_near_baseline:
                min_ratio = max(min_ratio, 0.85)
            if has_measured:
                if controls_near_baseline:
                    # 控制不变：主要反映室外湿球/负荷变化，信任模型相对变化
                    ratio = min(max(raw_ratio, 0.88), 1.0 + max_rise)
                else:
                    # 有调参：比例下限跟配置 min_ratio，允许泵→主机耦合体现
                    ratio = min(max(raw_ratio, min_ratio), 1.0 + max_rise)
            else:
                ratio = raw_ratio
            scaled = measured * ratio
            # 主机台数固定运行时，禁止比例缩放低于实测×min_ratio
            running_floor = measured * min_ratio
            scaled = max(scaled, running_floor)
            if measured > 0:
                cap = measured * (1.0 + max_rise)
                if scaled > cap:
                    scaled = cap
            return cooling_water_temp, cop, scaled, plr1
        return cooling_water_temp, cop, model_new, plr1

    @staticmethod
    def _calibrate_eta_from_nameplate(
        design_capacity: float,
        design_power: float,
        p: EnergyModelParams,
    ) -> float:
        """用铭牌冷量/电功率把 eta 校准到设计工况附近的卡诺修正系数。

        标定水温口径与求解器一致：冷凝侧用设计冷却水**进水** + 冷凝器逼近度。
        铭牌 COP 异常偏高（如统计口径含非压缩机功率）时钳位，避免 eta>1。
        """
        if design_capacity <= 0 or design_power <= 0:
            return p.eta_chiller
        target_cop = design_capacity / design_power
        t_evap_k = (p.design_chw_temp - p.evap_approach) + _KELVIN
        # 与 _solve_condenser / CapFunT 一致：进水参考，非冷凝器出水
        t_cond_k = (p.design_cw_temp + p.cond_approach) + _KELVIN
        carnot = t_evap_k / max(t_cond_k - t_evap_k, 1.0)
        if carnot <= 0:
            return p.eta_chiller
        eta = target_cop / carnot
        # 物理上 eta 为「实际/卡诺」分数；异常铭牌时回退配置值方向并钳位
        return min(max(eta, 0.30), 0.85)

    def _eir_fplr(self, plr: float, p: EnergyModelParams | None = None) -> float:
        """部分负荷电功率比：P/P_full ≈ a + b·PLR + c·PLR² + d·PLR³。

        PLR=1 时归一化为 1；低负荷时 EIRFPLR/PLR > 1，表示比功率升高。
        """
        p = p or self.p
        x = min(max(plr, 0.05), 1.0)
        raw = p.plr_eir_a + p.plr_eir_b * x + p.plr_eir_c * (x**2) + p.plr_eir_d * (x**3)
        # 强制满负荷点为 1，避免系数配置漂移
        at_full = p.plr_eir_a + p.plr_eir_b + p.plr_eir_c + p.plr_eir_d
        if at_full > 1e-6:
            raw = raw / at_full
        return max(raw, 0.05)

    def _cap_fun_t(
        self,
        tchw: float,
        t_cw_enter: float,
        p: EnergyModelParams | None = None,
    ) -> float:
        """简化可用容量修正（ElectricEIR capFunT 精神，无厂商 6 系数表）。

        设计点 (design_chw_temp, design_cw_temp) → 1.0；
        冷却水进水升高 → 容量下降；冷水出水升高 → 容量略升。
        """
        p = p or self.p
        if not p.enable_cap_fun_t:
            return 1.0
        d_eva = tchw - p.design_chw_temp
        d_con = t_cw_enter - p.design_cw_temp
        raw = 1.0 + p.cap_fun_t_chw * d_eva - p.cap_fun_t_cw * d_con
        return min(max(raw, p.cap_fun_t_min), p.cap_fun_t_max)

    def _affinity(self, rated_power: float, freq: float, p: EnergyModelParams | None = None) -> float:
        """风机水泵相似定律：P = P_rated * (f / f_rated)³。"""
        p = p or self.p
        ratio = max(freq, 0.0) / p.freq_rated
        return rated_power * (ratio ** 3)

    def _tower_approach(
        self,
        f_fan: float,
        p: EnergyModelParams | None = None,
        flow_cp_ratio: float = 1.0,
    ) -> float:
        """冷却塔逼近度：风量越大逼近度越小；水量不足时抬升（Scheier 思想）。"""
        p = p or self.p
        if p.cooling_tower_count <= 0:
            # 无冷却塔运行时，冷凝侧散热能力极差；不是直接报错，而是让目标函数
            # 通过更高冷凝温度/更低 COP 自然抛弃该方案。
            return p.tower_approach_max + 20.0
        # 定频 50Hz 时视为满风量；兼容旧配置的 [20,45] 变频塔。
        span = 50.0 - 20.0
        ratio = min(max((f_fan - 20.0) / span, 0.0), 1.0)
        base = p.tower_approach_max - (p.tower_approach_max - p.tower_approach_min) * ratio
        # 相对已启用/方案上限台数：开满无台数惩罚，少开则逼近度变差
        ref_count = max(
            int(getattr(p, "cooling_tower_total_count", 0) or 0),
            int(p.cooling_tower_count),
            1,
        )
        count_penalty = max(0, ref_count - int(p.cooling_tower_count)) * 1.2
        # 冷却水流量低于额定 → 逼近度变差（℃）
        flow = min(max(flow_cp_ratio, 0.0), 1.5)
        water_penalty = max(0.0, 1.0 - flow) * max(p.tower_approach_water_k, 0.0)
        return base + count_penalty + water_penalty

    def _solve_condenser(
        self,
        q_evap: float,
        tchw: float,
        f_cp: float,
        f_fan: float,
        wet_bulb: float,
        flow_chw_ratio: float = 1.0,
        p: EnergyModelParams | None = None,
    ) -> tuple[float, float, float, float]:
        """定点迭代求解冷却水温、COP、机组功率。

        对齐 Buildings ElectricEIR：
            Q_ava = Q_nom * capFunT * φ_chw（冷冻水流量比与外层供冷能力对齐）
            PLR1 / CR / PLR2(PLRMinUnl)
            P = (Q_ava / COP_carnot) * EIRFPLR(PLR2) * CR
        关闭部分负荷曲线时 EIRFPLR=PLR2（恒 COP 线性），禁止误用 1.0。
        其中 COP_carnot 承担 EIRFunT 的温区角色。

        冷凝侧排热 Q_reject = Q_evap + W_compressor，用少量迭代收敛。
        返回 (冷却水温, 有效COP, 机组功率, PLR1)。
        """
        p = p or self.p
        # 冷却泵流量不足会抬高冷凝侧温升（range），以频率比反映
        flow_cp_ratio = max(
            (p.cooling_pump_count / max(p.cooling_pump_total_count, 1))
            * (f_cp / p.freq_rated),
            0.05,
        )  # 下限保护，防止除零/发散；过低时才钳，避免掩盖低流量升温
        approach = self._tower_approach(f_fan, p, flow_cp_ratio=flow_cp_ratio)
        flow_chw = max(float(flow_chw_ratio), 0.0)

        t_evap_k = (tchw - p.evap_approach) + _KELVIN
        plr_min = max(float(p.plr_min), 1e-3)
        plr_min_unl = max(float(p.plr_min_unl), plr_min)

        if q_evap <= 1e-9:
            t_cw = wet_bulb + approach
            return t_cw, 4.0, 0.0, 0.0

        chiller_power = q_evap / 4.0  # 初值：假设 COP≈4
        cooling_water_temp = wet_bulb + approach
        cop = 4.0
        plr1 = 0.0

        for _i in range(6):
            q_reject = q_evap + chiller_power
            # 冷却水温 = 湿球 + 塔逼近度 + 冷凝器温升
            # ΔT = Q_rej / (ṁ·cp)；设计流量按「设计排热 / 5K 温升」反推
            capacity = max(p.design_cooling_capacity, 1e-6)
            design_cop = 5.5
            if float(getattr(p, "design_chiller_power", 0.0) or 0.0) > 1e-6:
                design_cop = max(capacity / float(p.design_chiller_power), 2.0)
            q_rej_design = capacity * (1.0 + 1.0 / design_cop)
            m_cp = (q_rej_design / 5.0) * max(float(flow_cp_ratio), 1e-6)  # kW/K
            cond_range = q_reject / max(m_cp, 1e-6)
            t_cw_enter = wet_bulb + approach  # 冷凝器进水 ≈ 塔出水
            cooling_water_temp = t_cw_enter + cond_range
            # ElectricEIR / CapFunT：温区依赖用冷凝器进水，与铭牌 η 标定口径一致
            # （出水仅用于回路水温展示，不参与卡诺 COP）
            t_cond_k = (t_cw_enter + p.cond_approach) + _KELVIN

            denom = max(t_cond_k - t_evap_k, 1.0)  # 防止温差过小导致 COP 爆炸
            cop_carnot = p.eta_chiller * t_evap_k / denom
            cop_carnot = max(cop_carnot, 1.5)  # 工程下限，避免非物理极端值

            cap_ft = self._cap_fun_t(tchw, t_cw_enter, p)
            # 与外层 delivered 对齐：可用容量随冷冻水流量比下降
            q_ava = max(p.design_cooling_capacity * cap_ft * max(flow_chw, 1e-6), 1e-6)

            plr1 = min(max(q_evap / q_ava, 0.0), 1.0)
            cr = min(plr1 / plr_min, 1.0)
            plr2 = max(plr1, plr_min_unl)

            if p.enable_part_load_curve:
                eir_fplr = self._eir_fplr(plr2, p)
            else:
                # 恒 COP：EIRFPLR ≈ PLR，使 P ≈ Q_evap/COP（而非误用 1.0 导致半负荷≈满功率）
                eir_fplr = plr2

            # ElectricEIR：P = Q_ava * (1/COP) * EIRFunPLR * CR
            new_power = (q_ava / cop_carnot) * eir_fplr * cr
            # 有效 COP（含部分负荷）：供展示与节能率解释
            cop = q_evap / max(new_power, 1e-6) if q_evap > 0 else cop_carnot
            if abs(new_power - chiller_power) < 1e-4:
                chiller_power = new_power
                break
            chiller_power = new_power

        return cooling_water_temp, cop, chiller_power, plr1

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
