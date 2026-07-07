"""算法模块接入

本模块仅定义接口（Protocol）与空实现 stub。
核心算法（PSO 寻优、能耗模型、数据清洗、约束校验）由 Cursor 实现。

Trae 职责边界：
- 定义接口签名
- 提供 stub 空实现（返回固定值或 NotImplementedError）
- 在 main.py 中注入默认 stub，Cursor 后续可替换为真实实现
"""

from app.algorithms.constraints_stub import ConstraintsStub
from app.algorithms.data_cleaner_stub import DataCleanerStub
from app.algorithms.energy_model_stub import EnergyModelStub
from app.algorithms.interfaces import (
    IConstraints,
    IDataCleaner,
    IEnergyModel,
    IOptimizer,
)
from app.algorithms.optimizer_stub import OptimizerStub

__all__ = [
    "ConstraintsStub",
    "DataCleanerStub",
    "EnergyModelStub",
    "OptimizerStub",
    "IConstraints",
    "IDataCleaner",
    "IEnergyModel",
    "IOptimizer",
]
