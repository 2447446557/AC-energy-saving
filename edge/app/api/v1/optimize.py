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
    """触发寻优：先清洗工况，再 PSO；支持 force 跳过平滑（批量/对比场景）。"""
    from app.main import get_data_cleaner, get_optimizer
    from app.schemas.device import DeviceData

    cleaner = get_data_cleaner()
    try:
        cleaned = cleaner.clean(DeviceData(**request.device_data))
        device_payload = cleaned.model_dump(mode="json")
    except Exception:
        device_payload = request.device_data

    report = getattr(cleaner, "last_report", None)
    if report and getattr(report, "is_anomalous_sample", False):
        level = "CRITICAL" if getattr(report, "circuit_broken", False) else "WARNING"
        storage.save_alarm(
            level=level,
            category="data",
            message=(
                "手动寻优工况异常: "
                f"缺失补全={getattr(report, 'missing_fixed', 0)}, "
                f"跳变过滤={getattr(report, 'spikes_filtered', 0)}, "
                f"连续异常={getattr(report, 'consecutive_anomalies', 0)}"
            ),
        )

    optimizer = get_optimizer()
    result = optimizer.optimize(
        OptimizeRequest(
            device_data=device_payload,
            initial_params=request.initial_params,
            force=request.force,
        )
    )

    # 保存寻优记录
    _save_optimize_result(
        result,
        input_snapshot=json.dumps(device_payload, ensure_ascii=False),
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
    mode: str = Query(
        default="total_power",
        description="寻优目标：total_power=系统总电最低；min_cooling_water=冷却回水最低",
    ),
    closed_loop: bool = Query(
        default=False,
        description=(
            "是否把上一行寻优结果回写到下一行输入。"
            "默认 false：Excel 各运行时刻独立回放（推荐）；"
            "true：跨行闭环仿真（易与行间负荷跳变冲突）。"
        ),
    ),
):
    """上传 Excel/CSV 运行趋势文件，对“运行状态=运行”的行批量寻优。"""
    from app.main import get_data_cleaner, get_energy_model, get_optimizer
    from app.services.power_baseline import current_operating_params, measured_baseline_breakdown
    from app.services.input_audit import build_pipeline_audit

    objective_mode = mode if mode in ("total_power", "min_cooling_water") else "total_power"
    content = await file.read()
    parsed = parse_runtime_file(content, file.filename or "upload")
    optimizer = get_optimizer()
    energy_model = get_energy_model()
    cleaner = get_data_cleaner()
    # 批量与定时闭环隔离：重置负荷 EWMA、清洗器与实测回写状态，避免历史行串味
    forecast = getattr(optimizer, "_load_forecast", None)
    if forecast is not None and hasattr(forecast, "reset"):
        forecast.reset()
    feedback = getattr(optimizer, "_feedback", None)
    if feedback is not None and hasattr(feedback, "reset"):
        feedback.reset()
    if hasattr(cleaner, "reset"):
        cleaner.reset()

    results = []
    success_count = 0
    failed_count = 0
    timeout_count = 0
    processed = 0
    prev_result: OptimizeResult | None = None
    # 闭环首行锚点：主机功率与室外温湿度固定为批量第一行实测，避免逐行覆盖漂移
    loop_anchor: dict[str, float] | None = None

    for item in parsed["rows"][:max_rows]:
        # 可选闭环：仅当 closed_loop=true 时，将上一轮寻优回写到本行输入。
        # Excel 趋势回放默认关闭，避免把不同时刻工况串成假连续过程。
        if closed_loop and loop_anchor is None:
            raw0 = item["device_data"]
            loop_anchor = {
                "chiller_power": float(raw0.get("chiller_power") or 0.0),
                "outdoor_temp": float(raw0.get("outdoor_temp") or 0.0),
                "outdoor_humidity": float(raw0.get("outdoor_humidity") or 0.0),
                "total_power": float(raw0.get("total_power") or 0.0),
            }
        if (
            closed_loop
            and prev_result is not None
            and prev_result.status == "success"
            and loop_anchor is not None
        ):
            fb = dict(item["device_data"])
            # 校准锚点始终用首行实测，不随行覆盖
            fb["chiller_power_reference"] = float(loop_anchor["chiller_power"])
            fb["chiller_power_reference_outdoor_temp"] = float(
                loop_anchor["outdoor_temp"]
            )
            fb["chiller_power_reference_outdoor_humidity"] = float(
                loop_anchor["outdoor_humidity"]
            )
            # 闭环主机功率：允许随预测微调，但禁止相对首轮锚点持续上抬形成“越寻越费”
            pred_chiller = float(prev_result.predicted_chiller_power or 0.0)
            ref_chiller = float(fb.get("chiller_power_reference") or 0.0)
            if ref_chiller > 1e-6 and pred_chiller > 0:
                fb["chiller_power"] = round(min(pred_chiller, ref_chiller * 1.03), 2)
            else:
                fb["chiller_power"] = round(pred_chiller, 2)
            # 闭环室温回写：钳在安全天花板以下，禁止把下一轮输入又贴回 26℃
            pred_indoor = float(prev_result.predicted_indoor_temp or 0.0)
            try:
                from app.algorithms.constraints import SafetyConstraints

                _c = SafetyConstraints()
                _ceiling = _c.effective_comfort_ceiling(
                    float(fb.get("outdoor_temp") or 30.0), pred_indoor
                )
                pred_indoor = min(pred_indoor, _ceiling)
            except Exception:
                pred_indoor = min(pred_indoor, 25.4)
            fb["indoor_temp"] = round(pred_indoor, 2)
            chp_n = max(int(prev_result.chilled_pump_count or 0), 1)
            cwp_n = max(int(prev_result.cooling_pump_count or 0), 1)
            fb["chilled_pump_running_count"] = chp_n
            fb["cooling_pump_running_count"] = cwp_n
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

        # 批量行间工况可跳变数百 kW：每行重置清洗器，避免 spike/熔断串味。
        # 在线定时任务仍用有状态清洗；离线 Excel 回放不应当作连续秒级采样。
        if hasattr(cleaner, "reset"):
            cleaner.reset()
        device_payload = item["device_data"]
        try:
            from app.schemas.device import DeviceData

            cleaned = cleaner.clean(DeviceData(**device_payload))
            device_payload = cleaned.model_dump(mode="json")
        except Exception:
            pass

        request = OptimizeRequest(
            device_data=device_payload,
            force=True,
            mode=objective_mode,
            commit_feedback=False,
        )
        result = optimizer.optimize(request)
        # 闭环批量：成功后再写入反馈，供下一扰动辨识；开环回放不污染全局反馈
        if (
            closed_loop
            and result.status == "success"
            and hasattr(optimizer, "commit_feedback")
        ):
            try:
                from app.schemas.device import DeviceData

                optimizer.commit_feedback(
                    DeviceData(**device_payload),
                    baseline_power=float(result.baseline_power or 0.0),
                    predicted_power=float(result.predicted_power or 0.0),
                )
            except Exception:
                pass
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
            input_snapshot=json.dumps(device_payload, ensure_ascii=False),
        )
        if len(results) < max_results:
            from app.schemas.device import DeviceData

            measured_total = float(device_payload.get("total_power") or 0.0)
            data = DeviceData(**device_payload)
            baseline_params = current_operating_params(device_payload)
            measured_baseline = measured_baseline_breakdown(device_payload)
            physics_baseline_power = 0.0
            optimized_params = None
            try:
                physics_breakdown = energy_model.predict(data, baseline_params)
                physics_baseline_power = physics_breakdown.total_power
                optimized_params = {
                    "chilled_water_temp": result.chilled_water_temp,
                    "chilled_water_temp_offset": result.chilled_water_temp_offset,
                    "chiller_load_pct": result.chiller_load_pct,
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

            # 失败/超时行不展示虚假节能率（兜底预测仍可能算得出正值）
            if result.status == "success" and measured_total > 0:
                saving_vs_measured = (
                    (measured_total - result.predicted_power) / measured_total * 100.0
                )
            else:
                saving_vs_measured = 0.0
            if result.status == "success" and model_baseline_power > 0:
                saving_vs_display_baseline = (
                    (model_baseline_power - result.predicted_power) / model_baseline_power * 100.0
                )
            elif result.status == "success":
                saving_vs_display_baseline = result.energy_saving_rate
            else:
                saving_vs_display_baseline = 0.0
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
            if device_payload.get("terminal_fan_power", 0.0) == 0:
                warnings.append(
                    f"缺少末端风机功率（各楼层/末端空调箱风机），预测仅计入默认{terminal_default}kW"
                )
            warnings.extend(pipeline_audit.get("notes", []))
            results.append(
                {
                    "row_number": item["row_number"],
                    "raw": item.get("raw", {}),
                    "input": device_payload,
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
        "objective_mode": objective_mode,
        "closed_loop": bool(closed_loop),
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
