"""Excel 实测功率优先、配置兜底的功率缩放工具。"""

from __future__ import annotations


def scale_measured_component(
    measured_total: float,
    base_freq: float,
    new_freq: float,
    base_count: int,
    new_count: int,
) -> float | None:
    """按相似定律 + 台数变化，从 Excel 实测总功率推算新工况功率。

    Returns:
        推算功率；无实测时返回 None，由调用方回退到配置额定功率。
    """
    if measured_total <= 0 or base_freq <= 0:
        return None
    freq_factor = (new_freq / base_freq) ** 3
    count_factor = new_count / max(base_count, 1)
    return measured_total * freq_factor * count_factor
