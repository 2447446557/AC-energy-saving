"""算法接口定义

使用 typing.Protocol 定义四个核心算法接口。
Cursor 后续实现这些接口，替换 stub。

注意：Trae 仅做接口封装与参数透传，不实现核心算法逻辑。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.device import DeviceData
from app.schemas.optimize import OptimizeRequest, OptimizeResult


@runtime_checkable
class IOptimizer(Protocol):
    """PSO 寻优接口

    基于 scikit-opt 封装的工业级 PSO 粒子群寻优。
    Cursor 实现，Trae 仅做接口定义与调用封装。
    """

    def optimize(self, request: OptimizeRequest) -> OptimizeResult:
        """执行寻优

        Args:
            request: 寻优请求，包含当前工况数据

        Returns:
            寻优结果，包含最优控制参数组合
        """
        ...


@runtime_checkable
class IEnergyModel(Protocol):
    """空调能耗数学模型接口

    为寻优算法提供目标函数，计算当前工况总能耗。
    Cursor 实现。
    """

    def calculate(self, data: DeviceData, params: dict) -> float:
        """计算能耗

        Args:
            data: 当前工况数据
            params: 控制参数组合

        Returns:
            总能耗（kW）
        """
        ...


@runtime_checkable
class IDataCleaner(Protocol):
    """数据清洗与鲁棒容错接口

    处理异常跳变过滤、缺失值插值、数据平滑。
    Cursor 实现。
    """

    def clean(self, raw: DeviceData) -> DeviceData:
        """清洗数据

        Args:
            raw: 原始工况数据

        Returns:
            清洗后的工况数据
        """
        ...


@runtime_checkable
class IConstraints(Protocol):
    """约束校验接口

    所有寻优结果强制边界约束，保障设备与医疗区域舒适度。
    Cursor 实现。
    """

    def validate(self, params: dict) -> bool:
        """校验参数是否满足约束

        Args:
            params: 控制参数组合

        Returns:
            是否满足约束
        """
        ...
