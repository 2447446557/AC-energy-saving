"""寻优接口（封装 Cursor 算法）"""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Query, UploadFile

from app.schemas.common import Response, success
from app.schemas.optimize import OptimizeRequest, OptimizeResult
from app.services.batch_import import parse_runtime_file, parse_runtime_file_last_row
from app.services.storage import storage

router = APIRouter()


def _save_optimize_result(result: OptimizeResult, input_snapshot: str = "") -> None:
    """保存寻优结果与审计/告警。"""
    from app.models.optimize_record import OptimizeRecord

    record = OptimizeRecord(
        task_id=result.task_id,
        status=result.status,
        chilled_water_temp=result.chilled_water_temp,
        chilled_pump_freq=result.chilled_pump_freq,
        chilled_pump_count=result.chilled_pump_count,
        chilled_pump_power=result.chilled_pump_power,
        cooling_pump_freq=result.cooling_pump_freq,
        cooling_pump_count=result.cooling_pump_count,
        cooling_pump_power=result.cooling_pump_power,
        cooling_tower_fan_freq=result.cooling_tower_fan_freq,
        cooling_tower_count=result.cooling_tower_count,
        cooling_tower_power=result.cooling_tower_power,
        predicted_power=result.predicted_power,
        energy_saving_rate=result.energy_saving_rate,
        duration=result.duration,
        optimized_at=result.optimized_at,
        input_snapshot=input_snapshot,
        remark=result.remark,
    )
    storage.save_optimize_record(record)


@router.post("/run")
async def run_optimize(request: OptimizeRequest):
    """触发寻优

    Trae 仅做接口封装与参数透传，
    实际寻优算法由 Cursor 实现（当前为 stub）。
    """
    # 延迟导入，避免循环依赖
    from app.main import get_optimizer

    optimizer = get_optimizer()
    result = optimizer.optimize(request)

    # 保存寻优记录
    _save_optimize_result(
        result,
        input_snapshot=json.dumps(request.device_data, ensure_ascii=False),
    )
    storage.save_operation_log(
        action="optimize_run",
        target="optimizer",
        operator="api",
        result=result.status,
        detail=result.model_dump_json(),
    )
    if result.status != "success":
        storage.save_alarm(
            level="CRITICAL" if result.status == "failed" else "WARNING",
            category="optimize",
            message=f"手动寻优降级: status={result.status}, remark={result.remark}",
        )

    return success(result.model_dump(mode="json"))


@router.post("/import-preview")
async def import_preview(file: UploadFile = File(...)):
    """上传 Excel/CSV，解析最后一条「运行」行，供手动寻优输入框自动填充。"""
    content = await file.read()
    parsed = parse_runtime_file_last_row(content, file.filename or "upload")
    selected = parsed.get("selected_row")
    if selected is None:
        return success(
            {
                "filename": file.filename,
                "running_rows": parsed.get("running_rows", 0),
                "total_rows": parsed.get("total_rows", 0),
                "message": parsed.get("message", "未找到有效运行行"),
            },
            message=parsed.get("message", "未找到有效运行行"),
        )
    return success(
        {
            "filename": file.filename,
            "message": parsed.get("message"),
            "selected_row_number": parsed.get("selected_row_number"),
            "running_rows": parsed.get("running_rows", 0),
            "total_rows": parsed.get("total_rows", 0),
            "device_data": selected.get("device_data"),
            "equipment_units": selected.get("equipment_units"),
            "input_audit": selected.get("input_audit"),
            "field_sources": selected.get("field_sources"),
            "defaulted_fields": selected.get("defaulted_fields"),
            "config_filled_fields": selected.get("config_filled_fields", []),
            "config_defaults": parsed.get("config_defaults"),
            "raw": selected.get("raw"),
        }
    )


