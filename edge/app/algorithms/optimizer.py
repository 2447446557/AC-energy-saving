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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from itertools import product
from typing import Any

import numpy as np
from loguru import logger
from sko.PSO import PSO

from app.algorithms.constraints import VAR_ORDER, SafetyConstraints
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.core.config import get_business_config
from app.services.settings_config import get_merged_business_config
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
        pop: int | None = None,
        max_iter: int | None = None,
        w: float = 0.8,
        c1: float = 0.5,
        c2: float = 0.5,
        timeout_seconds: float | None = None,
        early_stop_precision: float | None = None,
        early_stop_patience: int | None = None,
        parallel_discrete: bool | None = None,
        parallel_workers: int | None = None,
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
        cfg = get_merged_business_config().get("optimize", {}) or {}
        pso_cfg = cfg.get("pso", {}) or {}

        self._pop = int(pop if pop is not None else pso_cfg.get("pop", 30))
        self._max_iter = int(max_iter if max_iter is not None else pso_cfg.get("max_iter", 45))
        self._w = w
        self._c1 = c1
        self._c2 = c2
        self._early_stop_precision = float(
            early_stop_precision
            if early_stop_precision is not None
            else pso_cfg.get("early_stop_precision", 1.0)
        )
        self._early_stop_patience = int(
            early_stop_patience
            if early_stop_patience is not None
            else pso_cfg.get("early_stop_patience", 5)
        )
        self._parallel_discrete = bool(
            parallel_discrete
            if parallel_discrete is not None
            else pso_cfg.get("parallel_discrete", True)
        )
        workers = int(
            parallel_workers
            if parallel_workers is not None
            else pso_cfg.get("parallel_workers", 1)
        )
        # 目标函数主要是 Python 层模型计算，线程过多会被 GIL 和调度开销拖慢。
        # 默认单 worker 更稳定；现场若确认并行收益，可通过配置显式调大。
        self._parallel_workers = workers if workers > 0 else 1

        if timeout_seconds is None:
            timeout_seconds = float(cfg.get("timeout_seconds", 60))
        self._timeout = timeout_seconds

    def apply_runtime_settings(self, config: dict | None = None) -> None:
        """热更新寻优任务参数（超时等），不重建 PSO 实例。"""
        from app.services.settings_config import get_merged_business_config

        cfg = config if config is not None else get_merged_business_config()
        optimize_cfg = cfg.get("optimize", {}) or {}
        try:
            self._timeout = float(optimize_cfg.get("timeout_seconds", self._timeout))
        except (TypeError, ValueError):
            pass
        pso_cfg = optimize_cfg.get("pso", {}) or {}
        for attr, key, cast in (
            ("_pop", "pop", int),
            ("_max_iter", "max_iter", int),
            ("_early_stop_precision", "early_stop_precision", float),
            ("_early_stop_patience", "early_stop_patience", int),
        ):
            if key in pso_cfg:
                try:
                    setattr(self, attr, cast(pso_cfg[key]))
                except (TypeError, ValueError):
                    pass

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
        self._guard.set_baseline(current_params)
        prev_chw_floor = self._constraints._chilled_water_temp_floor
        self._constraints.set_chilled_water_temp_floor(
            self._resolve_chilled_water_temp_floor(request)
        )
        try:
            try:
                model_baseline = self._energy_model.predict(
                    data, current_params
                ).total_power
            except Exception as e:
                logger.error(f"基线能耗计算失败: {e}")
                model_baseline = 0.0
            measured_total = float(data.total_power or 0.0)
            baseline_power = (
                measured_total if measured_total > 1e-6 else model_baseline
            )

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
            has_load = data.indoor_load > 10
            smoothed["chilled_pump_count"] = self._snap_pump_count(
                "chilled", best_params.get("chilled_pump_count", 1), require_positive=has_load
            )
            smoothed["cooling_pump_count"] = self._snap_pump_count(
                "cooling", best_params.get("cooling_pump_count", 1), require_positive=has_load
            )
            smoothed["cooling_tower_count"] = self._snap_tower_count(
                data, best_params.get("cooling_tower_count", 5), require_positive=has_load
            )

            smoothed, guard_remark = self._prefer_current_if_no_saving(
                data, current_params, smoothed
            )

            remark = "" if converged else "达到最大迭代（未提前收敛）"
            if guard_remark:
                remark = f"{remark}; {guard_remark}" if remark else guard_remark
            return self._build_result(
                task_id, "success", data, smoothed, start, baseline_power, remark=remark
            )
        finally:
            self._constraints.set_chilled_water_temp_floor(prev_chw_floor)

    # ---------- PSO 执行 ----------

    def _resolve_chilled_water_temp_floor(
        self, request: OptimizeRequest
    ) -> float | None:
        """解析本次寻优冷水温度下限：请求显式指定优先，否则用系统配置下限。"""
        if request.chilled_water_temp_min is not None:
            return request.chilled_water_temp_min
        try:
            return float(self._constraints.bounds["chilled_water_temp"][0])
        except (TypeError, ValueError, KeyError, IndexError):
            return None

    def _run_pso_with_timeout(
        self, data: DeviceData
    ) -> tuple[dict[str, float] | None, float | None, bool]:
        """在子线程/并行 worker 中运行 PSO，主线程按墙钟超时等待。"""
        lb, ub = self._constraints.bounds_array()
        discrete_options = self._discrete_options(data)
        if not discrete_options:
            discrete_options = [{}]

        if len(discrete_options) == 1:
            extra = discrete_options[0]
            objective = self._make_objective(data, fixed_extra=extra)
            return self._run_pso_for_objective(
                lb=lb,
                ub=ub,
                full_objective=objective,
                fixed_extra=extra,
                sync=False,
            )

        if not self._parallel_discrete:
            best_params: dict[str, float] | None = None
            best_y: float | None = None
            converged = False
            for extra in discrete_options:
                objective = self._make_objective(data, fixed_extra=extra)
                params, y, scheme_converged = self._run_pso_for_objective(
                    lb=lb,
                    ub=ub,
                    full_objective=objective,
                    fixed_extra=extra,
                    sync=False,
                )
                if params is None or y is None:
                    continue
                if best_y is None or y < best_y:
                    best_params = params
                    best_y = y
                    converged = scheme_converged
            return best_params, best_y, converged

        best_params: dict[str, float] | None = None
        best_y: float | None = None
        converged = False
        workers = min(len(discrete_options), self._parallel_workers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._run_pso_for_objective,
                    lb,
                    ub,
                    self._make_objective(data, fixed_extra=extra),
                    extra,
                    True,
                ): extra
                for extra in discrete_options
            }
            try:
                for future in as_completed(futures, timeout=self._timeout):
                    try:
                        params, y, scheme_converged = future.result()
                    except Exception as e:
                        logger.debug(f"离散方案 PSO 失败: {e}")
                        continue
                    if params is None or y is None:
                        continue
                    if best_y is None or y < best_y:
                        best_params = params
                        best_y = y
                        converged = scheme_converged
            except TimeoutError:
                logger.error(f"并行 PSO 寻优超时 (>{self._timeout}s)")

        return best_params, best_y, converged

    @staticmethod
    def _discrete_options(data: DeviceData) -> list[dict[str, int]]:
        return [
            {
                "chilled_pump_count": chilled_count,
                "cooling_pump_count": cooling_count,
                "cooling_tower_count": tower_count,
            }
            for chilled_count, cooling_count, tower_count in product(
                PSOOptimizer._pump_schemes("chilled"),
                PSOOptimizer._pump_schemes("cooling"),
                PSOOptimizer._cooling_tower_schemes(data),
            )
        ]

    def _run_pso_for_objective(
        self,
        lb: list[float],
        ub: list[float],
        full_objective,
        fixed_extra: dict[str, float],
        sync: bool = False,
    ) -> tuple[dict[str, float] | None, float | None, bool]:
        """对单个离散方案执行 PSO。"""
        fixed_values = [lb[i] for i in range(len(VAR_ORDER))]
        free_indices = [i for i, (lo, hi) in enumerate(zip(lb, ub)) if hi > lo]
        fixed_indices = [i for i, (lo, hi) in enumerate(zip(lb, ub)) if hi <= lo]

        def expand_vector(x) -> np.ndarray:
            full = np.asarray(fixed_values, dtype=float)
            x = np.asarray(x, dtype=float).ravel()
            for pos, idx in enumerate(free_indices):
                full[idx] = x[pos]
            for idx in fixed_indices:
                full[idx] = lb[idx]
            return full

        def objective(x) -> float:
            return full_objective(expand_vector(x))

        if not free_indices:
            params = {var: float(lb[i]) for i, var in enumerate(VAR_ORDER)}
            params.update(fixed_extra)
            return params, float(full_objective(np.asarray(lb, dtype=float))), True

        result: dict[str, Any] = {}

        def _worker() -> None:
            try:
                pso = PSO(
                    func=objective,
                    n_dim=len(free_indices),
                    pop=self._pop,
                    max_iter=self._max_iter,
                    lb=[lb[i] for i in free_indices],
                    ub=[ub[i] for i in free_indices],
                    w=self._w,
                    c1=self._c1,
                    c2=self._c2,
                )
                if self._early_stop_precision > 0:
                    pso.run(
                        precision=self._early_stop_precision,
                        N=self._early_stop_patience,
                    )
                else:
                    pso.run()
                best_x = expand_vector(pso.gbest_x)
                best_y = float(np.asarray(pso.gbest_y, dtype=float).ravel()[0])
                result["params"] = {
                    var: float(best_x[i]) for i, var in enumerate(VAR_ORDER)
                }
                result["params"].update(fixed_extra)
                result["y"] = best_y
                result["converged"] = self._detect_convergence(pso)
            except Exception as e:
                result["error"] = e

        if sync:
            _worker()
            if "error" in result:
                raise result["error"]
            return (
                result.get("params"),
                result.get("y"),
                result.get("converged", False),
            )

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(self._timeout)

        if worker.is_alive():
            logger.error(f"PSO 寻优超时 (>{self._timeout}s)")
            return None, None, False
        if "error" in result:
            raise result["error"]
        return result.get("params"), result.get("y"), result.get("converged", False)

    def _make_objective(
        self,
        data: DeviceData,
        fixed_extra: dict | None = None,
    ):
        """构造 PSO 目标函数（最小化）：能耗 + 舒适惩罚 + 硬约束惩罚。"""
        energy_model = self._energy_model
        constraints = self._constraints
        fixed_extra = fixed_extra or {}
        cache: dict[tuple[Any, ...], float] = {}
        model_context = self._model_context(data, fixed_extra)

        def evaluate(params: dict[str, float]) -> float:
            cache_key = tuple(
                round(float(params.get(var, 0.0)), 3)
                for var in (
                    *VAR_ORDER,
                    "chilled_pump_count",
                    "cooling_pump_count",
                    "cooling_tower_count",
                )
            )
            if cache_key in cache:
                return cache[cache_key]
            try:
                breakdown = energy_model.predict(data, {**params, **model_context})
                cost = breakdown.total_power
                cost += _COMFORT_PENALTY_WEIGHT * constraints.comfort_penalty(
                    breakdown.predicted_indoor_temp
                )
                cost += _HARD_PENALTY_WEIGHT * constraints.hard_violation(params)
                if data.indoor_load > 10 and (
                    params.get("chilled_pump_count", 1) <= 0
                    or params.get("cooling_pump_count", 1) <= 0
                ):
                    cost += _HARD_PENALTY_WEIGHT
                if params.get("cooling_tower_count", 5) <= 0 and data.indoor_load > 10:
                    cost += _HARD_PENALTY_WEIGHT
                if not np.isfinite(cost):
                    cost = _HARD_PENALTY_WEIGHT
                else:
                    cost = float(cost)
            except Exception:
                cost = _HARD_PENALTY_WEIGHT
            cache[cache_key] = cost
            return cost

        def objective(x) -> float:
            x = np.asarray(x, dtype=float).ravel()
            base_params = {var: float(x[i]) for i, var in enumerate(VAR_ORDER)}
            return evaluate({**base_params, **fixed_extra})

        return objective

    def _model_context(
        self,
        data: DeviceData,
        fixed_extra: dict[str, float],
    ) -> dict[str, Any]:
        """预计算一次寻优目标函数内重复使用的模型上下文。

        PSO 会评估大量粒子；当前台数、基线参数和对应现场配置在同一个
        离散方案内不变，提前计算可避免每次粒子评估重复读取/推断。
        """
        context: dict[str, Any] = {}
        try:
            from app.services.power_baseline import current_operating_params, infer_active_counts

            raw_data = data.model_dump()
            active_counts = infer_active_counts(raw_data)
            baseline_params = current_operating_params(raw_data)
            context["_active_counts"] = active_counts
            context["_baseline_params"] = baseline_params
            context["_site_params"] = self._energy_model._params_for_site(
                chilled_pump_count=int(
                    fixed_extra.get(
                        "chilled_pump_count",
                        active_counts.get("chilled_pump_count", 1),
                    )
                ),
                cooling_pump_count=int(
                    fixed_extra.get(
                        "cooling_pump_count",
                        active_counts.get("cooling_pump_count", 1),
                    )
                ),
                tower_count=int(
                    fixed_extra.get(
                        "cooling_tower_count",
                        active_counts.get("cooling_tower_count", 5),
                    )
                ),
            )
            context["_baseline_site_params"] = self._energy_model._params_for_site(
                chilled_pump_count=int(baseline_params.get("chilled_pump_count", 1)),
                cooling_pump_count=int(baseline_params.get("cooling_pump_count", 1)),
                tower_count=int(baseline_params.get("cooling_tower_count", 5)),
            )
        except Exception as e:
            logger.debug(f"预计算寻优模型上下文失败，回退逐次推断: {e}")
        return context

    @staticmethod
    def _pump_schemes(kind: str) -> list[int]:
        """读取冷冻泵/冷却泵离散开启方案。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            pump = eq.chilled_pump if kind == "chilled" else eq.cooling_pump
            schemes = sorted(
                {max(0, min(int(s), pump.count)) for s in pump.active_count_schemes}
            )
            return schemes or [pump.count]
        except Exception:
            return [1]

    @staticmethod
    def _cooling_tower_schemes(data: DeviceData) -> list[int]:
        """读取冷却塔离散开启方案；有负荷时保留 0 方案但由目标函数强惩罚。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            enabled_count = len([tower for tower in eq.cooling_towers if tower.enabled])
            schemes = sorted({max(0, int(s)) for s in eq.cooling_tower_schemes})
            return schemes or [enabled_count]
        except Exception:
            return [5]

    @staticmethod
    def _nearest_scheme(value: Any, schemes: list[int], require_positive: bool = False) -> int:
        """将离散台数吸附到配置允许方案，避免输出非配置值。"""
        allowed = sorted({int(s) for s in schemes})
        if require_positive:
            positive = [s for s in allowed if s > 0]
            if positive:
                allowed = positive
        if not allowed:
            return 1 if require_positive else 0
        try:
            target = int(round(float(value)))
        except (TypeError, ValueError):
            target = allowed[-1]
        return min(allowed, key=lambda s: (abs(s - target), s))

    @staticmethod
    def _snap_pump_count(kind: str, value: Any, require_positive: bool = False) -> int:
        return PSOOptimizer._nearest_scheme(
            value,
            PSOOptimizer._pump_schemes(kind),
            require_positive=require_positive,
        )

    @staticmethod
    def _snap_tower_count(
        data: DeviceData, value: Any, require_positive: bool = False
    ) -> int:
        return PSOOptimizer._nearest_scheme(
            value,
            PSOOptimizer._cooling_tower_schemes(data),
            require_positive=require_positive,
        )

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

    def _prefer_current_if_no_saving(
        self,
        data: DeviceData,
        current_params: dict[str, float],
        candidate: dict[str, float],
    ) -> tuple[dict[str, float], str]:
        """当前工况已舒适且实测功耗不高于推荐时，保持现有设定。"""
        measured = float(data.total_power or 0.0)
        if measured <= 1e-6:
            return candidate, ""
        try:
            current_bd = self._energy_model.predict(data, current_params)
            candidate_bd = self._energy_model.predict(data, candidate)
        except Exception:
            return candidate, ""
        if self._constraints.comfort_penalty(current_bd.predicted_indoor_temp) > 0.0:
            return candidate, ""
        if candidate_bd.total_power < measured - 0.5:
            return candidate, ""
        kept = self._constraints.clip(current_params)
        return kept, "当前工况已达舒适且功耗不高于推荐，保持现有设定"

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
        from app.services.power_baseline import current_operating_params

        raw = current_operating_params(data.model_dump())
        return {k: float(v) for k, v in raw.items()}

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
            breakdown = self._energy_model.predict(data, params)
            predicted = breakdown.total_power
        except Exception:
            breakdown = None
            predicted = 0.0

        if baseline_power > 1e-6 and predicted > 0:
            saving = (baseline_power - predicted) / baseline_power * 100.0
            saving = max(saving, 0.0)  # 节能率不为负（兜底时可能持平）
        else:
            saving = 0.0

        chilled_pump_count = int(params.get("chilled_pump_count", 1))
        cooling_pump_count = int(params.get("cooling_pump_count", 1))
        tower_count = int(params.get("cooling_tower_count", 5))
        chilled_pump_power = (
            breakdown.chilled_pump_power if breakdown else self._pump_power("chilled", chilled_pump_count, params["chilled_pump_freq"])
        )
        cooling_pump_power = (
            breakdown.cooling_pump_power if breakdown else self._pump_power("cooling", cooling_pump_count, params["cooling_pump_freq"])
        )
        tower_power = (
            breakdown.cooling_tower_fan_power if breakdown else self._cooling_tower_power(tower_count)
        )
        return OptimizeResult(
            task_id=task_id,
            status=status,
            chilled_water_temp=round(params["chilled_water_temp"], 2),
            chilled_pump_freq=round(params["chilled_pump_freq"], 2),
            chilled_pump_count=chilled_pump_count,
            chilled_pump_power=round(chilled_pump_power, 3),
            cooling_pump_freq=round(params["cooling_pump_freq"], 2),
            cooling_pump_count=cooling_pump_count,
            cooling_pump_power=round(cooling_pump_power, 3),
            cooling_tower_fan_freq=round(params["cooling_tower_fan_freq"], 2),
            cooling_tower_count=tower_count,
            cooling_tower_power=round(tower_power, 3),
            predicted_power=round(predicted, 3),
            baseline_power=round(baseline_power, 3),
            predicted_indoor_temp=(
                round(breakdown.predicted_indoor_temp, 2) if breakdown else 0.0
            ),
            predicted_chiller_power=(
                round(breakdown.chiller_power, 3) if breakdown else 0.0
            ),
            predicted_cooling_water_temp=(
                round(breakdown.cooling_water_temp, 2) if breakdown else 0.0
            ),
            predicted_cop=round(breakdown.cop, 3) if breakdown else 0.0,
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
        params["chilled_pump_count"] = self._default_pump_count("chilled")
        params["cooling_pump_count"] = self._default_pump_count("cooling")
        params["cooling_tower_count"] = 5
        return OptimizeResult(
            task_id=task_id,
            status=status,
            chilled_water_temp=round(params["chilled_water_temp"], 2),
            chilled_pump_freq=round(params["chilled_pump_freq"], 2),
            chilled_pump_count=int(params["chilled_pump_count"]),
            chilled_pump_power=round(
                self._pump_power(
                    "chilled",
                    int(params["chilled_pump_count"]),
                    params["chilled_pump_freq"],
                ),
                3,
            ),
            cooling_pump_freq=round(params["cooling_pump_freq"], 2),
            cooling_pump_count=int(params["cooling_pump_count"]),
            cooling_pump_power=round(
                self._pump_power(
                    "cooling",
                    int(params["cooling_pump_count"]),
                    params["cooling_pump_freq"],
                ),
                3,
            ),
            cooling_tower_fan_freq=round(params["cooling_tower_fan_freq"], 2),
            cooling_tower_count=5,
            cooling_tower_power=round(self._cooling_tower_power(5), 3),
            predicted_power=0.0,
            energy_saving_rate=0.0,
            duration=round(time.time() - start, 4),
            optimized_at=datetime.now(),
            remark=remark,
        )

    @staticmethod
    def _default_pump_count(kind: str) -> int:
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            pump = eq.chilled_pump if kind == "chilled" else eq.cooling_pump
            return max(0, int(pump.count))
        except Exception:
            return 1

    @staticmethod
    def _pump_power(kind: str, count: int, freq: float) -> float:
        """按推荐开启台数和频率计算水泵功率。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            pump = eq.chilled_pump if kind == "chilled" else eq.cooling_pump
            count = max(0, min(int(count), pump.count))
            ratio = max(float(freq), 0.0) / 50.0
            return count * pump.motor_power_kw * (ratio ** 3)
        except Exception:
            return 0.0

    @staticmethod
    def _cooling_tower_power(count: int) -> float:
        """按推荐开启台数计算冷却塔定频总功率。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            enabled = [tower for tower in eq.cooling_towers if tower.enabled]
            count = max(0, min(int(count), len(enabled)))
            return sum(tower.motor_power_kw for tower in enabled[:count])
        except Exception:
            return 0.0
