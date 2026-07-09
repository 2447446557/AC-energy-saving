"""寻优定时任务

每 10~15 分钟执行一次全局寻优（贴合空调热惰性特性）。
"""

from __future__ import annotations

import json

from loguru import logger

from app.schemas.optimize import OptimizeRequest


def run_optimize() -> None:
    """寻优任务入口

    流程：
    1. 获取最新工况数据
    2. 数据清洗（stub）
    3. 执行寻优（stub）
    4. 约束校验（stub）
    5. 保存记录
    6. 下发控制（stub）

    注意：Trae 仅做调度与调用串联，
    寻优/清洗/约束/下发的核心逻辑由 Cursor 实现。
    """
    logger.info("===== 寻优任务开始 =====")

    try:
        # 延迟导入，避免循环依赖
        from app.services.storage import storage
        from app.services.simulator import simulator
        from app.main import (
            get_optimizer,
            get_data_cleaner,
            get_constraints,
        )

        # 1. 获取最新工况数据
        latest = storage.get_latest_runtime_data()
        if latest is None:
            # 没有数据则生成一条
            simulator.generate_once()
            latest = storage.get_latest_runtime_data()

        if latest is None:
            logger.warning("无工况数据，跳过寻优")
            return

        # 2. 数据清洗（stub，Cursor 实现）
        from app.schemas.device import DeviceData

        raw_data = json.loads(latest.raw_data)
        device_data = DeviceData(**raw_data)

        cleaner = get_data_cleaner()
        cleaned_data = cleaner.clean(device_data)
        report = getattr(cleaner, "last_report", None)
        if report and getattr(report, "is_anomalous_sample", False):
            level = "CRITICAL" if getattr(report, "circuit_broken", False) else "WARNING"
            storage.save_alarm(
                level=level,
                category="data",
                message=(
                    "工况数据异常: "
                    f"缺失补全={getattr(report, 'missing_fixed', 0)}, "
                    f"跳变过滤={getattr(report, 'spikes_filtered', 0)}, "
                    f"越界剔除={getattr(report, 'out_of_range', 0)}, "
                    f"工况突变={getattr(report, 'regime_shifts', 0)}, "
                    f"连续异常={getattr(report, 'consecutive_anomalies', 0)}"
                ),
            )

        # 3. 执行寻优（stub，Cursor 实现）
        optimizer = get_optimizer()
        request = OptimizeRequest(
            device_data=cleaned_data.model_dump(mode="json"),
            force=True,
        )
        result = optimizer.optimize(request)

        # 4. 约束校验（stub，Cursor 实现）
        constraints = get_constraints()
        params = {
            "chilled_water_temp": result.chilled_water_temp,
            "chilled_pump_freq": result.chilled_pump_freq,
            "cooling_pump_freq": result.cooling_pump_freq,
            "cooling_tower_fan_freq": result.cooling_tower_fan_freq,
        }
        if not constraints.validate(params):
            logger.warning("寻优结果未通过约束校验，已丢弃")
            storage.save_alarm(
                level="CRITICAL",
                category="optimize",
                message=f"寻优结果未通过安全约束校验，已丢弃: {params}",
            )
            storage.save_operation_log(
                action="optimize_task",
                target="optimizer",
                result="failed",
                detail='{"reason":"constraint_invalid"}',
            )
            return

        # 5. 保存记录
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
            input_snapshot=cleaned_data.model_dump_json(),
            remark=result.remark,
        )
        storage.save_optimize_record(record)
        storage.save_operation_log(
            action="optimize_task",
            target="optimizer",
            result=result.status,
            detail=result.model_dump_json(),
        )
        if result.status != "success":
            storage.save_alarm(
                level="CRITICAL" if result.status == "failed" else "WARNING",
                category="optimize",
                message=f"寻优任务降级: status={result.status}, remark={result.remark}",
            )

        logger.info(
            f"寻优完成: 节能率={result.energy_saving_rate:.1f}%, "
            f"耗时={result.duration:.2f}s"
        )

    except Exception as e:
        logger.error(f"寻优任务异常: {e}", exc_info=True)
        try:
            from app.services.storage import storage

            storage.save_alarm(
                level="CRITICAL",
                category="system",
                message=f"寻优任务异常: {e}",
            )
            storage.save_operation_log(
                action="optimize_task",
                target="optimizer",
                result="failed",
                detail=json.dumps({"error": str(e)}, ensure_ascii=False),
            )
        except Exception:
            logger.debug("寻优异常告警落库失败", exc_info=True)

    logger.info("===== 寻优任务结束 =====")
