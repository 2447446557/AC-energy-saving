"""LightGBM 黑盒功率模型接口：训练 / 预测 / 状态 / 与白盒对比。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Query, UploadFile

from app.schemas.common import success
from app.schemas.ml_power import (
    MlPowerCompareRequest,
    MlPowerPredictRequest,
    MlPowerTrainJsonRequest,
)
from app.services.batch_import import parse_runtime_file
from app.services.lightgbm_power_service import (
    device_data_to_row,
    get_lightgbm_power_model,
    predict_from_device,
    rows_from_batch_parse,
    train_from_rows,
)

router = APIRouter()


@router.get("/status")
async def ml_power_status():
    """查看 LightGBM 模型是否已加载及最近训练指标。"""
    return success(get_lightgbm_power_model().status())


@router.post("/train/json")
async def ml_power_train_json(request: MlPowerTrainJsonRequest):
    """用 JSON 行列表训练（每行含 outdoor_temp、泵频、total_power 等）。"""
    try:
        metrics = train_from_rows(request.rows, target=request.target)
    except Exception as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(metrics.to_dict(), message="LightGBM 训练完成")


@router.post("/train/upload")
async def ml_power_train_upload(
    file: UploadFile = File(...),
    target: str = Form(default="total_power"),
    max_rows: int = Form(default=50000),
):
    """上传 Excel/CSV 运行趋势训练（复用现有批量解析，取运行行）。"""
    content = await file.read()
    try:
        parsed = parse_runtime_file(content, file.filename or "upload.csv")
        rows = rows_from_batch_parse(parsed)[: max(1, min(int(max_rows), 200000))]
        metrics = train_from_rows(rows, target=target)
    except Exception as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(
        {
            "metrics": metrics.to_dict(),
            "parsed_rows": len(parsed.get("rows") or []),
            "trained_rows": len(rows),
            "filename": file.filename,
            "backend": get_lightgbm_power_model().status().get("backend"),
        },
        message="LightGBM 训练完成",
    )


@router.post("/predict")
async def ml_power_predict(request: MlPowerPredictRequest):
    """对单条工况预测功率（需已训练）。"""
    try:
        data = predict_from_device(request.device_data)
    except Exception as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(data)


@router.post("/compare-whitebox")
async def ml_power_compare_whitebox(request: MlPowerCompareRequest):
    """同一工况下对比白盒能耗模型与 LightGBM 预测。"""
    from app.main import get_energy_model
    from app.services.power_baseline import current_operating_params

    model = get_lightgbm_power_model()
    white = None
    black = None
    remark = ""
    try:
        energy = get_energy_model()
        params = request.control_params or current_operating_params(
            request.device_data.model_dump()
        )
        bd = energy.predict(request.device_data, params)
        white = {
            "predicted_power": round(float(bd.total_power), 3),
            "predicted_chiller_power": round(float(bd.chiller_power), 3),
            "predicted_indoor_temp": round(float(bd.predicted_indoor_temp), 3),
        }
    except Exception as e:
        remark = f"白盒预测失败: {e}"

    try:
        black = predict_from_device(request.device_data)
    except Exception as e:
        if remark:
            remark += "; "
        remark += f"黑盒预测失败: {e}"

    delta = None
    if white and black and black.get("predicted_power") is not None:
        delta = round(
            float(black["predicted_power"]) - float(white["predicted_power"]), 3
        )

    return success(
        {
            "whitebox": white,
            "lightgbm": black,
            "delta_black_minus_white_kw": delta,
            "model_status": model.status(),
            "input_row": device_data_to_row(request.device_data),
            "remark": remark,
        }
    )


@router.get("/features")
async def ml_power_features(target: str = Query(default="total_power")):
    """返回训练所用特征列说明。"""
    return success(
        {
            "target": target,
            "feature_columns": list(get_lightgbm_power_model().feature_columns or []),
            "default_features": [
                "outdoor_temp",
                "outdoor_humidity",
                "indoor_temp",
                "indoor_humidity",
                "indoor_load",
                "chiller_load",
                "chilled_water_temp",
                "cooling_water_temp",
                "chilled_pump_freq",
                "cooling_pump_freq",
                "cooling_tower_fan_freq",
                "chilled_pump_running_count",
                "cooling_pump_running_count",
                "terminal_fan_power",
            ],
            "note": "黑盒只预测功率；推荐设定仍由 PSO+约束完成。",
        }
    )
