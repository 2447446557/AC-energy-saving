"""数据清洗与鲁棒容错模块（医院刚需 · 系统稳定性核心）

本模块决定寻优输入数据的质量，是项目鲁棒性的核心体现。处理四类问题
（对应设计文档 4.3 节）：

1. 超限跳变过滤：温度/频率/功耗等瞬时突变，判定为传感器脏数据并剔除，
   用历史插值替代，杜绝异常值污染寻优目标函数。
2. 缺失值插值：短时断采（None / 非有限值 / 物理上不可能的 0）自动补全，
   避免寻优因缺数据中断。
3. 数据平滑降噪：对连续采样做指数加权滑动平均（EWMA），抑制工况抖动，
   防止下游频繁调节设备。
4. 连续异常判定（熔断前置）：统计连续异常样本数，超阈值置“熔断”标志，
   由兜底模块据此停止寻优更新、切回安全固定参数。

设计约定
--------
- 清洗器持有内部历史缓冲区（有状态），单样本清洗即可参考历史趋势。
- 所有阈值显性定义、可解释，不存在隐性魔法逻辑。
- 清洗永不抛异常：任何意外都退化为“透传 + 记异常”，保证边缘端不宕机。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from loguru import logger

from app.schemas.device import DeviceData


@dataclass(frozen=True)
class FieldSpec:
    """单字段清洗规格。

    Attributes:
        name: DeviceData 字段名。
        low: 物理合理下限（含）。
        high: 物理合理上限（含）。
        max_jump: 相邻样本允许的最大变化量（超过判定为跳变）。
        allow_zero: 是否允许 0（False 时 0 视为缺失/断采）。
    """

    name: str
    low: float
    high: float
    max_jump: float
    allow_zero: bool = True


# 关键（安全相关）字段：其一旦连续异常，即便整样本异常占比未超阈值，
# 也判定该样本异常——因为寻优/控制强依赖这些量，缺失即“盲飞”，需触发熔断。
_CRITICAL_FIELDS: frozenset[str] = frozenset(
    {"indoor_temp", "indoor_load", "chilled_water_temp", "cooling_water_temp"}
)

# 关键工况字段清洗规格（阈值贴合医院中央空调常见量级，可现场校准）
_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("outdoor_temp", -30.0, 55.0, 8.0),
    FieldSpec("outdoor_humidity", 0.0, 100.0, 30.0),
    FieldSpec("indoor_temp", 10.0, 40.0, 3.0, allow_zero=False),
    FieldSpec("indoor_humidity", 0.0, 100.0, 30.0),
    FieldSpec("indoor_load", 0.0, 5000.0, 60.0),
    FieldSpec("chiller_load", 0.0, 100.0, 40.0),
    FieldSpec("chiller_power", 0.0, 5000.0, 100.0),
    FieldSpec("chilled_water_temp", 2.0, 20.0, 4.0, allow_zero=False),
    FieldSpec("cooling_water_temp", 10.0, 50.0, 6.0, allow_zero=False),
    FieldSpec("chilled_pump_freq", 0.0, 50.0, 15.0),
    FieldSpec("chilled_pump_power", 0.0, 500.0, 20.0),
    FieldSpec("cooling_pump_freq", 0.0, 50.0, 15.0),
    FieldSpec("cooling_pump_power", 0.0, 500.0, 20.0),
    FieldSpec("cooling_tower_fan_freq", 0.0, 50.0, 15.0),
    FieldSpec("cooling_tower_fan_power", 0.0, 500.0, 20.0),
    FieldSpec("terminal_fan_power", 0.0, 500.0, 20.0),
    FieldSpec("total_power", 0.0, 20000.0, 300.0),
)


@dataclass
class CleanReport:
    """单次清洗报告（可用于告警/审计）。"""

    total_fields: int = 0
    missing_fixed: int = 0
    spikes_filtered: int = 0
    out_of_range: int = 0
    regime_shifts: int = 0
    is_anomalous_sample: bool = False
    consecutive_anomalies: int = 0
    circuit_broken: bool = False
    details: list[str] = field(default_factory=list)


class RobustDataCleaner:
    """鲁棒数据清洗器（实现 IDataCleaner）。

    Args:
        history_size: 每字段历史窗口长度（用于跳变判定与插值）。
        ewma_alpha: EWMA 平滑系数（越大越贴近新值，越小越平滑）。
        anomaly_field_ratio: 单样本异常字段占比阈值，超过则整样本判异常。
        circuit_break_threshold: 连续异常样本数阈值，达到即触发熔断。
        regime_shift_confirm: 连续多少个“同向、自洽”的越跳读数被确认为真实
            工况突变（而非瞬时跳变），从而自适应接受新工况并重置基线。
    """

    def __init__(
        self,
        history_size: int = 20,
        ewma_alpha: float = 0.4,
        anomaly_field_ratio: float = 0.3,
        circuit_break_threshold: int = 5,
        regime_shift_confirm: int = 3,
    ) -> None:
        self._history: dict[str, deque[float]] = {
            spec.name: deque(maxlen=history_size) for spec in _FIELD_SPECS
        }
        # 越跳候选缓冲：连续越跳且自洽达到 confirm 次即判定为工况突变
        self._pending: dict[str, deque[float]] = {
            spec.name: deque(maxlen=max(regime_shift_confirm, 1))
            for spec in _FIELD_SPECS
        }
        self._specs = {spec.name: spec for spec in _FIELD_SPECS}
        self._alpha = ewma_alpha
        self._anomaly_field_ratio = anomaly_field_ratio
        self._circuit_break_threshold = circuit_break_threshold
        self._regime_shift_confirm = max(regime_shift_confirm, 1)

        self._consecutive_anomalies = 0
        self._circuit_broken = False
        self.last_report: CleanReport = CleanReport()

    # ---------- IDataCleaner 协议实现 ----------

    def clean(self, raw: DeviceData) -> DeviceData:
        """清洗单条工况数据，返回清洗后的新对象（不修改入参）。"""
        report = CleanReport(total_fields=len(self._specs))
        try:
            values = raw.model_dump()
        except Exception as e:  # 极端防护：入参异常也不允许崩溃
            logger.error(f"数据清洗读取失败，原样透传: {e}")
            return raw

        critical_hit = False
        for name, spec in self._specs.items():
            raw_val = values.get(name)
            cleaned_val, tag = self._clean_field(spec, raw_val)
            values[name] = cleaned_val
            if tag == "regime_shift":
                # 确认为真实工况突变：重置该字段基线，令后续以新工况为参照，
                # 且不计入异常（属于合法自适应，不应触发熔断）。
                self._history[name].clear()
            self._history[name].append(cleaned_val)
            if tag:
                report.details.append(f"{name}: {tag}")
                if tag == "missing":
                    report.missing_fixed += 1
                elif tag == "spike":
                    report.spikes_filtered += 1
                elif tag == "out_of_range":
                    report.out_of_range += 1
                elif tag == "regime_shift":
                    report.regime_shifts += 1
                    logger.info(f"[清洗] 识别到工况突变，自适应接受新工况: {name}={cleaned_val:.2f}")
                    continue  # 工况突变不计异常
                if name in _CRITICAL_FIELDS:
                    critical_hit = True

        # 整样本异常判定：异常字段占比超阈值，或任一关键安全字段异常 → 判异常
        anomaly_count = (
            report.missing_fixed + report.spikes_filtered + report.out_of_range
        )
        ratio_hit = (
            anomaly_count / max(report.total_fields, 1)
        ) >= self._anomaly_field_ratio
        report.is_anomalous_sample = ratio_hit or critical_hit

        # 连续异常计数 + 熔断判定
        if report.is_anomalous_sample:
            self._consecutive_anomalies += 1
        else:
            self._consecutive_anomalies = 0

        if self._consecutive_anomalies >= self._circuit_break_threshold:
            if not self._circuit_broken:
                logger.error(
                    f"数据连续异常 {self._consecutive_anomalies} 次，触发熔断，"
                    f"寻优应切回安全固定参数"
                )
            self._circuit_broken = True
        elif self._consecutive_anomalies == 0:
            # 恢复正常则解除熔断
            if self._circuit_broken:
                logger.info("数据恢复正常，解除熔断")
            self._circuit_broken = False

        report.consecutive_anomalies = self._consecutive_anomalies
        report.circuit_broken = self._circuit_broken
        self.last_report = report

        if anomaly_count > 0:
            logger.debug(
                f"清洗完成: 缺失{report.missing_fixed} 跳变{report.spikes_filtered} "
                f"越界{report.out_of_range} 连续异常{self._consecutive_anomalies}"
            )

        try:
            return DeviceData(**values)
        except Exception as e:
            logger.error(f"清洗结果重建失败，原样透传: {e}")
            return raw

    # ---------- 状态查询（供兜底模块使用） ----------

    def is_circuit_broken(self) -> bool:
        """当前是否处于数据熔断状态。"""
        return self._circuit_broken

    def reset(self) -> None:
        """清空历史与熔断状态（重启/手动复位时调用）。"""
        for dq in self._history.values():
            dq.clear()
        for dq in self._pending.values():
            dq.clear()
        self._consecutive_anomalies = 0
        self._circuit_broken = False

    # ---------- 内部清洗逻辑 ----------

    def _clean_field(self, spec: FieldSpec, raw_val) -> tuple[float, str]:
        """清洗单字段，返回 (清洗值, 异常标签)。标签为空串表示正常。

        标签取值：missing / out_of_range / spike / regime_shift / ""。
        """
        history = self._history[spec.name]
        pending = self._pending[spec.name]
        fallback = self._interpolate(spec, history)

        # 1) 缺失 / 非有限值（清空越跳缓冲：缺失不构成工况突变）
        if raw_val is None or not isinstance(raw_val, (int, float)) or (
            isinstance(raw_val, float) and not math.isfinite(raw_val)
        ):
            pending.clear()
            return fallback, "missing"
        value = float(raw_val)

        # 2) 断采：不允许 0 的字段出现 0，视为缺失
        if not spec.allow_zero and abs(value) < 1e-9:
            pending.clear()
            return fallback, "missing"

        # 3) 超出物理合理范围 → 越界剔除（物理不可能值永不接受为工况突变）
        if value < spec.low or value > spec.high:
            pending.clear()
            return fallback, "out_of_range"

        # 4) 跳变判定：与上一有效值差异过大
        if history and abs(value - history[-1]) > spec.max_jump:
            # 越跳候选入缓冲，判断是否为“持续且自洽”的真实工况突变
            pending.append(value)
            if len(pending) >= self._regime_shift_confirm and (
                max(pending) - min(pending)
            ) <= spec.max_jump:
                # 连续多次越跳且彼此一致 → 确认工况突变，采用其均值作为新基线
                accepted = sum(pending) / len(pending)
                pending.clear()
                return float(min(max(accepted, spec.low), spec.high)), "regime_shift"
            # 尚未确认 → 当作瞬时脏数据剔除
            return fallback, "spike"

        # 5) 正常值 → 清空越跳缓冲 + EWMA 平滑降噪
        pending.clear()
        if history:
            smoothed = self._alpha * value + (1.0 - self._alpha) * history[-1]
            return smoothed, ""
        return value, ""

    def _interpolate(self, spec: FieldSpec, history: deque[float]) -> float:
        """异常/缺失时的替代值：优先线性外推，其次沿用末值，再次取区间中值。"""
        if len(history) >= 2:
            # 线性外推：延续最近两点的趋势，比单纯持平更贴近真实工况
            trend = history[-1] + (history[-1] - history[-2])
            return min(max(trend, spec.low), spec.high)
        if len(history) == 1:
            return history[-1]
        # 无历史：取物理区间中值作为最保守估计
        return (spec.low + spec.high) / 2.0
