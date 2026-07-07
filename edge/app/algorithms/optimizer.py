"""PSO 粒子群寻优模块（项目核心壁垒 · 工业级封装）

基于开源库 scikit-opt 封装工业级 PSO 粒子群寻优，实现多变量协同寻优
（对应设计文档 4.6 节、需求文档 3.1 节）。

优化变量（4 维，顺序见 constraints.VAR_ORDER）：
    冷水供水温度、冷冻泵频率、冷却泵频率、冷却塔风机频率

优化目标：
    在满足设备安全硬约束、室内舒适软约束的前提下，系统总能耗最小。

目标函数 = 能耗模型总功率
          + 舒适度惩罚（室内温度越界，软约束）
          + 硬约束越界极大惩罚（保险，正常被 lb/ub 拦截）

工程化鲁棒设计：
- 寻优超时：子线程执行 + 墙钟超时，超时立即兜底，杜绝阻塞主调度。
- 收敛失败：结果非有限 / 未改进 / 抛异常，一律降级为兜底。
- 数据熔断：上游数据清洗判定连续异常时，直接切回安全固定参数。
- 参数平滑：最优解经阶梯平滑后输出，保护设备。
- 局部最优规避：多粒子 + 惯性权重 + 适度迭代，兼顾收敛速度与全局性。
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any

import numpy as np
from loguru import logger
from sko.PSO import PSO

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.core.config import get_business_config
from app.schemas.device import DeviceData
from app.schemas.optimize import OptimizeRequest, OptimizeResult

# 目标函数惩罚系数（远大于典型能耗量级，使非法/越界解被 PSO 自动抛弃）
_HARD_PENALTY_WEIGHT = 1.0e6
_COMFORT_PENALTY_WEIGHT = 500.0


class PSOOptimizer:
    """工业级 PSO 寻优器（实现 IOptimizer）。"""

    def __init__(
        self,
        energy_model: ACEnergyModel,
        constraints: SafetyConstraints,
        guard: SafeOutputGuard,
        data_cleaner: Any | None = None,
        pop: int = 40,
        max_iter: int = 60,
        w: float = 0.8,
        c1: float = 0.5,
        c2: float = 0.5,
        timeout_seconds: float | None = None,
    ) -> None:
        """
        Args:
            energy_model: 能耗模型（提供目标函数）。
            constraints: 安全约束（提供边界与惩罚）。
            guard: 安全输出守卫（平滑 + 兜底）。
            data_cleaner: 可选数据清洗器，用于感知熔断状态。
            pop: 粒子数量（越多全局性越好，代价是耗时）。
            max_iter: 最大迭代次数。
            w/c1/c2: 惯性权重 / 个体 / 群体学习因子。
            timeout_seconds: 寻优墙钟超时，缺省读取业务配置。
        """
        self._energy_model = energy_model
        self._constraints = constraints
        self._guard = guard
        self._data_cleaner = data_cleaner
        self._pop = pop
        self._max_iter = max_iter
        self._w = w
        self._c1 = c1
        self._c2 = c2

        if timeout_seconds is None:
            cfg = get_business_config().get("optimize", {}) or {}
            timeout_seconds = float(cfg.get("timeout_seconds", 60))
        self._timeout = timeout_seconds

    # ---------- IOptimizer 协议实现 ----------

    def optimize(self, request: OptimizeRequest) -> OptimizeResult:
        """执行一次寻优，返回带兜底保障的最优控制参数。"""
        start = time.time()
        task_id = str(uuid.uuid4())

        # --- 解析工况数据 ---
        try:
            data = DeviceData(**request.device_data)
        except Exception as e:
            logger.error(f"寻优输入解析失败: {e}")
            return self._fallback_result(
                task_id, "failed", f"输入解析失败:{e}", start
            )

        # --- 数据熔断优先级最高：连续异常直接切固定参数 ---
        if self._data_cleaner is not None and getattr(
            self._data_cleaner, "is_circuit_broken", lambda: False
        )():
            params = self._guard.fallback_params("数据熔断")
            return self._build_result(
                task_id, "failed", data, params, start, remark="数据连续异常熔断，切回安全固定参数"
            )

        # --- 基线能耗（当前实测控制参数下的能耗，用于计算节能率） ---
        current_params = self._current_params(data)
        try:
            baseline_power = self._energy_model.predict(
                data, current_params
            ).total_power
        except Exception as e:
            logger.error(f"基线能耗计算失败: {e}")
            baseline_power = 0.0

        # --- 运行 PSO（带超时） ---
        try:
            best_params, best_y, converged = self._run_pso_with_timeout(data)
        except Exception as e:
            logger.error(f"PSO 寻优异常: {e}", exc_info=True)
            params = self._guard.fallback_params(f"寻优异常:{e}")
            return self._build_result(
                task_id, "failed", data, params, start, baseline_power,
                remark=f"寻优异常，已兜底:{e}",
            )

        if best_params is None:
            # 超时
            params = self._guard.fallback_params("寻优超时")
            return self._build_result(
                task_id, "timeout", data, params, start, baseline_power,
                remark=f"寻优超时(>{self._timeout}s)，已兜底",
            )

        # --- 收敛/合法性校验 ---
        if (
            best_y is None
            or not np.isfinite(best_y)
            or best_y >= _HARD_PENALTY_WEIGHT
            or not self._constraints.validate(best_params)
        ):
            params = self._guard.fallback_params("收敛失败/结果非法")
            return self._build_result(
                task_id, "failed", data, params, start, baseline_power,
                remark="寻优收敛失败或结果非法，已兜底",
            )

        # --- 有效解：登记 + 阶梯平滑输出 ---
        self._guard.register_good(best_params)
        # 应急判定：若“当前正在下发的设定值”在本工况下已预测舒适度越界，
        # 说明常规阶梯速度跟不上工况突变，启用应急步长加快逼近最优解。
        urgent = self._is_comfort_at_risk(data)
        smoothed = self._guard.smooth(best_params, urgent=urgent)

        remark = "" if converged else "达到最大迭代（未提前收敛）"
        return self._build_result(
            task_id, "success", data, smoothed, start, baseline_power, remark=remark
        )

    # ---------- PSO 执行 ----------

    def _run_pso_with_timeout(
        self, data: DeviceData
    ) -> tuple[dict[str, float] | None, float | None, bool]:
        """在子线程运行 PSO，主线程按墙钟超时等待。

        Returns:
            (最优参数 dict | None, 最优目标值 | None, 是否提前收敛)
            超时返回 (None, None, False)。
        """
        lb, ub = self._constraints.bounds_array()
        objective = self._make_objective(data)

        result: dict[str, Any] = {}

        def _worker() -> None:
            try:
                pso = PSO(
                    func=objective,
                    n_dim=len(VAR_ORDER),
                    pop=self._pop,
                    max_iter=self._max_iter,
                    lb=lb,
                    ub=ub,
                    w=self._w,
                    c1=self._c1,
                    c2=self._c2,
                )
                pso.run()
                best_x = np.asarray(pso.gbest_x, dtype=float).ravel()
                best_y = float(np.asarray(pso.gbest_y, dtype=float).ravel()[0])
                result["params"] = {
                    var: float(best_x[i]) for i, var in enumerate(VAR_ORDER)
                }
                result["y"] = best_y
                # 提前收敛判定：最优值历史末段无明显改进
                result["converged"] = self._detect_convergence(pso)
            except Exception as e:  # 线程内异常不外抛，记录后由主线程兜底
                result["error"] = e

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(self._timeout)

        if worker.is_alive():
            logger.error(f"PSO 寻优超时 (>{self._timeout}s)")
            return None, None, False
        if "error" in result:
            raise result["error"]
        return result.get("params"), result.get("y"), result.get("converged", False)

    def _make_objective(self, data: DeviceData):
        """构造 PSO 目标函数（最小化）：能耗 + 舒适惩罚 + 硬约束惩罚。"""
        energy_model = self._energy_model
        constraints = self._constraints

        def objective(x) -> float:
            x = np.asarray(x, dtype=float).ravel()
            params = {var: float(x[i]) for i, var in enumerate(VAR_ORDER)}
            try:
                breakdown = energy_model.predict(data, params)
                cost = breakdown.total_power
                cost += _COMFORT_PENALTY_WEIGHT * constraints.comfort_penalty(
                    breakdown.predicted_indoor_temp
                )
                cost += _HARD_PENALTY_WEIGHT * constraints.hard_violation(params)
                if not np.isfinite(cost):
                    return _HARD_PENALTY_WEIGHT
                return float(cost)
            except Exception:
                # 单点评估异常不能中断整体寻优，返回极大值使该粒子被抛弃
                return _HARD_PENALTY_WEIGHT

        return objective

    @staticmethod
    def _detect_convergence(pso: PSO, tol: float = 1e-4, window: int = 8) -> bool:
        """依据 gbest 历史判断是否提前收敛（末段窗口内改进小于容差）。"""
        history = getattr(pso, "gbest_y_hist", None)
        if not history or len(history) < window:
            return False
        try:
            tail = [float(np.asarray(v).ravel()[0]) for v in history[-window:]]
        except Exception:
            return False
        return (max(tail) - min(tail)) < tol

    # ---------- 结果构造 ----------

    def _is_comfort_at_risk(self, data: DeviceData) -> bool:
        """判断当前正在下发的设定值在本工况下是否已预测舒适度越界。

        用于触发应急平滑：常规阶梯速度不足以跟上工况突变时快速纠偏。
        任何异常一律按“非紧急”处理，避免误触发大步调节。
        """
        try:
            prev = self._guard.last_output
            indoor = self._energy_model.predict(data, prev).predicted_indoor_temp
            return self._constraints.comfort_penalty(indoor) > 0.0
        except Exception:
            return False

    @staticmethod
    def _current_params(data: DeviceData) -> dict[str, float]:
        """从实测工况提取当前控制参数（作为节能率基线）。"""
        return {
            "chilled_water_temp": float(data.chilled_water_temp or 7.0),
            "chilled_pump_freq": float(data.chilled_pump_freq or 35.0),
            "cooling_pump_freq": float(data.cooling_pump_freq or 35.0),
            "cooling_tower_fan_freq": float(data.cooling_tower_fan_freq or 30.0),
        }

    def _build_result(
        self,
        task_id: str,
        status: str,
        data: DeviceData,
        params: dict[str, float],
        start: float,
        baseline_power: float = 0.0,
        remark: str = "",
    ) -> OptimizeResult:
        """依据最终控制参数构造 OptimizeResult（含预测能耗与节能率）。"""
        try:
            predicted = self._energy_model.predict(data, params).total_power
        except Exception:
            predicted = 0.0

        if baseline_power > 1e-6 and predicted > 0:
            saving = (baseline_power - predicted) / baseline_power * 100.0
            saving = max(saving, 0.0)  # 节能率不为负（兜底时可能持平）
        else:
            saving = 0.0

        return OptimizeResult(
            task_id=task_id,
            status=status,
            chilled_water_temp=round(params["chilled_water_temp"], 2),
            chilled_pump_freq=round(params["chilled_pump_freq"], 2),
            cooling_pump_freq=round(params["cooling_pump_freq"], 2),
            cooling_tower_fan_freq=round(params["cooling_tower_fan_freq"], 2),
            predicted_power=round(predicted, 3),
            energy_saving_rate=round(saving, 2),
            duration=round(time.time() - start, 4),
            optimized_at=datetime.now(),
            remark=remark,
        )

    def _fallback_result(
        self, task_id: str, status: str, remark: str, start: float
    ) -> OptimizeResult:
        """无有效工况时的纯兜底结果（固定参数）。"""
        params = self._guard.fallback_params(remark)
        return OptimizeResult(
            task_id=task_id,
            status=status,
            chilled_water_temp=round(params["chilled_water_temp"], 2),
            chilled_pump_freq=round(params["chilled_pump_freq"], 2),
            cooling_pump_freq=round(params["cooling_pump_freq"], 2),
            cooling_tower_fan_freq=round(params["cooling_tower_fan_freq"], 2),
            predicted_power=0.0,
            energy_saving_rate=0.0,
            duration=round(time.time() - start, 4),
            optimized_at=datetime.now(),
            remark=remark,
        )
