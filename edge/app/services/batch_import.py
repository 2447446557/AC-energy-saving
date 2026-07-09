"""现场运行趋势文件批量导入解析

支持 Excel(.xls/.xlsx) 与 CSV。现场导出文件常见问题：
- 表头前有标题/空行；
- .xls 实际可能是 HTML 表格；
- 列名带单位、空格、括号；
- 只应对“运行状态=运行”的行做寻优。

本模块负责把这些不稳定输入统一转成 OptimizeRequest 所需的 DeviceData dict。
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from app.services.input_audit import build_input_audit, init_field_sources, mark_field


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("时间", "数据时间", "采集时间", "记录时间", "timestamp", "time"),
    "outdoor_temp": ("室外温度", "室外温度℃", "室外温度°C", "环境温度"),
    "outdoor_humidity": ("室外湿度", "室外湿度%", "环境湿度"),
    "indoor_temp": ("室内温度", "室内温度℃", "室内温度°C"),
    "indoor_humidity": ("室内湿度", "室内湿度%"),
    "indoor_load": ("室内负荷", "室内负荷kw", "室内负荷kW", "负荷"),
    "chiller_load": ("机组负载", "机组负荷", "机组负载%", "冷机负载"),
    "chiller_power": ("机组功率", "机组功率kw", "机组功率kW", "冷机功率"),
    "chilled_water_temp": ("冷水出水温度", "冷冻水出水温度", "冷冻水温度"),
    "cooling_water_temp": ("冷却水出水温度", "冷却水温度"),
    "chilled_pump_freq": ("当前冷冻泵频率", "冷冻泵频率", "冷冻泵频率hz"),
    "chilled_pump_power": ("当前冷冻泵功率", "冷冻泵功率"),
    "cooling_pump_freq": ("当前冷却泵频率", "冷却泵频率", "冷却泵频率hz"),
    "cooling_pump_power": ("当前冷却泵功率", "冷却泵功率"),
    "cooling_tower_fan_freq": ("冷却塔频率", "冷却塔风机频率", "冷却塔频率hz"),
    "cooling_tower_fan_power": ("冷却塔总功率", "冷却塔功率", "冷却塔风机功率"),
    "terminal_fan_power": ("末端风机功率", "末端风机功率kw"),
    "total_power": ("系统总功率", "总功率", "系统功率"),
}

_STATUS_ALIASES = ("运行状态", "寻优运行状态", "设备状态", "状态")
_SITE_DEFAULTS_FALLBACK = {
    "outdoor_temp": 30.0,
    "outdoor_humidity": 60.0,
    "indoor_temp": 27.0,
    "indoor_humidity": 55.0,
    "terminal_fan_power": 0.0,
}


def _site_defaults() -> dict[str, float]:
    try:
        from app.services.settings_config import settings_config_service

        return settings_config_service.get_batch_defaults()
    except Exception as e:
        logger.debug(f"读取批量缺省配置失败，使用内置默认: {e}")
        return dict(_SITE_DEFAULTS_FALLBACK)

_REQUIRED_FIELDS = (
    "outdoor_temp",
    "outdoor_humidity",
    "indoor_temp",
    "indoor_humidity",
    "indoor_load",
    "chiller_load",
    "chiller_power",
    "chilled_water_temp",
    "cooling_water_temp",
    "chilled_pump_freq",
    "chilled_pump_power",
    "cooling_pump_freq",
    "cooling_pump_power",
    "cooling_tower_fan_freq",
    "cooling_tower_fan_power",
    "terminal_fan_power",
    "total_power",
)


def _normalize(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u3000", "").replace("：", ":")
    return re.sub(r"[\s\(\)（）_%℃°ckwhz/\\-]+", "", text, flags=re.I).lower()


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else default
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "--", "-"}:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return default
    number = float(match.group())
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    """将 pandas/numpy/时间等值转成前端可 JSON 序列化的原始展示值。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _raw_row_dict(row: pd.Series) -> dict[str, Any]:
    """保留 Excel 原始列名和值，供前端查看寻优前所有原始数据。"""
    return {str(column): _json_safe(row.get(column)) for column in row.index}


