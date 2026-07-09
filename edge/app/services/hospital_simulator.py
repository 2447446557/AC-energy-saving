"""高仿真度医院空调时序模拟数据生成（仿真测试核心）

对应需求文档 3.3 节：编写高仿真度医院空调时序模拟数据生成逻辑，模拟极端
场景（传感器故障、数据断连、负荷骤变、季节工况切换），用于算法调试与鲁棒
性专项测试。

仿真特性
--------
1. 时序连续性：内部维护虚拟时钟，逐周期推进；负荷/温度带热惰性（AR(1) 平滑），
   贴近真实工况的缓变特征，而非独立随机噪声。
2. 医院负荷画像：24h 基础负荷（病房恒温）+ 日间手术室/门诊高峰 + 随机扰动。
3. 季节工况：室外温湿度随月份变化，可主动切换季节。
4. 物理自洽：机组/水泵/风机功率由能耗模型据环境与控制参数反算，
   保证模拟数据内部一致（能耗随工况合理波动）。
5. 极端场景注入：可按概率或指定场景注入传感器跳变、数据断连、负荷骤变。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from loguru import logger

from app.algorithms.energy_model import ACEnergyModel
from app.schemas.device import DeviceData


@dataclass
class AnomalyConfig:
    """极端场景注入概率配置（0~1）。"""

    sensor_spike: float = 0.03   # 传感器瞬时跳变
    data_dropout: float = 0.03   # 数据断连（关键字段丢失）
    load_surge: float = 0.02     # 负荷骤升/骤降
    enabled: bool = True         # 总开关（关闭则输出纯净数据）


# 24 小时医院综合负荷权重（相对基准的倍率，含病房恒定负荷 + 日间高峰）
_HOSPITAL_HOURLY_WEIGHT = (
    0.55, 0.50, 0.48, 0.47, 0.48, 0.52,  # 00-05 夜间低谷
    0.62, 0.75, 0.90, 1.00, 1.05, 1.02,  # 06-11 门诊/手术高峰
    0.95, 1.00, 1.03, 0.98, 0.90, 0.82,  # 12-17 午后
    0.78, 0.72, 0.68, 0.64, 0.60, 0.57,  # 18-23 晚间回落
)


class HospitalDataGenerator:
    """医院中央空调高仿真度时序数据生成器（实现 DataGenerator）。"""

    def __init__(
        self,
        energy_model: ACEnergyModel | None = None,
        anomaly: AnomalyConfig | None = None,
        step_minutes: float = 5.0,
        base_load_kw: float = 90.0,
        seed: int | None = None,
        start_time: datetime | None = None,
    ) -> None:
        """
        Args:
            energy_model: 能耗模型，用于反算自洽功率（缺省新建默认模型）。
            anomaly: 极端场景注入配置。
            step_minutes: 每次生成推进的虚拟分钟数。
            base_load_kw: 医院综合基准负荷（kW）。
            seed: 随机种子（用于可复现测试）。
            start_time: 虚拟起始时间（缺省当前时间）。
        """
        self._model = energy_model or ACEnergyModel()
        self._anomaly = anomaly or AnomalyConfig()
        self._step = timedelta(minutes=step_minutes)
        # 下限保护，避免基准负荷为 0 导致后续归一化除零
        self._base_load = max(float(base_load_kw), 1.0)
        self._rng = np.random.default_rng(seed)

        self._vtime = start_time or datetime.now()
        # 带热惰性的状态变量（None 表示尚未初始化）
        self._load: float | None = None
        self._outdoor_temp: float | None = None
        self._season_offset: float = 0.0  # 季节主动切换的附加偏移（℃）

    # ---------- DataGenerator 协议实现 ----------

    def generate(self, scenario: str | None = None) -> DeviceData:
        """生成一条时序工况数据。

        Args:
            scenario: 强制注入的场景（"normal"/"spike"/"dropout"/"surge"），
                None 时按 AnomalyConfig 概率随机注入，便于测试指定极端场景。
        """
        self._vtime += self._step

        env = self._sample_environment()
        load = self._sample_load(scenario)

        # 模拟“寻优前”的既有运行控制参数（带小幅波动，代表现场基线工况）
        control = self._baseline_control()

        # 依据环境 + 控制参数，用能耗模型反算自洽的机组/辅机功率与冷却水温
        data = self._compose(env, load, control)

        # 极端场景注入（在自洽数据基础上人为破坏，供鲁棒性测试）
        data = self._inject_anomaly(data, scenario)
        return data

    # ---------- 环境 / 负荷 采样 ----------

    def _sample_environment(self) -> dict[str, float]:
        """室外温湿度：季节基线 + 日内波动 + 热惰性平滑。"""
        month = self._vtime.month
        hour = self._vtime.hour + self._vtime.minute / 60.0

        # 季节基线：以 1 月最低、7 月最高的正弦近似
        seasonal = 18.0 + 12.0 * np.sin((month - 4) / 12.0 * 2 * np.pi)
        # 日内波动：约 14 时最高、凌晨最低
        diurnal = 4.0 * np.sin((hour - 9) / 24.0 * 2 * np.pi)
        target = seasonal + diurnal + self._season_offset + self._rng.normal(0, 0.6)

        if self._outdoor_temp is None:
            self._outdoor_temp = target
        else:
            # AR(1) 热惰性：新值向目标缓慢逼近
            self._outdoor_temp = 0.85 * self._outdoor_temp + 0.15 * target
        outdoor_temp = float(self._outdoor_temp)

        # 湿度与温度弱负相关，夜间偏高
        humidity = float(
            np.clip(72.0 - 0.6 * (outdoor_temp - 20.0) + self._rng.normal(0, 4), 25, 98)
        )
        return {"outdoor_temp": round(outdoor_temp, 2), "outdoor_humidity": round(humidity, 1)}

    def _sample_load(self, scenario: str | None) -> float:
        """室内综合冷负荷：小时画像 * 基准 + 扰动 + 热惰性。"""
        weight = _HOSPITAL_HOURLY_WEIGHT[self._vtime.hour]
        target = self._base_load * weight * (1.0 + self._rng.normal(0, 0.05))

        if self._load is None:
            self._load = target
        else:
            self._load = 0.8 * self._load + 0.2 * target
        return max(float(self._load), 5.0)

    def _baseline_control(self) -> dict[str, float]:
        """现场既有（未寻优）控制参数，带小幅噪声。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            tower_freq = next(
                (tower.fixed_freq for tower in eq.cooling_towers if tower.enabled),
                50.0,
            )
            chilled_min, chilled_max = eq.chilled_pump.min_freq, eq.chilled_pump.max_freq
            cooling_min, cooling_max = eq.cooling_pump.min_freq, eq.cooling_pump.max_freq
            chilled_base = (chilled_min + chilled_max) / 2.0
            cooling_base = (cooling_min + cooling_max) / 2.0
            return {
                "chilled_water_temp": float(np.clip(7.0 + self._rng.normal(0, 0.2), 6.0, 12.0)),
                "chilled_pump_freq": float(
                    np.clip(chilled_base + self._rng.normal(0, 1.0), chilled_min, chilled_max)
                ),
                "cooling_pump_freq": float(
                    np.clip(cooling_base + self._rng.normal(0, 1.0), cooling_min, cooling_max)
                ),
                "cooling_tower_fan_freq": float(tower_freq),
            }
        except Exception:
            pass
        return {
            "chilled_water_temp": float(np.clip(7.0 + self._rng.normal(0, 0.2), 6.0, 12.0)),
            "chilled_pump_freq": float(np.clip(42.0 + self._rng.normal(0, 1.0), 25.0, 50.0)),
            "cooling_pump_freq": float(np.clip(42.0 + self._rng.normal(0, 1.0), 25.0, 50.0)),
            "cooling_tower_fan_freq": float(np.clip(38.0 + self._rng.normal(0, 1.0), 20.0, 45.0)),
        }

    # ---------- 物理自洽组装 ----------

    def _compose(
        self, env: dict[str, float], load: float, control: dict[str, float]
    ) -> DeviceData:
        """据环境/负荷/控制参数，用能耗模型反算功率，组装完整工况。"""
        indoor_temp = float(np.clip(25.0 + self._rng.normal(0, 0.3), 23.0, 27.0))
        terminal_fan = round(2.0 + load / self._base_load * 1.0, 2)

        # 先构造用于能耗反算的临时工况
        probe = DeviceData(
            timestamp=self._vtime,
            outdoor_temp=env["outdoor_temp"],
            outdoor_humidity=env["outdoor_humidity"],
            indoor_temp=indoor_temp,
            indoor_load=round(load, 2),
            terminal_fan_power=terminal_fan,
            **control,
        )
        bd = self._model.predict(probe, control)

        return DeviceData(
            timestamp=self._vtime,
            outdoor_temp=env["outdoor_temp"],
            outdoor_humidity=env["outdoor_humidity"],
            indoor_temp=indoor_temp,
            indoor_humidity=round(float(np.clip(55 + self._rng.normal(0, 3), 40, 70)), 1),
            indoor_load=round(load, 2),
            chiller_load=round(min(load / self._model.p.design_cooling_capacity * 100, 100), 1),
            chiller_power=bd.chiller_power,
            chilled_water_temp=round(control["chilled_water_temp"], 2),
            cooling_water_temp=bd.cooling_water_temp,
            chilled_pump_freq=round(control["chilled_pump_freq"], 2),
            chilled_pump_power=bd.chilled_pump_power,
            cooling_pump_freq=round(control["cooling_pump_freq"], 2),
            cooling_pump_power=bd.cooling_pump_power,
            cooling_tower_fan_freq=round(control["cooling_tower_fan_freq"], 2),
            cooling_tower_fan_power=bd.cooling_tower_fan_power,
            terminal_fan_power=terminal_fan,
            total_power=bd.total_power,
        )

    # ---------- 极端场景注入 ----------

    def _inject_anomaly(self, data: DeviceData, scenario: str | None) -> DeviceData:
        """按指定场景或概率注入极端异常，破坏自洽数据供鲁棒性测试。"""
        a = self._anomaly
        if scenario == "normal":
            return data
        if not a.enabled and scenario is None:
            return data

        values = data.model_dump()

        def _roll(prob: float) -> bool:
            return float(self._rng.random()) < prob

        # 传感器瞬时跳变：某温度/功率字段突变为非物理极值
        if scenario == "spike" or (scenario is None and _roll(a.sensor_spike)):
            field = self._rng.choice(
                ["chilled_water_temp", "cooling_water_temp", "total_power", "indoor_temp"]
            )
            values[field] = float(values[field]) + float(self._rng.choice([-50, 50, 999]))
            logger.debug(f"[仿真] 注入传感器跳变: {field}={values[field]}")

        # 数据断连：关键字段丢失（置 0，清洗器按缺失处理）
        if scenario == "dropout" or (scenario is None and _roll(a.data_dropout)):
            field = self._rng.choice(
                ["indoor_temp", "chilled_water_temp", "cooling_water_temp"]
            )
            values[field] = 0.0
            logger.debug(f"[仿真] 注入数据断连: {field}=0")

        # 负荷骤变：室内负荷瞬时骤升/骤降
        if scenario == "surge" or (scenario is None and _roll(a.load_surge)):
            factor = float(self._rng.choice([0.3, 2.2]))
            values["indoor_load"] = round(float(values["indoor_load"]) * factor, 2)
            logger.debug(f"[仿真] 注入负荷骤变: x{factor}")

        return DeviceData(**values)

    # ---------- 场景控制（供测试/演示） ----------

    def switch_season(self, offset_c: float) -> None:
        """主动切换季节工况（附加室外温度偏移，℃）。"""
        self._season_offset = float(offset_c)
        self._outdoor_temp = None  # 重置热惰性，令新季节快速生效
        logger.info(f"[仿真] 季节工况切换，室外温度偏移 {offset_c:+.1f}℃")

    def generate_series(self, n: int, scenario: str | None = "normal") -> list[DeviceData]:
        """连续生成 n 条时序数据（仿真报告/批量测试用）。"""
        return [self.generate(scenario=scenario) for _ in range(n)]
