"""PSO 寻优 stub 实现

空实现，返回固定默认值。Cursor 后续替换为真实 PSO 算法。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

from loguru import logger

from app.algorithms.interfaces import IOptimizer
from app.schemas.optimize import OptimizeRequest, OptimizeResult


class OptimizerStub:
    """寻优算法 stub

    返回固定默认参数，让闭环能跑通。
    Cursor 替换为基于 scikit-opt 的 PSO 实现。
    """

    def optimize(self, request: OptimizeRequest) -> OptimizeResult:
        """执行寻优（stub：返回默认参数）"""
        start_time = time.time()
        task_id = str(uuid.uuid4())

        logger.warning(
            "OptimizerStub.optimize: 使用 stub 默认值，"
            "请 Cursor 替换为真实 PSO 实现"
        )

        return OptimizeResult(
            task_id=task_id,
            status="success",
            chilled_water_temp=7.0,
            chilled_pump_freq=35.0,
            cooling_pump_freq=35.0,
            cooling_tower_fan_freq=30.0,
            predicted_power=0.0,
            energy_saving_rate=0.0,
            duration=time.time() - start_time,
            optimized_at=datetime.now(),
            remark="stub 默认值，待 Cursor 实现",
        )