def _to_timestamp(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return datetime.now().isoformat()
    text = str(value).strip()
    for fmt in ("%y/%m/%d %H:%M", "%y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    try:
        parsed = pd.to_datetime(value, dayfirst=False)
        if pd.isna(parsed):
            return datetime.now().isoformat()
        return parsed.to_pydatetime().isoformat()
    except Exception:
        return datetime.now().isoformat()


def _read_raw_table(content: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    buffer = BytesIO(content)
    if suffix == ".csv":
        for encoding in ("utf-8-sig", "gbk", "gb18030"):
            try:
                return pd.read_csv(BytesIO(content), header=None, encoding=encoding)
            except Exception:
                continue
        return pd.read_csv(buffer, header=None)

    try:
        return pd.read_excel(buffer, header=None)
    except Exception as excel_error:
        # 部分现场系统导出的 .xls 实际是 HTML table。
        try:
            tables = pd.read_html(BytesIO(content))
            if tables:
                return tables[0]
        except Exception:
            pass
        raise ValueError(f"无法解析 Excel/CSV 文件: {excel_error}") from excel_error


def _header_score(row: pd.Series) -> int:
    cells = {_normalize(cell) for cell in row.tolist()}
    score = 0
    for aliases in list(_FIELD_ALIASES.values()) + [_STATUS_ALIASES]:
        alias_norms = [_normalize(alias) for alias in aliases]
        if any(any(alias in cell or cell in alias for cell in cells) for alias in alias_norms):
            score += 1
    return score


def _promote_header(raw: pd.DataFrame) -> pd.DataFrame:
    best_idx = 0
    best_score = -1
    for idx in range(min(len(raw), 30)):
        score = _header_score(raw.iloc[idx])
        if score > best_score:
            best_idx, best_score = idx, score
    if best_score <= 0:
        raise ValueError("未识别到有效表头，请确认 Excel 包含室外温度/室内负荷等列")

    current_header = raw.iloc[best_idx]
    previous_header = raw.iloc[best_idx - 1] if best_idx > 0 else None
    groups: list[str] = []
    last_group = ""
    for i in range(len(current_header)):
        group = "" if previous_header is None else str(previous_header.iloc[i]).strip()
        if group and group.lower() != "nan":
            last_group = group
        groups.append(last_group)

    headers = []
    for i, value in enumerate(current_header):
        metric = str(value).strip()
        if not metric or metric.lower() == "nan":
            metric = f"col_{i}"
        group = groups[i]
        if group and _normalize(group) != _normalize(metric) and metric != f"col_{i}":
            headers.append(f"{group}__{metric}")
        else:
            headers.append(metric)
    df = raw.iloc[best_idx + 1 :].copy()
    df.columns = headers
    return df.dropna(how="all")


def _find_column(df: pd.DataFrame, aliases: tuple[str, ...]) -> str | None:
    normalized_columns = {column: _normalize(column) for column in df.columns}
    alias_norms = [_normalize(alias) for alias in aliases]
    for column, normalized in normalized_columns.items():
        if any(alias == normalized for alias in alias_norms):
            return column
    for column, normalized in normalized_columns.items():
        if any(alias in normalized or normalized in alias for alias in alias_norms):
            return column
    return None


def _columns_matching(df: pd.DataFrame, *parts: str) -> list[str]:
    normalized_parts = [_normalize(part) for part in parts]
    matched = []
    for column in df.columns:
        normalized = _normalize(column)
        if all(part in normalized for part in normalized_parts):
            matched.append(column)
    return matched


def _first_number(row: pd.Series, columns: list[str], default: float = 0.0) -> float:
    for column in columns:
        value = _to_float(row.get(column), 0.0)
        if value != 0:
            return value
    return default


def _sum_numbers(row: pd.Series, columns: list[str]) -> float:
    return sum(_to_float(row.get(column), 0.0) for column in columns)


def _avg_nonzero(row: pd.Series, columns: list[str], default: float = 0.0) -> float:
    values = [_to_float(row.get(column), 0.0) for column in columns]
    values = [value for value in values if value > 0]
    return sum(values) / len(values) if values else default


def _chiller_status_columns(df: pd.DataFrame) -> list[str]:
    return [
        column
        for column in _columns_matching(df, "约克离心机", "运行状态")
        if "2#约克离心机" not in str(column) or True
    ]


def _is_running_row(row: pd.Series, status_column: str | None, chiller_status_columns: list[str]) -> bool:
    chiller_statuses = [_normalize(row.get(column)) for column in chiller_status_columns]
    if chiller_statuses:
        return any("运行" in status for status in chiller_statuses)
    if status_column is None:
        return True
    return "运行" in _normalize(row.get(status_column))


def _active_chiller_prefix(row: pd.Series, df: pd.DataFrame) -> str | None:
    for prefix in ("1#约克离心机", "2#约克离心机"):
        cols = _columns_matching(df, prefix, "运行状态")
        if cols and "运行" in _normalize(row.get(cols[0])):
            return prefix
    return None


def _equipment_config():
    try:
        from app.services.equipment_config import equipment_config_service

        return equipment_config_service.get_config()
    except Exception:
        return None


def _excel_has_value(row: pd.Series, column_map: dict[str, str | None], field: str) -> bool:
    column = column_map.get(field)
    return column is not None and _to_float(row.get(column), 0.0) != 0.0


def _unit_prefix(column: str) -> str:
    text = str(column).strip()
    if "__" in text:
        return text.split("__", 1)[0].strip()
    return text


def _column_metric(column: str) -> str | None:
    text = str(column)
    if "电机功率百分比" in text or "功率百分比" in text:
        return "load"
    if "频率" in text:
        return "freq"
    if "功率" in text:
        return "power"
    if "电流" in text:
        return "current"
    return None


def _discover_chiller_prefixes(df: pd.DataFrame) -> list[str]:
    prefixes: list[str] = []
    seen: set[str] = set()
    for column in df.columns:
        col_str = str(column)
        prefix = _unit_prefix(col_str)
        if not _is_chiller_column(prefix, col_str) or prefix in seen:
            continue
        seen.add(prefix)
        prefixes.append(prefix)
    return sorted(prefixes)


def _chiller_load_columns(df: pd.DataFrame, prefix: str) -> list[str]:
    cols = _columns_matching(df, prefix, "电机功率百分比")
    if not cols:
        cols = _columns_matching(df, prefix, "功率百分比")
    return cols


def _chiller_power_columns(df: pd.DataFrame, prefix: str) -> list[str]:
    return [
        column
        for column in _columns_matching(df, prefix, "功率")
        if "百分比" not in str(column)
    ]


def _is_chiller_running(row: pd.Series, df: pd.DataFrame, prefix: str) -> bool:
    status_cols = _columns_matching(df, prefix, "运行状态")
    if status_cols:
        return "运行" in _normalize(row.get(status_cols[0]))
    load = _first_number(row, _chiller_load_columns(df, prefix))
    power = _first_number(row, _chiller_power_columns(df, prefix))
    return load > 0 or power > 0


def _match_chiller_cfg(prefix: str, cfg_units: list[Any], eq: Any | None) -> Any | None:
    for unit in cfg_units:
        if unit.name == prefix or prefix in unit.name or unit.name in prefix:
            return unit
    if eq is not None and hasattr(eq, "chiller"):
        return eq.chiller
    return None


def extract_chillers_from_row(
    row: pd.Series,
    df: pd.DataFrame,
    eq: Any | None = None,
) -> list[dict[str, Any]]:
    """按运行状态与多级表头提取全部运行中的冷水机组。"""
    eq = eq or _equipment_config()
    cfg_units = _chiller_cfg_units(eq)
    chillers: list[dict[str, Any]] = []

    for prefix in _discover_chiller_prefixes(df):
        if not _is_chiller_running(row, df, prefix):
            continue
        load = _first_number(row, _chiller_load_columns(df, prefix))
        power = _first_number(row, _chiller_power_columns(df, prefix))
        cfg_unit = _match_chiller_cfg(prefix, cfg_units, eq)
        if _is_missing_scalar(power) and load > 0 and cfg_unit is not None:
            power = _estimate_chiller_power(load, cfg_unit)
        chillers.append(
            {
                "name": prefix,
                "label": prefix,
                "load": round(float(load), 3),
                "power": round(float(power or 0.0), 3),
            }
        )
    return chillers


def _is_chiller_column(prefix: str, col_str: str) -> bool:
    text = f"{prefix} {col_str}"
    return "约克离心机" in text or "冷水机组" in text or ("离心机" in text and "冷却塔" not in text)


def extract_equipment_units(row: pd.Series, df: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """从多级表头 Excel 行提取逐台冷水机组/冷冻泵/冷却泵/冷却塔读数。"""
    buckets: dict[str, list[dict[str, Any]]] = {
        "chillers": [],
        "chilled_pumps": [],
        "cooling_pumps": [],
        "cooling_towers": [],
    }
    kind_map = {
        "冷冻泵": "chilled_pumps",
        "冷却泵": "cooling_pumps",
        "冷却塔": "cooling_towers",
    }
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for column in df.columns:
        col_str = str(column)
        prefix = _unit_prefix(col_str)
        if _is_chiller_column(prefix, col_str):
            continue
        matched_kind = None
        for keyword, kind in kind_map.items():
            if keyword in prefix or keyword in col_str:
                matched_kind = kind
                break
        if matched_kind is None:
            continue
        metric = _column_metric(col_str)
        if metric is None:
            continue
        key = (matched_kind, prefix)
        item = grouped.setdefault(
            key,
            {"name": prefix, "label": prefix, "freq": 0.0, "power": 0.0, "current": 0.0},
        )
        value = _to_float(row.get(column))
        if metric in ("freq", "load") or value > 0:
            item[metric] = value

    for (kind, _), item in grouped.items():
        if item.get("freq", 0.0) > 0 or item.get("power", 0.0) > 0 or item.get("current", 0.0) > 0:
            buckets[kind].append(
                {
                    "name": item["name"],
                    "label": item["label"],
                    "freq": round(float(item.get("freq", 0.0)), 3),
                    "power": round(float(item.get("power", 0.0)), 3),
                    "current": round(float(item.get("current", 0.0)), 3),
                }
            )

    buckets["chillers"] = extract_chillers_from_row(row, df)

    for kind in buckets:
        buckets[kind].sort(key=lambda x: x["name"])
    return buckets


def _chiller_cfg_units(eq: Any | None) -> list[Any]:
    try:
        from app.services.equipment_config import equipment_config_service

        return [
            unit
            for unit in equipment_config_service.get_units()
            if unit.unit_type == "chiller" and unit.enabled
        ]
    except Exception:
        return []


def _estimate_chiller_power(load_pct: float, cfg: Any) -> float:
    thermal_kw = float(cfg.rated_capacity_kw or 0.0) * float(cfg.max_load_rate or 0.8) * load_pct / 100.0
    cop = max(float(cfg.rated_cop or 5.5), 2.0)
    return round(thermal_kw / cop, 3) if thermal_kw > 0 else 0.0


def build_equipment_units_from_config(
    device_data: dict[str, Any],
    eq: Any | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """无逐台 Excel 列时，按设备配置生成默认逐台输入结构。"""
    eq = eq or _equipment_config()
    chilled_labels = ["东", "西", "3", "4"]
    cooling_labels = ["东", "西", "3", "4"]
    units: dict[str, list[dict[str, Any]]] = {
        "chillers": [],
        "chilled_pumps": [],
        "cooling_pumps": [],
        "cooling_towers": [],
    }
    if eq is None:
        return units

    chiller_load = float(device_data.get("chiller_load") or 0.0)
    chiller_power = float(device_data.get("chiller_power") or 0.0)
    chiller_cfg_units = _chiller_cfg_units(eq)
    if chiller_cfg_units:
        per_power = chiller_power / len(chiller_cfg_units) if chiller_cfg_units else chiller_power
        for unit in chiller_cfg_units:
            power = per_power
            if _is_missing_scalar(power) and chiller_load > 0:
                power = _estimate_chiller_power(chiller_load, unit)
            units["chillers"].append(
                {
                    "name": unit.name,
                    "label": unit.name,
                    "load": round(chiller_load, 3),
                    "power": round(float(power or 0.0), 3),
                }
            )
    else:
        chiller_count = max(int(eq.chiller.count), 0)
        per_power = chiller_power / chiller_count if chiller_count else chiller_power
        for index in range(chiller_count):
            name = eq.chiller.name if chiller_count == 1 else f"{eq.chiller.name}_{index + 1}"
            power = per_power
            if _is_missing_scalar(power) and chiller_load > 0:
                power = _estimate_chiller_power(chiller_load, eq.chiller)
            units["chillers"].append(
                {
                    "name": name,
                    "label": name,
                    "load": round(chiller_load, 3),
                    "power": round(float(power or 0.0), 3),
                }
            )

    chilled_count = max(int(eq.chilled_pump.count), 0)
    chilled_freq = float(device_data.get("chilled_pump_freq") or 0.0)
    chilled_power = float(device_data.get("chilled_pump_power") or 0.0)
    per_chilled_power = chilled_power / chilled_count if chilled_count else chilled_power
    for index in range(chilled_count):
        suffix = chilled_labels[index] if index < len(chilled_labels) else str(index + 1)
        units["chilled_pumps"].append(
            {
                "name": f"{eq.chilled_pump.name}_{suffix}",
                "label": f"{eq.chilled_pump.name}_{suffix}",
                "freq": round(chilled_freq, 3),
                "power": round(per_chilled_power, 3),
                "current": 0.0,
            }
        )

    cooling_count = max(int(eq.cooling_pump.count), 0)
    cooling_freq = float(device_data.get("cooling_pump_freq") or 0.0)
    cooling_power = float(device_data.get("cooling_pump_power") or 0.0)
    per_cooling_power = cooling_power / cooling_count if cooling_count else cooling_power
    for index in range(cooling_count):
        suffix = cooling_labels[index] if index < len(cooling_labels) else str(index + 1)
        units["cooling_pumps"].append(
            {
                "name": f"{eq.cooling_pump.name}_{suffix}",
                "label": f"{eq.cooling_pump.name}_{suffix}",
                "freq": round(cooling_freq, 3),
                "power": round(per_cooling_power, 3),
                "current": 0.0,
            }
        )

    tower_power_total = float(device_data.get("cooling_tower_fan_power") or 0.0)
    tower_freq = float(device_data.get("cooling_tower_fan_freq") or 0.0)
    enabled_towers = [tower for tower in eq.cooling_towers if tower.enabled]
    for tower in enabled_towers:
        units["cooling_towers"].append(
            {
                "name": tower.name,
                "label": tower.name,
                "freq": round(tower.fixed_freq if tower_freq <= 0 else tower_freq, 3),
                "power": round(tower.motor_power_kw, 3),
                "current": 0.0,
            }
        )
    if tower_power_total > 0 and enabled_towers:
        running_power = tower_power_total / len(enabled_towers)
        for item in units["cooling_towers"]:
            item["power"] = round(running_power, 3)
    return units


def resolve_equipment_units(
    row: pd.Series,
    df: pd.DataFrame,
    device_data: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """优先使用 Excel 逐台列，缺省类型按配置与汇总值回填。"""
    units = extract_equipment_units(row, df)
    fallback = build_equipment_units_from_config(device_data, _equipment_config())
    for key in ("chillers", "chilled_pumps", "cooling_pumps", "cooling_towers"):
        if not units.get(key):
            units[key] = fallback.get(key) or []
    return units


def _is_missing_scalar(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(number) or abs(number) < 1e-9


def _pump_power_at_freq(motor_kw: float, freq: float) -> float:
    ratio = max(float(freq), 0.0) / 50.0
    return float(motor_kw) * (ratio**3)


def get_manual_input_config_defaults() -> dict[str, Any]:
    """供手动寻优输入框使用的配置缺省值（settings + equipment + 约束）。"""
    site = _site_defaults()
    defaults: dict[str, Any] = dict(site)
    try:
        from app.algorithms.constraints import SafetyConstraints
        from app.services.settings_config import settings_config_service

        app_settings = settings_config_service.get_app_settings()
        constraints = SafetyConstraints()
        chw_lo, chw_hi = constraints.bounds["chilled_water_temp"]
        defaults["chilled_water_temp"] = round((chw_lo + chw_hi) / 2.0, 2)
        defaults["cooling_water_temp"] = 32.0
        if not defaults.get("terminal_fan_power"):
            defaults["terminal_fan_power"] = app_settings.energy_model.terminal_fan_default
    except Exception:
        defaults.setdefault("chilled_water_temp", 8.0)
        defaults.setdefault("cooling_water_temp", 32.0)
        defaults.setdefault("terminal_fan_power", 2.0)

    eq = _equipment_config()
    if eq is not None:
        load_pct = max(10.0, min(100.0, eq.chiller.max_load_rate * 100.0 * 0.8))
        thermal_kw = (
            eq.chiller.rated_capacity_kw * eq.chiller.max_load_rate * load_pct / 100.0
        )
        cop = max(eq.chiller.rated_cop, 2.0)
        defaults["chiller_load"] = round(load_pct, 2)
        defaults["chiller_power"] = round(thermal_kw / cop, 3)
        defaults["indoor_load"] = round(thermal_kw, 3)
        defaults["chilled_pump_freq"] = eq.chilled_pump.min_freq
        defaults["cooling_pump_freq"] = eq.cooling_pump.min_freq
        defaults["chilled_pump_power"] = round(
            eq.chilled_pump.count
            * _pump_power_at_freq(eq.chilled_pump.motor_power_kw, eq.chilled_pump.min_freq),
            3,
        )
        defaults["cooling_pump_power"] = round(
            eq.cooling_pump.count
            * _pump_power_at_freq(eq.cooling_pump.motor_power_kw, eq.cooling_pump.min_freq),
            3,
        )
        enabled_towers = [tower for tower in eq.cooling_towers if tower.enabled]
        defaults["cooling_tower_fan_freq"] = (
            enabled_towers[0].fixed_freq if enabled_towers else 50.0
        )
        defaults["cooling_tower_fan_power"] = round(
            sum(tower.motor_power_kw for tower in enabled_towers),
            3,
        )
        defaults["total_power"] = round(
            defaults["chiller_power"]
            + defaults["chilled_pump_power"]
            + defaults["cooling_pump_power"]
            + defaults["cooling_tower_fan_power"]
            + float(defaults.get("terminal_fan_power") or 0.0),
            3,
        )
    return defaults


def _enrich_equipment_units_from_config(
    equipment_units: dict[str, list[dict[str, Any]]] | None,
    device_data: dict[str, Any],
    eq: Any | None,
) -> dict[str, list[dict[str, Any]]]:
    """Excel 逐台缺值时，用设备配置额定参数填充频率/功率。"""
    units = equipment_units or {}
    if eq is None:
        return build_equipment_units_from_config(device_data, eq)
    if not any(units.get(key) for key in ("chillers", "chilled_pumps", "cooling_pumps", "cooling_towers")):
        units = build_equipment_units_from_config(device_data, eq)

    def fill_pump_units(items: list[dict[str, Any]], pump_cfg: Any, fallback_freq: float) -> list[dict[str, Any]]:
        filled = []
        for item in items:
            freq = item.get("freq") or fallback_freq or pump_cfg.min_freq
            power = item.get("power")
            if _is_missing_scalar(power):
                power = _pump_power_at_freq(pump_cfg.motor_power_kw, freq)
            if _is_missing_scalar(freq):
                freq = pump_cfg.min_freq
            filled.append(
                {
                    **item,
                    "freq": round(float(freq), 3),
                    "power": round(float(power), 3),
                    "current": round(float(item.get("current") or 0.0), 3),
                }
            )
        return filled

    chilled = fill_pump_units(
        units.get("chilled_pumps") or [],
        eq.chilled_pump,
        float(device_data.get("chilled_pump_freq") or eq.chilled_pump.min_freq),
    )
    cooling = fill_pump_units(
        units.get("cooling_pumps") or [],
        eq.cooling_pump,
        float(device_data.get("cooling_pump_freq") or eq.cooling_pump.min_freq),
    )
    towers = []
    enabled = [tower for tower in eq.cooling_towers if tower.enabled]
    tower_items = units.get("cooling_towers") or []
    if not tower_items:
        tower_items = [
            {"name": tower.name, "label": tower.name, "freq": tower.fixed_freq, "power": 0.0, "current": 0.0}
            for tower in enabled
        ]
    for index, item in enumerate(tower_items):
        tower_cfg = enabled[index] if index < len(enabled) else None
        freq = item.get("freq") or (tower_cfg.fixed_freq if tower_cfg else 50.0)
        power = item.get("power")
        if _is_missing_scalar(power) and tower_cfg is not None:
            power = tower_cfg.motor_power_kw
        towers.append(
            {
                **item,
                "name": item.get("name") or (tower_cfg.name if tower_cfg else f"冷却塔{index + 1}"),
                "label": item.get("label") or item.get("name") or (tower_cfg.name if tower_cfg else f"冷却塔{index + 1}"),
                "freq": round(float(freq), 3),
                "power": round(float(power or 0.0), 3),
                "current": round(float(item.get("current") or 0.0), 3),
            }
        )
    chiller_cfg_units = _chiller_cfg_units(eq)
    chiller_items = units.get("chillers") or []
    if not chiller_items:
        chiller_items = [
            {"name": unit.name, "label": unit.name, "load": 0.0, "power": 0.0}
            for unit in chiller_cfg_units
        ] or [
            {
                "name": eq.chiller.name,
                "label": eq.chiller.name,
                "load": float(device_data.get("chiller_load") or 0.0),
                "power": 0.0,
            }
        ]
    chillers = []
    for index, item in enumerate(chiller_items):
        cfg_unit = chiller_cfg_units[index] if index < len(chiller_cfg_units) else eq.chiller
        load = item.get("load")
        if _is_missing_scalar(load):
            load = float(device_data.get("chiller_load") or 0.0)
        power = item.get("power")
        if _is_missing_scalar(power) and float(load or 0.0) > 0 and cfg_unit is not None:
            power = _estimate_chiller_power(float(load), cfg_unit)
        chillers.append(
            {
                **item,
                "name": item.get("name") or (cfg_unit.name if cfg_unit else f"冷水机组{index + 1}"),
                "label": item.get("label") or item.get("name") or (cfg_unit.name if cfg_unit else f"冷水机组{index + 1}"),
                "load": round(float(load or 0.0), 3),
                "power": round(float(power or 0.0), 3),
            }
        )
    return {
        "chillers": chillers,
        "chilled_pumps": chilled,
        "cooling_pumps": cooling,
        "cooling_towers": towers,
    }


def _align_units_to_device_scalars(
    device_data: dict[str, Any],
    equipment_units: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """逐台功率与汇总字段不一致时，按汇总字段均分到各台（避免错误逐台列覆盖正确汇总）。"""
    units = {
        "chillers": [dict(item) for item in (equipment_units.get("chillers") or [])],
        "chilled_pumps": [dict(item) for item in (equipment_units.get("chilled_pumps") or [])],
        "cooling_pumps": [dict(item) for item in (equipment_units.get("cooling_pumps") or [])],
        "cooling_towers": [dict(item) for item in (equipment_units.get("cooling_towers") or [])],
    }
    groups = (
        ("chillers", "chiller_power", None),
        ("chilled_pumps", "chilled_pump_power", "chilled_pump_freq"),
        ("cooling_pumps", "cooling_pump_power", "cooling_pump_freq"),
        ("cooling_towers", "cooling_tower_fan_power", "cooling_tower_fan_freq"),
    )
    for unit_key, total_key, freq_key in groups:
        items = units[unit_key]
        target = float(device_data.get(total_key) or 0.0)
        if target <= 0 or not items:
            continue
        current = sum(float(item.get("power") or 0.0) for item in items)
        fallback_freq = float(device_data.get(freq_key) or 0.0) if freq_key else 0.0
        if current <= 0 or abs(current - target) / max(target, 1e-6) > 0.05:
            each = target / len(items)
            for item in items:
                item["power"] = round(each, 3)
                if freq_key and float(item.get("freq") or 0.0) <= 0 and fallback_freq > 0:
                    item["freq"] = round(fallback_freq, 3)
    chiller_load = float(device_data.get("chiller_load") or 0.0)
    if chiller_load > 0 and units["chillers"]:
        loads = [float(item.get("load") or 0.0) for item in units["chillers"]]
        if not loads or sum(loads) <= 0:
            for item in units["chillers"]:
                item["load"] = round(chiller_load, 3)
    return units


def _sync_device_data_from_units(
    device_data: dict[str, Any],
    equipment_units: dict[str, list[dict[str, Any]]],
) -> None:
    """将逐台设备汇总写回寻优输入标量字段。"""
    chillers = equipment_units.get("chillers") or []
    chilled = equipment_units.get("chilled_pumps") or []
    cooling = equipment_units.get("cooling_pumps") or []
    towers = equipment_units.get("cooling_towers") or []
    chiller_loads = [float(item.get("load") or 0.0) for item in chillers if float(item.get("load") or 0.0) > 0]
    if chiller_loads:
        device_data["chiller_load"] = round(sum(chiller_loads) / len(chiller_loads), 3)
    device_data["chiller_power"] = round(
        sum(float(item.get("power") or 0.0) for item in chillers),
        3,
    )
    chilled_freqs = [float(item.get("freq") or 0.0) for item in chilled if float(item.get("freq") or 0.0) > 0]
    cooling_freqs = [float(item.get("freq") or 0.0) for item in cooling if float(item.get("freq") or 0.0) > 0]
    tower_freqs = [float(item.get("freq") or 0.0) for item in towers if float(item.get("freq") or 0.0) > 0]
    if chilled_freqs:
        device_data["chilled_pump_freq"] = round(sum(chilled_freqs) / len(chilled_freqs), 3)
    if cooling_freqs:
        device_data["cooling_pump_freq"] = round(sum(cooling_freqs) / len(cooling_freqs), 3)
    if tower_freqs:
        device_data["cooling_tower_fan_freq"] = round(sum(tower_freqs) / len(tower_freqs), 3)
    device_data["chilled_pump_power"] = round(
        sum(float(item.get("power") or 0.0) for item in chilled),
        3,
    )
    device_data["cooling_pump_power"] = round(
        sum(float(item.get("power") or 0.0) for item in cooling),
        3,
    )
    device_data["cooling_tower_fan_power"] = round(
        sum(float(item.get("power") or 0.0) for item in towers),
        3,
    )


def apply_manual_input_config_defaults(
    device_data: dict[str, Any],
    equipment_units: dict[str, list[dict[str, Any]]] | None = None,
    field_sources: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[str]]:
    """Excel/手工输入缺值时，用系统配置项补齐并重新汇总总功率。"""
    config_defaults = get_manual_input_config_defaults()
    eq = _equipment_config()
    filled_fields: list[str] = []

    scalar_fields = (
        "outdoor_temp",
        "outdoor_humidity",
        "indoor_temp",
        "indoor_humidity",
        "indoor_load",
        "chiller_load",
        "chiller_power",
        "chilled_water_temp",
        "cooling_water_temp",
        "terminal_fan_power",
    )
    for field in scalar_fields:
        if _is_missing_scalar(device_data.get(field)):
            device_data[field] = config_defaults.get(field, device_data.get(field, 0.0))
            filled_fields.append(field)
            if field_sources is not None:
                mark_field(
                    field_sources,
                    field,
                    device_data[field],
                    "config_default",
                    detail="Excel 缺值，使用系统/设备配置缺省",
                    substituted=True,
                )

    if eq is not None and not _is_missing_scalar(device_data.get("chiller_load")):
        if _is_missing_scalar(device_data.get("chiller_power")):
            load_pct = float(device_data["chiller_load"])
            thermal_kw = eq.chiller.rated_capacity_kw * eq.chiller.max_load_rate * load_pct / 100.0
            cop = max(eq.chiller.rated_cop, 2.0)
            device_data["chiller_power"] = round(thermal_kw / cop, 3)
            filled_fields.append("chiller_power")
        if _is_missing_scalar(device_data.get("indoor_load")):
            load_pct = float(device_data["chiller_load"])
            device_data["indoor_load"] = round(
                eq.chiller.rated_capacity_kw * eq.chiller.max_load_rate * load_pct / 100.0,
                3,
            )
            filled_fields.append("indoor_load")

    enriched_units = _enrich_equipment_units_from_config(equipment_units, device_data, eq)
    enriched_units = _align_units_to_device_scalars(device_data, enriched_units)
    _sync_device_data_from_units(device_data, enriched_units)

    for field in ("chilled_pump_freq", "chilled_pump_power", "cooling_pump_freq", "cooling_pump_power", "cooling_tower_fan_freq", "cooling_tower_fan_power"):
        if _is_missing_scalar(device_data.get(field)):
            device_data[field] = config_defaults.get(field, 0.0)
            filled_fields.append(field)

    device_data["total_power"] = round(
        float(device_data.get("chiller_power") or 0.0)
        + float(device_data.get("chilled_pump_power") or 0.0)
        + float(device_data.get("cooling_pump_power") or 0.0)
        + float(device_data.get("cooling_tower_fan_power") or 0.0)
        + float(device_data.get("terminal_fan_power") or 0.0),
        3,
    )
    if field_sources is not None:
        mark_field(
            field_sources,
            "total_power",
            device_data["total_power"],
            "computed",
            detail="机组+冷冻泵+冷却泵+冷却塔+末端（缺省补齐后重算）",
        )
    return device_data, enriched_units, sorted(set(filled_fields))


def parse_runtime_file_last_row(content: bytes, filename: str) -> dict[str, Any]:
    """解析上传文件，并返回最后一条「运行」行的工况（供手动寻优输入框填充）。"""
    parsed = parse_runtime_file(content, filename)
    rows = parsed.get("rows") or []
    if not rows:
        return {
            **parsed,
            "selected_row": None,
            "config_defaults": get_manual_input_config_defaults(),
            "message": "未找到运行状态=运行的有效数据行",
        }
    selected = dict(rows[-1])
    device_data, equipment_units, config_filled = apply_manual_input_config_defaults(
        dict(selected.get("device_data") or {}),
        selected.get("equipment_units"),
        selected.get("field_sources"),
    )
    selected["device_data"] = device_data
    selected["equipment_units"] = equipment_units
    defaulted_fields = sorted(set(selected.get("defaulted_fields") or []) | set(config_filled))
    selected["defaulted_fields"] = defaulted_fields
    selected["config_filled_fields"] = config_filled
    return {
        **parsed,
        "selected_row": selected,
        "selected_row_number": selected.get("row_number"),
        "config_defaults": get_manual_input_config_defaults(),
        "message": f"已选取第 {selected.get('row_number')} 行（最后一条运行数据）",
    }


def _derive_site_fields(
    row: pd.Series,
    df: pd.DataFrame,
    device_data: dict[str, Any],
    column_map: dict[str, str | None],
    field_sources: dict[str, dict[str, Any]],
) -> list[str]:
    """针对现场“约克离心机/冷冻泵/冷却泵/冷却塔”多级表头做派生映射。"""
    defaulted: list[str] = []
    eq = _equipment_config()
    chiller_prefix = _active_chiller_prefix(row, df) or "1#约克离心机"

    # 制冷机房室外温湿度：多级表头为 制冷机房室外温湿度__湿度(%) / __温度(℃)
    outdoor_humidity = _first_number(
        row,
        _columns_matching(df, "制冷机房室外温湿度", "湿度")
        or _columns_matching(df, "室外温湿度", "湿度")
        or _columns_matching(df, "室外", "湿度"),
    )
    outdoor_temp = _first_number(
        row,
        _columns_matching(df, "制冷机房室外温湿度", "温度")
        or _columns_matching(df, "室外温湿度", "温度")
        or _columns_matching(df, "室外", "温度"),
    )
    if outdoor_humidity:
        device_data["outdoor_humidity"] = outdoor_humidity
        cols = _columns_matching(df, "制冷机房室外温湿度", "湿度") or _columns_matching(
            df, "室外", "湿度"
        )
        mark_field(
            field_sources,
            "outdoor_humidity",
            outdoor_humidity,
            "excel_multi_header",
            excel_column=cols[0] if cols else None,
            detail="制冷机房室外温湿度/湿度列",
            substituted=_excel_has_value(row, column_map, "outdoor_humidity") is False,
        )
    if outdoor_temp:
        device_data["outdoor_temp"] = outdoor_temp
        cols = _columns_matching(df, "制冷机房室外温湿度", "温度") or _columns_matching(
            df, "室外", "温度"
        )
        mark_field(
            field_sources,
            "outdoor_temp",
            outdoor_temp,
            "excel_multi_header",
            excel_column=cols[0] if cols else None,
            detail="制冷机房室外温湿度/温度列",
            substituted=_excel_has_value(row, column_map, "outdoor_temp") is False,
        )

    indoor_humidity = _first_number(
        row,
        _columns_matching(df, "制冷机房室内温湿度", "湿度")
        or _columns_matching(df, "室内温湿度", "湿度")
        or _columns_matching(df, "室内", "湿度"),
    )
    indoor_temp = _first_number(
        row,
        _columns_matching(df, "制冷机房室内温湿度", "温度")
        or _columns_matching(df, "室内温湿度", "温度")
        or _columns_matching(df, "室内", "温度"),
    )
    if indoor_humidity and not _excel_has_value(row, column_map, "indoor_humidity"):
        device_data["indoor_humidity"] = indoor_humidity
        cols = _columns_matching(df, "制冷机房室内温湿度", "湿度") or _columns_matching(
            df, "室内", "湿度"
        )
        mark_field(
            field_sources,
            "indoor_humidity",
            indoor_humidity,
            "excel_multi_header",
            excel_column=cols[0] if cols else None,
            detail="制冷机房室内温湿度/湿度列",
            substituted=True,
        )
    if indoor_temp and not _excel_has_value(row, column_map, "indoor_temp"):
        device_data["indoor_temp"] = indoor_temp
        cols = _columns_matching(df, "制冷机房室内温湿度", "温度") or _columns_matching(
            df, "室内", "温度"
        )
        mark_field(
            field_sources,
            "indoor_temp",
            indoor_temp,
            "excel_multi_header",
            excel_column=cols[0] if cols else None,
            detail="制冷机房室内温湿度/温度列",
            substituted=True,
        )

    chiller_loads: list[float] = []
    chiller_powers: list[float] = []
    for prefix in _discover_chiller_prefixes(df):
        if not _is_chiller_running(row, df, prefix):
            continue
        unit_load = _first_number(row, _chiller_load_columns(df, prefix))
        unit_power = _first_number(row, _chiller_power_columns(df, prefix))
        if unit_load > 0:
            chiller_loads.append(unit_load)
        cfg_unit = _match_chiller_cfg(prefix, _chiller_cfg_units(eq), eq)
        if _is_missing_scalar(unit_power) and unit_load > 0 and cfg_unit is not None:
            unit_power = _estimate_chiller_power(unit_load, cfg_unit)
        if unit_power > 0:
            chiller_powers.append(unit_power)

    chiller_load = sum(chiller_loads) / len(chiller_loads) if chiller_loads else 0.0
    chiller_prefix = _active_chiller_prefix(row, df) or "1#约克离心机"
    if chiller_load == 0:
        chiller_load = _first_number(row, _chiller_load_columns(df, chiller_prefix))
    if chiller_load == 0:
        chiller_load = max(
            _first_number(row, _chiller_load_columns(df, "1#约克离心机")),
            _first_number(row, _chiller_load_columns(df, "2#约克离心机")),
        )
    if chiller_load:
        device_data["chiller_load"] = chiller_load
        load_cols = _chiller_load_columns(df, chiller_prefix) or _chiller_load_columns(
            df, "1#约克离心机"
        )
        mark_field(
            field_sources,
            "chiller_load",
            chiller_load,
            "excel_multi_header",
            excel_column=load_cols[0] if load_cols else None,
            detail="运行中冷水机组负载%（多台取平均）",
        )
        if not _excel_has_value(row, column_map, "chiller_power"):
            if chiller_powers:
                device_data["chiller_power"] = round(sum(chiller_powers), 3)
                mark_field(
                    field_sources,
                    "chiller_power",
                    device_data["chiller_power"],
                    "excel_derived",
                    detail="各台冷水机组电功率合计",
                    substituted=True,
                )
            elif eq:
                thermal_kw = (
                    eq.chiller.rated_capacity_kw
                    * eq.chiller.max_load_rate
                    * chiller_load
                    / 100.0
                )
                cop = max(eq.chiller.rated_cop, 2.0)
                device_data["chiller_power"] = thermal_kw / cop
                mark_field(
                    field_sources,
                    "chiller_power",
                    device_data["chiller_power"],
                    "equipment_config",
                    detail=(
                        f"热负荷=额定制冷量×负荷上限×负载%={thermal_kw:.2f}kW，"
                        f"÷COP({cop})"
                    ),
                    substituted=True,
                )
            else:
                device_data["chiller_power"] = 516.2 * 0.8 * chiller_load / 100.0 / 5.5
                mark_field(
                    field_sources,
                    "chiller_power",
                    device_data["chiller_power"],
                    "equipment_config",
                    detail="默认设备参数推算",
                    substituted=True,
                )

    evaporating_temp = _first_number(row, _columns_matching(df, chiller_prefix, "蒸发温度"))
    condensing_temp = _first_number(row, _columns_matching(df, chiller_prefix, "冷凝温度"))
    return_water_temp = _first_number(
        row,
        _columns_matching(df, "冷水总回水温度")
        or _columns_matching(df, "冷冻水总回水温度")
        or _columns_matching(df, "回水温度"),
    )
    if not _excel_has_value(row, column_map, "chilled_water_temp"):
        if return_water_temp:
            device_data["chilled_water_temp"] = max(return_water_temp - 5.0, 5.0)
            rw_cols = _columns_matching(df, "冷水总回水温度") or _columns_matching(
                df, "回水温度"
            )
            mark_field(
                field_sources,
                "chilled_water_temp",
                device_data["chilled_water_temp"],
                "approximation",
                excel_column=rw_cols[0] if rw_cols else None,
                detail=f"回水温度{return_water_temp}℃ − 5℃（工程近似）",
                substituted=True,
            )
        elif evaporating_temp:
            device_data["chilled_water_temp"] = evaporating_temp
            evap_cols = _columns_matching(df, chiller_prefix, "蒸发温度")
            mark_field(
                field_sources,
                "chilled_water_temp",
                evaporating_temp,
                "approximation",
                excel_column=evap_cols[0] if evap_cols else None,
                detail="无出水/回水列，暂用蒸发温度近似",
                substituted=True,
            )
    if not _excel_has_value(row, column_map, "cooling_water_temp") and condensing_temp:
        device_data["cooling_water_temp"] = condensing_temp
        cond_cols = _columns_matching(df, chiller_prefix, "冷凝温度")
        mark_field(
            field_sources,
            "cooling_water_temp",
            condensing_temp,
            "approximation",
            excel_column=cond_cols[0] if cond_cols else None,
            detail="无冷却水出水列，暂用冷凝温度近似",
            substituted=True,
        )

    chilled_freq = _avg_nonzero(row, _columns_matching(df, "冷冻泵", "频率"))
    chilled_power = _sum_numbers(row, _columns_matching(df, "冷冻泵", "功率"))
    cooling_freq = _avg_nonzero(row, _columns_matching(df, "冷却泵", "频率"))
    cooling_power = _sum_numbers(row, _columns_matching(df, "冷却泵", "功率"))
    tower_power = _sum_numbers(row, _columns_matching(df, "冷却塔", "功率"))

    if chilled_freq and not _excel_has_value(row, column_map, "chilled_pump_freq"):
        device_data["chilled_pump_freq"] = chilled_freq
        mark_field(
            field_sources,
            "chilled_pump_freq",
            chilled_freq,
            "excel_derived",
            detail="冷冻泵频率列非零平均",
        )
    if chilled_power and not _excel_has_value(row, column_map, "chilled_pump_power"):
        device_data["chilled_pump_power"] = chilled_power
        mark_field(
            field_sources,
            "chilled_pump_power",
            chilled_power,
            "excel_derived",
            detail="冷冻泵功率列求和",
        )
    if cooling_freq and not _excel_has_value(row, column_map, "cooling_pump_freq"):
        device_data["cooling_pump_freq"] = cooling_freq
        mark_field(
            field_sources,
            "cooling_pump_freq",
            cooling_freq,
            "excel_derived",
            detail="冷却泵频率列非零平均",
        )
    if cooling_power and not _excel_has_value(row, column_map, "cooling_pump_power"):
        device_data["cooling_pump_power"] = cooling_power
        mark_field(
            field_sources,
            "cooling_pump_power",
            cooling_power,
            "excel_derived",
            detail="冷却泵功率列求和",
        )
    if tower_power and not _excel_has_value(row, column_map, "cooling_tower_fan_power"):
        device_data["cooling_tower_fan_power"] = tower_power
        mark_field(
            field_sources,
            "cooling_tower_fan_power",
            tower_power,
            "excel_derived",
            detail="冷却塔功率列求和",
        )

    if not _excel_has_value(row, column_map, "cooling_tower_fan_freq"):
        fixed_tower_freq = 50.0
        if eq:
            enabled = [tower for tower in eq.cooling_towers if tower.enabled]
            if enabled:
                fixed_tower_freq = enabled[0].fixed_freq
        device_data["cooling_tower_fan_freq"] = fixed_tower_freq
        mark_field(
            field_sources,
            "cooling_tower_fan_freq",
            fixed_tower_freq,
            "equipment_config",
            detail="Excel无塔频率列，用设备配置定频",
            substituted=True,
        )

    if (
        device_data.get("indoor_load", 0.0) == 0
        and not _excel_has_value(row, column_map, "indoor_load")
        and chiller_load
        and eq
    ):
        device_data["indoor_load"] = (
            eq.chiller.rated_capacity_kw * eq.chiller.max_load_rate * chiller_load / 100.0
        )
        mark_field(
            field_sources,
            "indoor_load",
            device_data["indoor_load"],
            "equipment_config",
            detail="额定制冷量×负荷上限×机组负载%",
            substituted=True,
        )

    device_data["total_power"] = (
        device_data.get("chiller_power", 0.0)
        + device_data.get("chilled_pump_power", 0.0)
        + device_data.get("cooling_pump_power", 0.0)
        + device_data.get("cooling_tower_fan_power", 0.0)
        + device_data.get("terminal_fan_power", 0.0)
    )
    mark_field(
        field_sources,
        "total_power",
        device_data["total_power"],
        "computed",
        detail="机组+冷冻泵+冷却泵+冷却塔+末端",
    )

    site_defaults = _site_defaults()
    for field, default in site_defaults.items():
        if device_data.get(field, 0.0) == 0.0:
            device_data[field] = default
            defaulted.append(field)
            mark_field(
                field_sources,
                field,
                default,
                "batch_default",
                detail="settings.yaml batch_defaults",
                substituted=True,
            )
    return defaulted


def parse_runtime_file(content: bytes, filename: str) -> dict[str, Any]:
    """解析上传文件，返回可寻优行与跳过统计。"""
    raw = _read_raw_table(content, filename)
    df = _promote_header(raw)
    column_map = {
        field: _find_column(df, aliases) for field, aliases in _FIELD_ALIASES.items()
    }
    status_column = _find_column(df, _STATUS_ALIASES)
    chiller_status_columns = _chiller_status_columns(df)

    missing_fields = [field for field in _REQUIRED_FIELDS if column_map.get(field) is None]
    rows: list[dict[str, Any]] = []
    skipped_not_running = 0
    skipped_invalid = 0

    for offset, (_, row) in enumerate(df.iterrows(), start=1):
        if row.isna().all():
            continue
        if not _is_running_row(row, status_column, chiller_status_columns):
            skipped_not_running += 1
            continue

        device_data = {
            "timestamp": _to_timestamp(row.get(column_map["timestamp"])) if column_map.get("timestamp") else datetime.now().isoformat()
        }
        site_defaults = _site_defaults()
        field_sources = init_field_sources(site_defaults)
        if column_map.get("timestamp"):
            mark_field(
                field_sources,
                "timestamp",
                device_data["timestamp"],
                "excel_column",
                excel_column=column_map["timestamp"],
            )
        for field in _REQUIRED_FIELDS:
            column = column_map.get(field)
            raw_val = _to_float(row.get(column), site_defaults.get(field, 0.0)) if column else site_defaults.get(field, 0.0)
            device_data[field] = raw_val
            if column and _to_float(row.get(column), 0.0) != 0.0:
                mark_field(
                    field_sources,
                    field,
                    raw_val,
                    "excel_column",
                    excel_column=column,
                )
            elif column:
                mark_field(
                    field_sources,
                    field,
                    raw_val,
                    "batch_default",
                    excel_column=column,
                    detail="Excel列存在但值为0，使用缺省",
                    substituted=True,
                )
        defaulted_fields = [
            field
            for field in _REQUIRED_FIELDS
            if column_map.get(field) is None and field in site_defaults
        ]
        for field in _derive_site_fields(row, df, device_data, column_map, field_sources):
            if field not in defaulted_fields:
                defaulted_fields.append(field)

        for field, patterns in (
            ("outdoor_humidity", ("制冷机房室外温湿度", "湿度")),
            ("outdoor_temp", ("制冷机房室外温湿度", "温度")),
        ):
            cols = _columns_matching(df, *patterns) or _columns_matching(
                df, "室外", patterns[1]
            )
            if cols and _first_number(row, cols) != 0 and field in defaulted_fields:
                defaulted_fields.remove(field)

        input_audit = build_input_audit(device_data, field_sources, defaulted_fields)
        equipment_units = resolve_equipment_units(row, df, device_data)

        if not any(_to_float(row.get(column_map[field]), 0.0) for field in _REQUIRED_FIELDS if column_map.get(field)):
            if device_data.get("chiller_load", 0.0) == 0 and device_data.get("total_power", 0.0) == 0:
                skipped_invalid += 1
                continue

        rows.append(
            {
                "row_number": offset,
                "raw": _raw_row_dict(row),
                "device_data": device_data,
                "equipment_units": equipment_units,
                "defaulted_fields": defaulted_fields,
                "field_sources": field_sources,
                "input_audit": input_audit,
            }
        )

    logger.info(
        f"批量导入解析完成: file={filename}, total={len(df)}, running={len(rows)}, "
        f"skipped_not_running={skipped_not_running}, missing={missing_fields}"
    )
    if rows:
        missing_fields = [
            field
            for field in _REQUIRED_FIELDS
            if all(field in row.get("defaulted_fields", []) for row in rows)
        ]
    defaulted_fields = sorted(
        {
            field
            for row in rows
            for field in row.get("defaulted_fields", [])
        }
    )
    return {
        "total_rows": int(len(df)),
        "running_rows": len(rows),
        "skipped_not_running": skipped_not_running,
        "skipped_invalid": skipped_invalid,
        "missing_fields": missing_fields,
        "defaulted_fields": defaulted_fields,
        "status_column": status_column,
        "column_map": column_map,
        "chiller_status_columns": chiller_status_columns,
        "rows": rows,
    }
