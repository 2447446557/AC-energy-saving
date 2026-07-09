"""批量寻优输入字段溯源与审计报告。"""

from __future__ import annotations

from typing import Any

# 寻优输入字段中文名
FIELD_LABELS: dict[str, str] = {
    "timestamp": "时间戳",
    "outdoor_temp": "室外温度 ℃",
    "outdoor_humidity": "室外湿度 %",
    "indoor_temp": "室内温度 ℃",
    "indoor_humidity": "室内湿度 %",
    "indoor_load": "室内负荷 kW",
    "chiller_load": "机组负载 %",
    "chiller_power": "机组功率 kW",
    "chilled_water_temp": "冷水出水温度 ℃",
    "cooling_water_temp": "冷却水出水温度 ℃",
    "chilled_pump_freq": "冷冻泵频率 Hz",
    "chilled_pump_power": "冷冻泵功率 kW",
    "cooling_pump_freq": "冷却泵频率 Hz",
    "cooling_pump_power": "冷却泵功率 kW",
    "cooling_tower_fan_freq": "冷却塔频率 Hz",
    "cooling_tower_fan_power": "冷却塔功率 kW",
    "terminal_fan_power": "末端风机功率 kW",
    "total_power": "系统总功率 kW",
}

SOURCE_LABELS: dict[str, str] = {
    "excel_column": "Excel 直接列",
    "excel_multi_header": "Excel 多级表头",
    "excel_derived": "Excel 派生汇总",
    "batch_default": "系统配置缺省值",
    "equipment_config": "设备配置推算",
    "approximation": "工程近似",
    "computed": "加总计算",
    "missing": "缺失",
}


def init_field_sources(site_defaults: dict[str, float]) -> dict[str, dict[str, Any]]:
    """初始化各字段溯源占位。"""
    return {
        field: {
            "label": FIELD_LABELS.get(field, field),
            "value": None,
            "source": "missing",
            "source_label": SOURCE_LABELS["missing"],
            "excel_column": None,
            "detail": None,
            "substituted": False,
            "default_used": site_defaults.get(field),
        }
        for field in FIELD_LABELS
    }


def mark_field(
    sources: dict[str, dict[str, Any]],
    field: str,
    value: Any,
    source: str,
    *,
    excel_column: str | None = None,
    detail: str | None = None,
    substituted: bool = False,
) -> None:
    if field not in sources:
        return
    sources[field].update(
        {
            "value": value,
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "excel_column": excel_column,
            "detail": detail,
            "substituted": substituted,
        }
    )


def build_input_audit(
    device_data: dict[str, Any],
    field_sources: dict[str, dict[str, Any]],
    defaulted_fields: list[str],
) -> dict[str, Any]:
    """生成单行输入审计摘要。"""
    fields = []
    missing_in_excel: list[str] = []
    substituted: list[str] = []
    from_excel: list[str] = []

    for field, label in FIELD_LABELS.items():
        src = field_sources.get(field, {})
        value = device_data.get(field, src.get("value"))
        source = src.get("source", "missing")
        entry = {
            "field": field,
            "label": label,
            "value": value,
            "source": source,
            "source_label": src.get("source_label", SOURCE_LABELS.get(source, source)),
            "excel_column": src.get("excel_column"),
            "detail": src.get("detail"),
            "substituted": bool(src.get("substituted")),
            "default_available": src.get("default_used"),
            "in_defaulted_list": field in defaulted_fields,
        }
        fields.append(entry)
        if source in ("excel_column", "excel_multi_header", "excel_derived"):
            from_excel.append(field)
        elif source in ("batch_default", "equipment_config", "approximation"):
            substituted.append(field)
        elif source == "missing" or field in defaulted_fields:
            missing_in_excel.append(field)

    total_parts = {
        "chiller_power": device_data.get("chiller_power", 0),
        "chilled_pump_power": device_data.get("chilled_pump_power", 0),
        "cooling_pump_power": device_data.get("cooling_pump_power", 0),
        "cooling_tower_fan_power": device_data.get("cooling_tower_fan_power", 0),
        "terminal_fan_power": device_data.get("terminal_fan_power", 0),
    }
    return {
        "fields": fields,
        "from_excel": from_excel,
        "substituted_or_derived": substituted,
        "missing_in_excel": missing_in_excel,
        "defaulted_fields": defaulted_fields,
        "total_power_formula": "chiller + chilled_pump + cooling_pump + cooling_tower + terminal",
        "total_power_parts": total_parts,
        "total_power_sum": round(sum(total_parts.values()), 4),
    }


def build_pipeline_audit(
    *,
    physics_baseline_power: float,
    measured_baseline_power: float,
    measured_total: float,
    predicted_power: float,
    optimizer_saving_rate: float,
    saving_vs_measured: float,
    saving_vs_display_baseline: float,
    field_sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """寻优链路自检说明（已知限制与偏差原因）。"""
    issues: list[str] = []
    notes: list[str] = []

    if abs(physics_baseline_power - measured_total) / max(measured_total, 1) > 0.25:
        issues.append(
            f"物理模型基线({physics_baseline_power:.1f}kW)与Excel汇总({measured_total:.1f}kW)偏差>25%"
        )
        notes.append("表格「模型基线」已优先使用Excel实测加总；寻优器内部节能率仍用物理基线")

    if optimizer_saving_rate != saving_vs_display_baseline:
        notes.append(
            f"result.energy_saving_rate({optimizer_saving_rate}%)为寻优器物理基线节能率；"
            f"表格节能率({saving_vs_display_baseline}%)相对Excel对齐基线"
        )

    if field_sources.get("chilled_water_temp", {}).get("source") == "approximation":
        issues.append("冷水出水温度为「回水温度−5℃」近似，低负载时段可能偏差大")

    if field_sources.get("terminal_fan_power", {}).get("value", 0) == 0:
        notes.append("末端风机Excel为0，预测阶段会计入缺省2kW")

    if field_sources.get("indoor_temp", {}).get("source") == "batch_default":
        issues.append("缺少室内温度，舒适度约束可能不准")

    return {
        "issues": issues,
        "notes": notes,
        "power_comparison": {
            "excel_total": round(measured_total, 3),
            "display_baseline": round(measured_baseline_power, 3),
            "physics_baseline": round(physics_baseline_power, 3),
            "predicted_optimized": round(predicted_power, 3),
            "saving_vs_excel_baseline_pct": round(saving_vs_display_baseline, 2),
            "saving_vs_measured_pct": round(saving_vs_measured, 2),
            "optimizer_internal_saving_pct": round(optimizer_saving_rate, 2),
        },
    }
