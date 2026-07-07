"""算法模块装配（Cursor 实现的统一接入点）

将 Cursor 实现的核心算法（能耗模型、约束、数据清洗、PSO 寻优、熔断兜底）
以及高仿真度模拟数据生成器组装为一组共享实例，供 main.py 注入到框架。

装配关系
--------
- SafetyConstraints  ← 约束边界（供 优化器 / 守卫 复用）
- ACEnergyModel      ← 目标函数
- RobustDataCleaner  ← 数据清洗（被 优化器 引用以感知熔断）
- SafeOutputGuard    ← 平滑 + 兜底（引用约束）
- PSOOptimizer       ← 引用 能耗模型 / 约束 / 守卫 / 清洗器
- HospitalDataGenerator ← 引用 能耗模型，产出物理自洽的仿真数据

设计要点：所有依赖显式注入，全局仅一套共享实例，保证数据清洗的熔断状态
能被寻优器实时感知，形成闭环鲁棒链路。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.algorithms.constraints import SafetyConstraints
from app.algorithms.data_cleaner import RobustDataCleaner
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.optimizer import PSOOptimizer
from app.services.hospital_simulator import HospitalDataGenerator


@dataclass
class AlgorithmBundle:
    """一组装配完成的算法实例。"""

    constraints: SafetyConstraints
    energy_model: ACEnergyModel
    data_cleaner: RobustDataCleaner
    guard: SafeOutputGuard
    optimizer: PSOOptimizer
    generator: HospitalDataGenerator


def build_algorithms() -> AlgorithmBundle:
    """构建并装配全部 Cursor 算法实例（依赖显式注入）。"""
    constraints = SafetyConstraints()
    energy_model = ACEnergyModel()
    data_cleaner = RobustDataCleaner()
    guard = SafeOutputGuard(constraints)
    optimizer = PSOOptimizer(
        energy_model=energy_model,
        constraints=constraints,
        guard=guard,
        data_cleaner=data_cleaner,
    )
    generator = HospitalDataGenerator(energy_model=energy_model)

    logger.info("Cursor 算法模块装配完成（能耗/约束/清洗/寻优/兜底/仿真）")
    return AlgorithmBundle(
        constraints=constraints,
        energy_model=energy_model,
        data_cleaner=data_cleaner,
        guard=guard,
        optimizer=optimizer,
        generator=generator,
    )
