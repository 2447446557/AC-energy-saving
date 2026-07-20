"""楼宇室内温度口径：多末端聚合为单一控制用温度。

一栋楼可有数百个末端温控点；寻优舒适约束与室温预测只能吃一个标量。
默认取最高温（偏安全）：优先保证最热区域不越出舒适区。
"""

from __future__ import annotations

import math
from typing import Any, Sequence


def _finite_temps(values: Sequence[Any]) -> list[float]:
    out: list[float] = []
    for raw in values:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def aggregate_indoor_temps(
    temps: Sequence[Any],
    *,
    mode: str = "max",
) -> float | None:
    """将多末端温度聚合为一个控制用温度。

    mode:
      - max: 最高温（默认，楼宇供冷偏安全）
      - p95: 95 分位（略抗单点坏值）
      - mean: 算术平均（仅作对比，不建议作控制）
    """
    vals = _finite_temps(temps)
    if not vals:
        return None
    mode_norm = (mode or "max").strip().lower()
    if mode_norm == "mean":
        return sum(vals) / len(vals)
    if mode_norm in {"p95", "percentile95", "q95"}:
        ordered = sorted(vals)
        if len(ordered) == 1:
            return ordered[0]
        idx = 0.95 * (len(ordered) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(ordered) - 1)
        frac = idx - lo
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac
    return max(vals)


def control_indoor_temp(data: Any, *, mode: str = "max") -> float:
    """从 DeviceData / dict 解析控制用室内温度。

    优先 ``indoor_temps`` 多点聚合；否则回退 ``indoor_temp`` 单点。
    """
    if data is None:
        return 0.0
    if isinstance(data, dict):
        multi = data.get("indoor_temps") or []
        single = data.get("indoor_temp", 0.0)
    else:
        multi = getattr(data, "indoor_temps", None) or []
        single = getattr(data, "indoor_temp", 0.0)
    agg = aggregate_indoor_temps(multi, mode=mode)
    if agg is not None:
        return float(agg)
    try:
        v = float(single)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0