@router.post("/batch-upload")
async def batch_upload_optimize(
    file: UploadFile = File(...),
    max_rows: int = Query(default=1000, ge=1, le=10000),
    max_results: int = Query(default=200, ge=1, le=1000),
):
    """上传 Excel/CSV 运行趋势文件，对“运行状态=运行”的行批量寻优。"""
    from app.main import get_energy_model, get_optimizer
    from app.services.power_baseline import current_operating_params, measured_baseline_breakdown
    from app.services.input_audit import build_pipeline_audit

    content = await file.read()
    parsed = parse_runtime_file(content, file.filename or "upload")
    optimizer = get_optimizer()
    energy_model = get_energy_model()

    results = []
    success_count = 0
    failed_count = 0
    timeout_count = 0
    processed = 0
    prev_result: OptimizeResult | None = None

    for item in parsed["rows"][:max_rows]:
        # 闭环反馈：将上一轮寻优的预测值代入本轮输入，
        # 使多次寻优形成连续闭环（预测功率→下一轮输入功率，预测室温→下一轮输入室温）
        if prev_result is not None and prev_result.status == "success":
            fb = dict(item["device_data"])
            # 当前功率随上一轮预测回写；首轮/现场实测功率单独保留为校准锚点，
            # 避免多轮模型自反馈把持续运行的多台主机压到非物理低功率。
            fb["chiller_power_reference"] = float(
                fb.get("chiller_power_reference") or fb.get("chiller_power") or 0.0
            )
            fb["chiller_power_reference_outdoor_temp"] = float(
                fb.get("chiller_power_reference_outdoor_temp")
                or fb.get("outdoor_temp")
                or 0.0
            )
            fb["chiller_power_reference_outdoor_humidity"] = float(
                fb.get("chiller_power_reference_outdoor_humidity")
                or fb.get("outdoor_humidity")
                or 0.0
            )
            fb["chiller_power"] = round(prev_result.predicted_chiller_power, 2)
            fb["indoor_temp"] = round(prev_result.predicted_indoor_temp, 2)
            chp_n = max(int(prev_result.chilled_pump_count or 0), 1)
            cwp_n = max(int(prev_result.cooling_pump_count or 0), 1)
            fb["chilled_pump_power"] = round(prev_result.chilled_pump_power * chp_n, 2)
            fb["cooling_pump_power"] = round(prev_result.cooling_pump_power * cwp_n, 2)
            fb["cooling_tower_fan_power"] = round(prev_result.cooling_tower_power, 2)
            # 总功率必须与闭环回写后的分项功率一致，不能直接采用可能被安全闸
            # 锚定过的 predicted_power。
            fb["total_power"] = round(
                float(fb.get("chiller_power") or 0.0)
                + float(fb.get("chilled_pump_power") or 0.0)
                + float(fb.get("cooling_pump_power") or 0.0)
                + float(fb.get("cooling_tower_fan_power") or 0.0)
                + float(fb.get("terminal_fan_power") or 0.0),
                2,
            )
            fb["chilled_pump_freq"] = round(prev_result.chilled_pump_freq, 2)
            fb["cooling_pump_freq"] = round(prev_result.cooling_pump_freq, 2)
            fb["cooling_tower_fan_freq"] = round(prev_result.cooling_tower_fan_freq, 2)
            fb["chiller_load"] = round(prev_result.chiller_load_pct, 2)
            fb["chilled_water_temp"] = round(prev_result.chilled_water_temp, 2)
            fb["cooling_water_temp"] = round(prev_result.predicted_cooling_water_temp, 2)
            item["device_data"] = fb

        request = OptimizeRequest(device_data=item["device_data"], force=True)
        result = optimizer.optimize(request)
        prev_result = result
        processed += 1
        if result.status == "success":
            success_count += 1
        elif result.status == "timeout":
            timeout_count += 1
        else:
            failed_count += 1

        _save_optimize_result(
            result,
            input_snapshot=json.dumps(item["device_data"], ensure_ascii=False),
        )
        if len(results) < max_results:
            from app.schemas.device import DeviceData

            measured_total = float(item["device_data"].get("total_power") or 0.0)
            data = DeviceData(**item["device_data"])
            baseline_params = current_operating_params(item["device_data"])
            measured_baseline = measured_baseline_breakdown(item["device_data"])
            physics_baseline_power = 0.0
            optimized_params = None
            try:
                physics_breakdown = energy_model.predict(data, baseline_params)
                physics_baseline_power = physics_breakdown.total_power
                optimized_params = {
                    "chilled_water_temp": result.chilled_water_temp,
                    "chilled_pump_freq": result.chilled_pump_freq,
                    "chilled_pump_count": result.chilled_pump_count,
                    "cooling_pump_freq": result.cooling_pump_freq,
                    "cooling_pump_count": result.cooling_pump_count,
                    "cooling_tower_fan_freq": result.cooling_tower_fan_freq,
                    "cooling_tower_count": result.cooling_tower_count,
                }
                optimized_breakdown = energy_model.predict(data, optimized_params)
                if measured_baseline is not None:
                    baseline_breakdown = measured_baseline
                    model_baseline_power = measured_baseline["total_power"]
                else:
                    baseline_breakdown = physics_breakdown
                    model_baseline_power = physics_baseline_power
            except Exception:
                baseline_breakdown = None
                optimized_breakdown = None
                optimized_params = None
                model_baseline_power = (
                    measured_baseline["total_power"] if measured_baseline else 0.0
                )
                physics_baseline_power = 0.0

            saving_vs_measured = (
                (measured_total - result.predicted_power) / measured_total * 100.0
                if measured_total > 0
                else 0.0
            )
            if model_baseline_power > 0:
                saving_vs_display_baseline = (
                    (model_baseline_power - result.predicted_power) / model_baseline_power * 100.0
                )
            else:
                saving_vs_display_baseline = result.energy_saving_rate
            saving_vs_model_baseline = round(saving_vs_display_baseline, 2)
            field_sources = item.get("field_sources", {})
            pipeline_audit = build_pipeline_audit(
                physics_baseline_power=physics_baseline_power,
                measured_baseline_power=model_baseline_power,
                measured_total=measured_total,
                predicted_power=result.predicted_power,
                optimizer_saving_rate=result.energy_saving_rate,
                saving_vs_measured=saving_vs_measured,
                saving_vs_display_baseline=saving_vs_display_baseline,
                field_sources=field_sources,
            )
            defaulted_fields = item.get("defaulted_fields", [])
            warnings = list(pipeline_audit.get("issues", []))
            try:
                from app.services.settings_config import settings_config_service

                batch_defaults = settings_config_service.get_batch_defaults()
                terminal_default = (
                    settings_config_service.get_app_settings().energy_model.terminal_fan_default
                )
            except Exception:
                batch_defaults = {
                    "outdoor_temp": 30.0,
                    "outdoor_humidity": 60.0,
                    "indoor_temp": 27.0,
                    "indoor_humidity": 55.0,
                }
                terminal_default = 2.0
            if defaulted_fields:
                warnings.append(
                    "Excel 缺少部分关键测点，已使用默认值/近似值，结果仅用于离线测试参考"
                )
            if any(
                field in defaulted_fields
                for field in (
                    "outdoor_temp",
                    "outdoor_humidity",
                    "indoor_temp",
                    "indoor_humidity",
                )
            ) and field_sources.get("outdoor_temp", {}).get("source") not in (
                "excel_column",
                "excel_multi_header",
            ):
                warnings.append(
                    "缺少室外/室内温湿度，已用缺省配置（室外"
                    f"{batch_defaults['outdoor_temp']}℃/{batch_defaults['outdoor_humidity']}%RH，"
                    f"室内{batch_defaults['indoor_temp']}℃/{batch_defaults['indoor_humidity']}%RH）"
                )
            elif any(
                f in defaulted_fields for f in ("indoor_temp", "indoor_humidity")
            ):
                warnings.append(
                    f"缺少室内温湿度，已用缺省（{batch_defaults['indoor_temp']}℃/"
                    f"{batch_defaults['indoor_humidity']}%RH）"
                )
            if item["device_data"].get("terminal_fan_power", 0.0) == 0:
                warnings.append(
                    f"缺少末端风机功率（各楼层/末端空调箱风机），预测仅计入默认{terminal_default}kW"
                )
            warnings.extend(pipeline_audit.get("notes", []))
            results.append(
                {
                    "row_number": item["row_number"],
                    "raw": item.get("raw", {}),
                    "input": item["device_data"],
                    "field_sources": field_sources,
                    "input_audit": item.get("input_audit", {}),
                    "defaulted_fields": defaulted_fields,
                    "measured_total_power": round(measured_total, 3),
                    "model_baseline_power": round(model_baseline_power, 3),
                    "physics_baseline_power": round(physics_baseline_power, 3),
                    "saving_rate": saving_vs_model_baseline,
                    "saving_rate_vs_measured": round(saving_vs_measured, 2),
                    "optimizer_internal_saving_rate": round(result.energy_saving_rate, 2),
                    "baseline_params": baseline_params,
                    "optimized_params": optimized_params if optimized_breakdown else None,
                    "baseline_breakdown": baseline_breakdown,
                    "optimized_breakdown": (
                        optimized_breakdown.__dict__ if optimized_breakdown else None
                    ),
                    "pipeline_audit": pipeline_audit,
                    "data_quality": {
                        "reliable_for_control": len(defaulted_fields) == 0,
                        "warnings": warnings,
                    },
                    "result": result.model_dump(mode="json"),
                }
            )

    summary = {
        "filename": file.filename,
        "total_rows": parsed["total_rows"],
        "running_rows": parsed["running_rows"],
        "processed_rows": processed,
        "success_count": success_count,
        "failed_count": failed_count,
        "timeout_count": timeout_count,
        "skipped_not_running": parsed["skipped_not_running"],
        "skipped_invalid": parsed["skipped_invalid"],
        "missing_fields": parsed["missing_fields"],
        "defaulted_fields": parsed.get("defaulted_fields", []),
        "status_column": parsed["status_column"],
        "column_map": parsed["column_map"],
        "result_truncated": parsed["running_rows"] > len(results),
        "results": results,
    }
    storage.save_operation_log(
        action="batch_optimize_upload",
        target="optimizer",
        operator="api",
        result="success" if failed_count == 0 and timeout_count == 0 else "partial",
        detail=json.dumps(
            {k: v for k, v in summary.items() if k != "results"},
            ensure_ascii=False,
        ),
    )
    return success(summary)


@router.get("/latest")
async def get_latest_optimize():
    """获取最近一次寻优结果"""
    record = storage.get_latest_optimize_record()
    if record is None:
        return success(None, message="暂无寻优记录")
    return success(record.model_dump(mode="json"))


@router.get("/history")
async def get_optimize_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
):
    """分页查询寻优历史"""
    items, total = storage.get_optimize_records(page, page_size)
    return success(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [item.model_dump(mode="json") for item in items],
        }
    )
