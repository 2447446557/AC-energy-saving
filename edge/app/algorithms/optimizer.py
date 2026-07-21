"""PSO 粒子群寻优模块（项目核心壁垒 · 工业级封装）

基于开源库 scikit-opt 封装工业级 PSO 粒子群寻优，实现多变量协同寻优
（对应设计文档 4.6 节、需求文档 3.1 节）。

可调变量（随室内外温度变化）：
    冷水出水温度（查表 ± 微调）、冷冻泵频率/台数、冷却泵频率/台数

固定不调（现场定额/保持当前）：
    主机负荷率、冷却塔频率、冷却塔台数与功率

注：VAR_ORDER 仍含负荷/塔频维，但搜索上下界钳为当前值（等效固定）。
    冷水出水温度 = 查表/实测基准 + 微调量；泵频率下限随室外温度分档抬升。

优化目标（二选一）：
    - total_power：系统总能耗最小（默认）
    - min_cooling_water：冷却回水（冷却水温度）最低（定频塔靠台数+冷却泵频）

目标函数 = 基础目标（总功率 或 冷却水温×权重）
          + 舒适度惩罚（室内温度越界，软约束）
          + 定值变化 / 欠供冷 / PLR 甜点软惩罚（ChillStream 可借鉴；回水模式可弱化泵降频偏好）
          + 硬约束越界极大惩罚（保险，正常被 lb/ub 拦截）

工程化鲁棒设计：
- 寻优超时：子线程执行 + 墙钟超时，超时立即兜底，杜绝阻塞主调度。
- 收敛失败：结果非有限 / 未改进 / 抛异常，一律降级为兜底。
- 数据熔断：上游数据清洗判定连续异常时，直接切回安全固定参数。
- 参数平滑：最优解经阶梯平滑后输出，保护设备；结果区分 PSO 推荐值与实发值。
- 短时负荷 EWMA 预测后再寻优；可选 LightGBM 旁路节能对照。
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

from app.algorithms.chillstream_features import (
    FALLBACK_RULES,
    LoadForecastState,
    blackbox_baseline_power,
    merge_feature_config,
    plr_sweet_spot_penalty,
    setpoint_change_penalty,
    unmet_cooling_penalty,
)
from app.algorithms.constraints import VAR_ORDER, SafetyConstraints
from app.algorithms.energy_model import ACEnergyModel
from app.algorithms.fallback import SafeOutputGuard
from app.algorithms.indoor_temp import control_indoor_temp
from app.core.config import get_business_config
from app.services.settings_config import get_merged_business_config
from app.schemas.device import DeviceData
from app.schemas.optimize import (
    OptimizeObjectiveMode,
    OptimizeRequest,
    OptimizeResult,
)

OBJECTIVE_TOTAL_POWER: OptimizeObjectiveMode = "total_power"
OBJECTIVE_MIN_COOLING_WATER: OptimizeObjectiveMode = "min_cooling_water"
# 冷却回水模式下将 ℃ 放大，使 0.1℃ 差与软惩罚同量级可比较
_CW_TEMP_COST_WEIGHT = 100.0

# 目标函数惩罚系数（远大于典型能耗量级，使非法/越界解被 PSO 自动抛弃）
_HARD_PENALTY_WEIGHT = 1.0e6
# 预测室内越出适宜温度时的软惩罚权重：优先拉回舒适带，但仍允许在无可行舒适解时收敛
_COMFORT_PENALTY_WEIGHT = 500.0
# 舒适裕量内：泵频每高过搜索下限 1Hz，目标函数略加点偏置，引导向最低频率靠拢省辅机电
_PUMP_FLOOR_BIAS_KW_PER_HZ = 0.08


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
        self._load_forecast = LoadForecastState()
        self._inspired_cfg = merge_feature_config(cfg.get("inspired"))

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
        self._inspired_cfg = merge_feature_config(optimize_cfg.get("inspired"))

    def _inspired_config(self) -> dict[str, Any]:
        return self._inspired_cfg if isinstance(self._inspired_cfg, dict) else merge_feature_config(None)

    # ---------- IOptimizer 协议实现 ----------

    def optimize(self, request: OptimizeRequest) -> OptimizeResult:
        """执行一次寻优，返回带兜底保障的最优控制参数。"""
        start = time.time()
        task_id = str(uuid.uuid4())
        forecast_indoor_load = 0.0
        mode: OptimizeObjectiveMode = getattr(request, "mode", None) or OBJECTIVE_TOTAL_POWER
        if mode not in (OBJECTIVE_TOTAL_POWER, OBJECTIVE_MIN_COOLING_WATER):
            mode = OBJECTIVE_TOTAL_POWER

        # --- 解析工况数据 ---
        try:
            data = DeviceData(**request.device_data)
        except Exception as e:
            logger.error(f"寻优输入解析失败: {e}")
            return self._fallback_result(
                task_id,
                "failed",
                f"输入解析失败:{e}",
                start,
                fallback_rule="parse_error",
            )

        # --- 数据熔断优先级最高：连续异常直接切固定参数 ---
        if self._data_cleaner is not None and getattr(
            self._data_cleaner, "is_circuit_broken", lambda: False
        )():
            params = self._guard.fallback_params("数据熔断")
            return self._build_result(
                task_id,
                "failed",
                data,
                params,
                start,
                remark="数据连续异常熔断，切回安全固定参数",
                fallback_rule="circuit_break",
            )

        # --- 基线能耗（当前实测控制参数下的能耗，用于计算节能率） ---
        # 单次寻优内固定现场设备参数快照，避免并行读库失败回退到默认冷量
        self._run_site_params_cache: dict[tuple[int, int, int], Any] = {}
        try:
            from app.algorithms.energy_model import _load_site_equipment

            _load_site_equipment()
            # 预热各离散台数方案的现场参数，保证并行 PSO 全程使用同一套冷量标定
            for extra in self._discrete_options(data) or [{}]:
                self._site_params_for_counts(
                    chilled_pump_count=int(extra.get("chilled_pump_count", 1)),
                    cooling_pump_count=int(extra.get("cooling_pump_count", 1)),
                    tower_count=int(extra.get("cooling_tower_count", 5)),
                )
        except Exception:
            pass
        # 冷冻/冷却泵功率不采信输入 kW，统一按频率立方律重算后再寻优
        data = self._normalize_pump_powers_from_freq(data)
        # 首轮即写入主机功率锚点，避免无 reference 时比例缩放到 0.65 地板制造虚假节能
        if float(getattr(data, "chiller_power_reference", 0.0) or 0.0) <= 0:
            chiller_now = float(data.chiller_power or 0.0)
            if chiller_now > 0:
                data = data.model_copy(
                    update={
                        "chiller_power_reference": chiller_now,
                        "chiller_power_reference_outdoor_temp": float(
                            data.outdoor_temp or 0.0
                        ),
                        "chiller_power_reference_outdoor_humidity": float(
                            data.outdoor_humidity or 0.0
                        ),
                    }
                )
        current_params = self._current_params(data)
        outdoor_temp = float(data.outdoor_temp or 30.0)
        measured_load = float(data.chiller_load or 0.0)
        bounds_kw = self._bounds_kw_for_data(data)
        current_full = self._finalize_control_params(data, current_params)
        self._guard.set_bounds_context(
            outdoor_temp=outdoor_temp,
            measured_load_pct=measured_load,
            **bounds_kw,
        )
        self._guard.set_baseline(current_full)

        if data.indoor_load > 10:
            has_power = (
                float(data.total_power or 0.0) > 1e-6
                or float(data.chiller_power or 0.0) > 1e-6
            )
            if not has_power:
                return self._build_result(
                    task_id,
                    "success",
                    data,
                    current_full,
                    start,
                    0.0,
                    remark="缺实测功率，保持现有设定",
                    fallback_rule="no_power",
                    forecast_indoor_load=forecast_indoor_load,
                )

        # --- 短时负荷 EWMA：定时闭环用；force/批量跳过，避免行间串味 ---
        inspired = self._inspired_config()
        opt_data = data
        measured_indoor_load = float(data.indoor_load or 0.0)
        use_load_forecast = (
            bool(inspired.get("enabled"))
            and bool(inspired.get("load_forecast_enabled"))
            and not bool(getattr(request, "force", False))
        )
        if use_load_forecast:
            forecast_indoor_load = self._load_forecast.update(
                measured_indoor_load, float(inspired.get("load_forecast_alpha", 0.35))
            )
            if abs(forecast_indoor_load - measured_indoor_load) > 1e-6:
                opt_data = data.model_copy(update={"indoor_load": forecast_indoor_load})
            else:
                forecast_indoor_load = measured_indoor_load
        else:
            forecast_indoor_load = measured_indoor_load

        try:
            try:
                model_baseline = self._predict_with_context(
                    data, current_full
                ).total_power
            except Exception as e:
                logger.error(f"基线能耗计算失败: {e}")
                model_baseline = 0.0
            measured_total = float(data.total_power or 0.0)
            policy_baseline = self._policy_reference_baseline(data, current_full)
            # 节能率基线：优先用输入实测总功率（与表格「输入总功率kW」同口径）
            baseline_power = (
                measured_total
                if measured_total > 1e-6
                else (
                    policy_baseline
                    if policy_baseline > 1e-6
                    else model_baseline
                )
            )

            # --- 运行 PSO（带超时）；负荷预测作用于 opt_data ---
            try:
                best_params, best_y, converged = self._run_pso_with_timeout(
                    opt_data, mode=mode
                )
            except Exception as e:
                logger.error(f"PSO 寻优异常: {e}", exc_info=True)
                params = self._guard.fallback_params(f"寻优异常:{e}")
                return self._build_result(
                    task_id,
                    "failed",
                    data,
                    params,
                    start,
                    baseline_power,
                    remark=f"寻优异常，已兜底:{e}",
                    fallback_rule="exception",
                    forecast_indoor_load=forecast_indoor_load,
                )

            if best_params is None:
                # 超时
                params = self._guard.fallback_params("寻优超时")
                return self._build_result(
                    task_id,
                    "timeout",
                    data,
                    params,
                    start,
                    baseline_power,
                    remark=f"寻优超时(>{self._timeout}s)，已兜底",
                    fallback_rule="timeout",
                    forecast_indoor_load=forecast_indoor_load,
                )

            # --- 收敛/合法性校验 ---
            # 注意：舒适软惩罚可能使目标值 > _HARD_PENALTY_WEIGHT，不能仅凭 best_y 判非法；
            # 以参数硬约束校验 + 硬越界量为准。
            hard_bad = False
            if best_params is not None:
                try:
                    hard_bad = (
                        self._constraints.hard_violation(
                            best_params,
                            outdoor_temp,
                            measured_load,
                            **bounds_kw,
                        )
                        > 1e-9
                    )
                except Exception:
                    hard_bad = True
            if (
                best_params is None
                or best_y is None
                or not np.isfinite(best_y)
                or hard_bad
                or not self._constraints.validate(
                    best_params, outdoor_temp, measured_load, **bounds_kw
                )
            ):
                params = self._guard.fallback_params("收敛失败/结果非法")
                return self._build_result(
                    task_id,
                    "failed",
                    data,
                    params,
                    start,
                    baseline_power,
                    remark="寻优收敛失败或结果非法，已兜底",
                    fallback_rule="invalid",
                    forecast_indoor_load=forecast_indoor_load,
                )

            # --- 有效解：登记 + 阶梯平滑输出 ---
            self._guard.register_good(best_params)
            recommended = self._finalize_control_params(data, best_params)
            recommended["chilled_pump_count"] = self._snap_pump_count(
                "chilled",
                best_params.get("chilled_pump_count", 1),
                require_positive=data.indoor_load > 10,
            )
            recommended["cooling_pump_count"] = self._snap_pump_count(
                "cooling",
                best_params.get("cooling_pump_count", 1),
                require_positive=data.indoor_load > 10,
            )
            recommended["cooling_tower_count"] = self._resolve_tower_count(
                data, best_params, mode
            )
            urgent = self._is_comfort_at_risk(data)
            # 手动寻优/多次闭环模拟（force=True）跳过阶梯平滑，直接展示 PSO 最优解；
            # 现场自动下发仍走平滑，保护设备。
            if getattr(request, "force", False):
                smoothed = {
                    var: float(best_params[var])
                    for var in VAR_ORDER
                    if var in best_params
                }
                smoothed = self._constraints.clip(
                    smoothed, outdoor_temp, measured_load, **bounds_kw
                )
            else:
                smoothed = self._guard.smooth(best_params, urgent=urgent)
                smoothed = self._constraints.clip(
                    smoothed, outdoor_temp, measured_load, **bounds_kw
                )
            smoothed = self._finalize_control_params(data, smoothed)
            has_load = data.indoor_load > 10
            smoothed["chilled_pump_count"] = self._snap_pump_count(
                "chilled", best_params.get("chilled_pump_count", 1), require_positive=has_load
            )
            smoothed["cooling_pump_count"] = self._snap_pump_count(
                "cooling", best_params.get("cooling_pump_count", 1), require_positive=has_load
            )
            smoothed["cooling_tower_count"] = self._resolve_tower_count(
                data, best_params, mode
            )
            # 回水最低模式：不套用「无节能则粘住当前」的能耗闸；总电模式保持原逻辑
            gate_remark = ""
            guard_remark = ""
            if mode == OBJECTIVE_MIN_COOLING_WATER:
                smoothed, gate_remark = self._apply_output_hard_gates(
                    data, current_full, smoothed, mode=mode
                )
                mode_note = "目标=冷却回水最低"
                remark_prefix = mode_note
            else:
                smoothed, guard_remark = self._prefer_current_if_no_saving(
                    data, current_full, smoothed
                )
                smoothed, gate_remark = self._apply_output_hard_gates(
                    data, current_full, smoothed, mode=mode
                )
                if self._constraints.is_in_comfort_band(control_indoor_temp(data)):
                    smoothed = self._refine_for_policy_saving(
                        data, current_full, smoothed
                    )
                    smoothed, gate_remark2 = self._apply_output_hard_gates(
                        data, current_full, smoothed, mode=mode
                    )
                    if gate_remark2:
                        gate_remark = (
                            f"{gate_remark}; {gate_remark2}" if gate_remark else gate_remark2
                        )
                remark_prefix = "目标=系统总电最低"

            if measured_total <= 1e-6:
                try:
                    baseline_params = dict(current_full)
                    for key in (
                        "chilled_pump_count",
                        "cooling_pump_count",
                        "cooling_tower_count",
                    ):
                        if key in smoothed:
                            baseline_params[key] = smoothed[key]
                    model_baseline = self._predict_with_context(
                        data, baseline_params
                    ).total_power
                    baseline_power = model_baseline
                except Exception as e:
                    logger.debug(f"重算基线能耗失败，沿用初始值: {e}")

            remark = "" if converged else "达到最大迭代（未提前收敛）"
            remark = f"{remark_prefix}; {remark}" if remark else remark_prefix
            if guard_remark:
                remark = f"{remark}; {guard_remark}" if remark else guard_remark
            if gate_remark:
                remark = f"{remark}; {gate_remark}" if remark else gate_remark
            if (
                use_load_forecast
                and abs(forecast_indoor_load - measured_indoor_load) > 0.5
            ):
                load_note = (
                    f"负荷EWMA={forecast_indoor_load:.1f}kW"
                    f"(实测{measured_indoor_load:.1f})"
                )
                remark = f"{remark}; {load_note}" if remark else load_note

            bb_power, bb_saving = self._blackbox_compare(
                data, current_full, smoothed, inspired
            )
            return self._build_result(
                task_id,
                "success",
                data,
                smoothed,
                start,
                baseline_power,
                remark=remark,
                recommended=recommended,
                forecast_indoor_load=forecast_indoor_load,
                blackbox_baseline_power=bb_power,
                blackbox_saving_rate=bb_saving,
                fallback_rule="ok",
                objective_mode=mode,
            )
        finally:
            self._run_site_params_cache = {}

    def _blackbox_compare(
        self,
        data: DeviceData,
        current: dict[str, float],
        applied: dict[str, float],
        inspired: dict[str, Any],
    ) -> tuple[float, float]:
        """LightGBM 旁路：当前设定 vs 实发方案的黑盒功率对照。"""
        if not inspired.get("enabled") or not inspired.get("blackbox_baseline_enabled"):
            return 0.0, 0.0
        cur_p, ok1 = blackbox_baseline_power(data, current)
        opt_p, ok2 = blackbox_baseline_power(data, applied)
        if not (ok1 and ok2) or cur_p <= 1e-6:
            return 0.0, 0.0
        saving = (cur_p - opt_p) / cur_p * 100.0
        return round(cur_p, 3), round(saving, 2)

    # ---------- PSO 执行 ----------

    def _run_pso_with_timeout(
        self,
        data: DeviceData,
        mode: OptimizeObjectiveMode = OBJECTIVE_TOTAL_POWER,
    ) -> tuple[dict[str, float] | None, float | None, bool]:
        """在子线程/并行 worker 中运行 PSO，主线程按墙钟超时等待。"""
        lb, ub = self._constraints.bounds_array(
            float(data.outdoor_temp or 30.0),
            float(data.chiller_load or 0.0),
            **self._bounds_kw_for_data(data),
        )
        discrete_options = self._discrete_options(data, mode=mode)
        if not discrete_options:
            discrete_options = [{}]

        if len(discrete_options) == 1:
            extra = discrete_options[0]
            objective = self._make_objective(data, fixed_extra=extra, mode=mode)
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
                objective = self._make_objective(data, fixed_extra=extra, mode=mode)
                params, y, scheme_converged = self._run_pso_for_objective(
                    lb=lb,
                    ub=ub,
                    full_objective=objective,
                    fixed_extra=extra,
                    sync=False,
                )
                if params is None or y is None:
                    continue
                # 用统一目标函数重评，避免并行/缓存导致 y 与 params 口径不一致
                y = float(objective([params[v] for v in VAR_ORDER]))
                if best_y is None or y < best_y:
                    best_params = params
                    best_y = y
                    converged = scheme_converged
            return best_params, best_y, converged

        # 并行离散：各方案独立寻优后，主线程用统一目标函数重评再比较
        results: list[tuple[dict[str, float], float, bool, dict[str, int]]] = []
        workers = min(len(discrete_options), self._parallel_workers)

        pool = ThreadPoolExecutor(max_workers=workers)
        futures = {
            pool.submit(
                self._run_pso_for_objective,
                lb,
                ub,
                self._make_objective(data, fixed_extra=extra, mode=mode),
                extra,
                True,
            ): extra
            for extra in discrete_options
        }
        timed_out = False
        try:
            for future in as_completed(futures, timeout=self._timeout):
                extra = futures[future]
                try:
                    params, y, scheme_converged = future.result()
                except Exception as e:
                    logger.debug(f"离散方案 PSO 失败: {e}")
                    continue
                if params is None or y is None:
                    continue
                results.append((params, float(y), scheme_converged, extra))
        except TimeoutError:
            logger.error(f"并行 PSO 寻优超时 (>{self._timeout}s)")
            timed_out = True
            for future in futures:
                future.cancel()
        # 超时时不阻塞等待 worker 完成（cancel_futures 取消排队任务，
        # 运行中的 daemon 线程会自行结束），正常完成时等待收尾。
        pool.shutdown(wait=not timed_out, cancel_futures=timed_out)

        best_params = None
        best_y = None
        converged = False
        for params, _y, scheme_converged, extra in results:
            objective = self._make_objective(data, fixed_extra=extra, mode=mode)
            try:
                y = float(objective([params[v] for v in VAR_ORDER]))
            except Exception:
                continue
            if best_y is None or y < best_y:
                best_params = params
                best_y = y
                converged = scheme_converged
        return best_params, best_y, converged

    @staticmethod
    def _resolve_tower_count(
        data: DeviceData,
        params: dict[str, Any] | None,
        mode: OptimizeObjectiveMode,
    ) -> int:
        """总电模式锁定当前塔台数；回水最低模式采用寻优离散方案台数。"""
        if mode == OBJECTIVE_MIN_COOLING_WATER and params:
            schemes = PSOOptimizer._cooling_tower_schemes(data)
            raw = params.get("cooling_tower_count")
            if raw is not None:
                return PSOOptimizer._nearest_scheme(raw, schemes, require_positive=True)
        return PSOOptimizer._fixed_tower_count(data)

    @staticmethod
    def _fixed_tower_count(data: DeviceData) -> int:
        """冷却塔台数定额：保持当前运行台数，不参与离散寻优。"""
        try:
            from app.services.power_baseline import infer_active_counts

            counts = infer_active_counts(data.model_dump())
            n = int(counts.get("cooling_tower_count") or 0)
            if n > 0:
                return n
        except Exception:
            pass
        schemes = PSOOptimizer._cooling_tower_schemes(data)
        return schemes[-1] if schemes else 5

    @staticmethod
    def _discrete_options(
        data: DeviceData,
        mode: OptimizeObjectiveMode = OBJECTIVE_TOTAL_POWER,
    ) -> list[dict[str, int]]:
        """离散台数方案。

        有室内负荷时排除 0 台泵，允许在配置方案内调整冷冻/冷却泵台数。
        总电模式：冷却塔台数定额为当前运行台数。
        回水最低模式：冷却塔在配置方案内参与离散搜索（定频塔靠多开压低温）。
        """
        require_positive = data.indoor_load > 10
        if mode == OBJECTIVE_MIN_COOLING_WATER:
            tower_schemes = [
                s for s in PSOOptimizer._cooling_tower_schemes(data) if s > 0
            ] or [1]
        else:
            tower_schemes = [PSOOptimizer._fixed_tower_count(data)]
        options: list[dict[str, int]] = []
        for chilled_count, cooling_count, tower_count in product(
            PSOOptimizer._pump_schemes("chilled"),
            PSOOptimizer._pump_schemes("cooling"),
            tower_schemes,
        ):
            if require_positive and (chilled_count <= 0 or cooling_count <= 0):
                continue
            options.append(
                {
                    "chilled_pump_count": chilled_count,
                    "cooling_pump_count": cooling_count,
                    "cooling_tower_count": tower_count,
                }
            )
        if options:
            return options
        return [
            {
                "chilled_pump_count": 1,
                "cooling_pump_count": 1,
                "cooling_tower_count": tower_schemes[-1],
            }
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
        mode: OptimizeObjectiveMode = OBJECTIVE_TOTAL_POWER,
    ):
        """构造 PSO 目标函数（最小化）。

        策略：
        - 冷水出水 = 查表基准 + 微调；设备频率下限随室外温度抬升；
        - 始终施加舒适裕量惩罚（与硬闸一致，预防顶出适宜区）；
        - 预测越出适宜硬边界时再叠加硬越界惩罚；
        - total_power：裕量内最小化功耗；
        - min_cooling_water：裕量内最小化冷却水温度（定频塔靠台数 + 冷却泵频）。
        """
        energy_model = self._energy_model
        constraints = self._constraints
        fixed_extra = fixed_extra or {}
        cache: dict[tuple[Any, ...], float] = {}
        model_context = self._model_context(data, fixed_extra)
        outdoor_temp = float(data.outdoor_temp or 30.0)
        measured_indoor = control_indoor_temp(data)
        measured_load = float(data.chiller_load or 0.0)
        bounds_kw = self._bounds_kw_for_data(data)
        try:
            _sb0 = constraints.search_bounds(
                outdoor_temp, measured_load, **bounds_kw
            )
            pump_bias_chp_lo = float(_sb0["chilled_pump_freq"][0])
            pump_bias_cwp_lo = float(_sb0["cooling_pump_freq"][0])
        except Exception:
            pump_bias_chp_lo = 0.0
            pump_bias_cwp_lo = 0.0

        inspired = self._inspired_config()
        inspired_on = bool(inspired.get("enabled"))
        try:
            current_snap = self._finalize_control_params(
                data, self._current_params(data)
            )
        except Exception:
            current_snap = {
                "chilled_water_temp": float(data.chilled_water_temp or 7.0),
                "chilled_pump_freq": float(data.chilled_pump_freq or 35.0),
                "cooling_pump_freq": float(data.cooling_pump_freq or 35.0),
            }
        demand_kw = max(float(data.indoor_load or 0.0), 0.0)
        min_cw_mode = mode == OBJECTIVE_MIN_COOLING_WATER

        def evaluate(params: dict[str, float]) -> float:
            cache_key = (
                mode,
                tuple(
                    round(float(params.get(var, 0.0)), 3)
                    for var in (
                        *VAR_ORDER,
                        "chilled_pump_count",
                        "cooling_pump_count",
                        "cooling_tower_count",
                        "chilled_water_temp",
                    )
                ),
            )
            if cache_key in cache:
                return cache[cache_key]
            try:
                breakdown = energy_model.predict(data, {**params, **model_context})
                if min_cw_mode:
                    cost = float(breakdown.cooling_water_temp) * _CW_TEMP_COST_WEIGHT
                else:
                    cost = breakdown.total_power
                pred_indoor = breakdown.predicted_indoor_temp
                # 舒适区预防性裕量惩罚（始终生效）：预测室温须留在上下限裕量内
                margin_pen = constraints.comfort_margin_penalty(
                    pred_indoor, outdoor_temp, measured_indoor
                )
                if margin_pen > 0:
                    cost += _COMFORT_PENALTY_WEIGHT * margin_pen
                # 越出舒适区硬边界的额外惩罚（始终适用，确保越界成本 > 边界成本）
                if not constraints.is_in_comfort_band(pred_indoor):
                    cost += _COMFORT_PENALTY_WEIGHT * constraints.comfort_penalty(
                        pred_indoor
                    )
                # 总电模式：已在舒适裕量内时轻微偏向更低泵频
                # 回水模式：不惩罚抬高冷却泵频（流量有助于压低回水）
                elif (
                    not min_cw_mode
                    and margin_pen <= 1e-12
                    and (pump_bias_chp_lo > 0 or pump_bias_cwp_lo > 0)
                ):
                    chp = float(params.get("chilled_pump_freq", pump_bias_chp_lo))
                    cwp = float(params.get("cooling_pump_freq", pump_bias_cwp_lo))
                    cost += _PUMP_FLOOR_BIAS_KW_PER_HZ * (
                        max(0.0, chp - pump_bias_chp_lo)
                        + max(0.0, cwp - pump_bias_cwp_lo)
                    )
                # ChillStream 可借鉴：定值跳变 / 欠供冷 / PLR 甜点软惩罚
                if inspired_on:
                    cost += setpoint_change_penalty(
                        params,
                        current_snap,
                        weight=float(inspired.get("setpoint_change_weight", 0.0)),
                        chw_scale=float(inspired.get("chw_change_scale", 1.0)),
                        freq_scale=float(inspired.get("freq_change_scale", 5.0)),
                    )
                    cost += unmet_cooling_penalty(
                        float(breakdown.delivered_cooling or 0.0),
                        demand_kw,
                        weight=float(inspired.get("unmet_cooling_weight", 0.0)),
                    )
                    # 与主机功率同一口径：ElectricEIR PLR1
                    plr = float(getattr(breakdown, "plr1", 0.0) or 0.0)
                    cost += plr_sweet_spot_penalty(
                        plr,
                        lo=float(inspired.get("plr_sweet_lo", 0.30)),
                        hi=float(inspired.get("plr_sweet_hi", 0.55)),
                        weight=float(inspired.get("plr_sweet_weight", 0.0)),
                    )
                search_vars = {var: params.get(var, 0.0) for var in VAR_ORDER}
                cost += _HARD_PENALTY_WEIGHT * constraints.hard_violation(
                    search_vars, outdoor_temp, measured_load, **bounds_kw
                )
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
            full = self._finalize_control_params(
                data, {**base_params, **fixed_extra}
            )
            return evaluate({**full, **fixed_extra})

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
            context["_site_params"] = self._site_params_for_counts(
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
            context["_baseline_site_params"] = self._site_params_for_counts(
                chilled_pump_count=int(baseline_params.get("chilled_pump_count", 1)),
                cooling_pump_count=int(baseline_params.get("cooling_pump_count", 1)),
                tower_count=int(baseline_params.get("cooling_tower_count", 5)),
            )
        except Exception as e:
            logger.debug(f"预计算寻优模型上下文失败，回退逐次推断: {e}")
        return context

    def _site_params_for_counts(
        self,
        chilled_pump_count: int,
        cooling_pump_count: int,
        tower_count: int,
    ):
        """单次寻优内缓存现场模型参数，避免并行读库抖动。"""
        key = (int(chilled_pump_count), int(cooling_pump_count), int(tower_count))
        cache = getattr(self, "_run_site_params_cache", None)
        if isinstance(cache, dict) and key in cache:
            return cache[key]
        params = self._energy_model._params_for_site(
            chilled_pump_count=key[0],
            cooling_pump_count=key[1],
            tower_count=key[2],
        )
        # 仅缓存已成功加载现场装机冷量的参数，避免把默认 120kW 写进缓存
        if isinstance(cache, dict) and float(params.design_cooling_capacity) > 500.0:
            cache[key] = params
        return params

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
        """读取冷却塔配置台数方案（仅作定额回退；寻优不再切换塔台数）。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            enabled_count = len([tower for tower in eq.cooling_towers if tower.enabled])
            schemes = sorted(
                {
                    max(0, min(int(s), enabled_count))
                    for s in (eq.cooling_tower_schemes or [enabled_count])
                }
            )
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

    def _bounds_kw_for_data(self, data: DeviceData) -> dict[str, Any]:
        """从实测工况提取 search_bounds 的附加参数（舒适区内锁定主机负荷上限）。"""
        ctx = self._constraints.bounds_context_for_data(data.model_dump())
        return {
            k: v
            for k, v in ctx.items()
            if k not in ("outdoor_temp", "measured_load_pct")
        }

    def _finalize_control_params(
        self, data: DeviceData, raw: dict[str, float | int]
    ) -> dict[str, float | int]:
        """将 PSO 搜索变量补全为含冷水温度/负荷的完整控制参数字典。"""
        outdoor_temp = float(data.outdoor_temp or 30.0)
        measured_load = float(data.chiller_load or 0.0)
        bounds_kw = self._bounds_kw_for_data(data)
        clipped = self._constraints.clip(
            {var: raw.get(var, 0.0) for var in VAR_ORDER},
            outdoor_temp,
            measured_load,
            **bounds_kw,
        )
        result = dict(raw)
        for var in VAR_ORDER:
            result[var] = clipped[var]
        bounds = self._constraints.search_bounds(
            outdoor_temp, measured_load, **bounds_kw
        )
        result["chiller_load_pct"] = max(
            float(result.get("chiller_load_pct", 0.0)),
            bounds["chiller_load_pct"][0],
        )
        result["chiller_load_pct"] = min(
            float(result.get("chiller_load_pct", 0.0)),
            bounds["chiller_load_pct"][1],
        )
        result["chilled_pump_freq"] = max(
            float(result.get("chilled_pump_freq", 0.0)),
            bounds["chilled_pump_freq"][0],
        )
        result["cooling_pump_freq"] = max(
            float(result.get("cooling_pump_freq", 0.0)),
            bounds["cooling_pump_freq"][0],
        )
        offset = float(result.get("chilled_water_temp_offset", 0.0))
        result["chilled_water_temp"] = self._constraints.resolve_chilled_water_for_control(
            outdoor_temp,
            float(data.chilled_water_temp or 7.0),
            control_indoor_temp(data),
            offset,
        )
        # 主机负荷、冷却塔频率定额：始终回写为现场当前值
        if measured_load > 0:
            result["chiller_load_pct"] = measured_load
        elif float(result.get("chiller_load_pct", 0.0)) <= 0:
            result["chiller_load_pct"] = clipped.get("chiller_load_pct", 80.0)
        tower_now = float(data.cooling_tower_fan_freq or 0.0)
        if tower_now <= 0:
            tower_lo, tower_hi = bounds["cooling_tower_fan_freq"]
            tower_now = tower_lo if tower_lo == tower_hi else 0.5 * (tower_lo + tower_hi)
        if tower_now > 0:
            result["cooling_tower_fan_freq"] = tower_now
        result["cooling_tower_count"] = int(
            result.get("cooling_tower_count", self._fixed_tower_count(data))
        )
        return result

    def _predict_with_context(
        self,
        data: DeviceData,
        params: dict[str, float | int],
    ):
        """与 PSO 目标函数一致的预测上下文，保证功耗对比口径统一。"""
        fixed_extra = {
            "chilled_pump_count": int(params.get("chilled_pump_count", 1)),
            "cooling_pump_count": int(params.get("cooling_pump_count", 1)),
            "cooling_tower_count": int(params.get("cooling_tower_count", 5)),
        }
        ctx = self._model_context(data, fixed_extra)
        return self._energy_model.predict(data, {**params, **ctx})

    def _policy_reference_baseline(
        self, data: DeviceData, current_full: dict[str, float | int]
    ) -> float:
        """同口径基线：查表值 - finetune（最冷允许冷水）+ 当前主机/泵设定。"""
        finetune = self._constraints.chw_finetune.max_delta
        ref = dict(current_full)
        ref["chilled_water_temp_offset"] = -finetune
        try:
            ref = self._finalize_control_params(data, ref)
            return self._predict_with_context(data, ref).total_power
        except Exception:
            return 0.0

    def _refine_for_policy_saving(
        self,
        data: DeviceData,
        current_full: dict[str, float | int],
        params: dict[str, float | int],
    ) -> dict[str, float | int]:
        """轻量精修：稀疏试探冷水微调与泵频（约几十次预测，不做全网格）。"""
        outdoor = float(data.outdoor_temp or 30.0)
        measured_indoor = control_indoor_temp(data)
        measured_total = float(data.total_power or 0.0)
        measured_load = float(data.chiller_load or 0.0)
        finetune = self._constraints.chw_finetune.max_delta
        bounds_kw = self._bounds_kw_for_data(data)
        try:
            sb = self._constraints.search_bounds(
                outdoor, measured_load, **bounds_kw
            )
            chp_lo = float(sb["chilled_pump_freq"][0])
            cwp_lo = float(sb["cooling_pump_freq"][0])
            chp_hi = float(sb["chilled_pump_freq"][1])
            cwp_hi = float(sb["cooling_pump_freq"][1])
        except Exception:
            chp_lo, cwp_lo, chp_hi, cwp_hi = 0.0, 0.0, 50.0, 50.0

        def _acceptable(indoor: float) -> bool:
            return self._constraints.is_within_comfort_margin(
                indoor, outdoor, measured_indoor
            )

        best = dict(params)
        try:
            best = self._finalize_control_params(data, best)
            best_bd = self._predict_with_context(data, best)
            best_power = best_bd.total_power
            best_margin = self._constraints.comfort_margin_penalty(
                best_bd.predicted_indoor_temp, outdoor, measured_indoor
            )
        except Exception:
            return params

        need_recovery = not _acceptable(best_bd.predicted_indoor_temp)
        base_chp = float(best.get("chilled_pump_freq", data.chilled_pump_freq or 40.0))
        base_cwp = float(best.get("cooling_pump_freq", data.cooling_pump_freq or 40.0))
        base_load = float(best.get("chiller_load_pct", data.chiller_load or 80.0))
        base_offset = float(best.get("chilled_water_temp_offset", 0.0))
        ceiling = self._constraints.effective_comfort_ceiling(outdoor, measured_indoor)
        best_indoor = float(best_bd.predicted_indoor_temp)

        def _sparse_freqs(base: float, lo: float, hi: float, down: bool) -> list[float]:
            """仅取当前/±2/±5/端点等少数点，避免 1Hz 全扫。"""
            vals = {round(base, 2)}
            if down:
                for d in (2.0, 5.0):
                    vals.add(round(max(lo, base - d), 2))
                if lo > 0:
                    vals.add(round(lo, 2))
            else:
                for d in (2.0, 5.0):
                    vals.add(round(min(hi, base + d), 2))
                vals.add(round(hi, 2))
            return sorted(vals)

        # 主机负荷定额不调，精修只动冷水微调与泵频
        load_deltas = (0.0,)
        if need_recovery:
            chp_list = _sparse_freqs(base_chp, chp_lo, chp_hi, down=False)
            cwp_list = _sparse_freqs(base_cwp, cwp_lo, cwp_hi, down=False)
            power_cap = (
                measured_total * 1.15 if measured_total > 1e-6 else float("inf")
            )
            offsets = (
                sorted(
                    {
                        round(base_offset, 3),
                        round(-finetune, 3),
                        round(-finetune * 0.5, 3),
                    }
                )
                if finetune > 0
                else [round(base_offset, 3)]
            )
        else:
            chp_list = _sparse_freqs(base_chp, chp_lo, chp_hi, down=True)
            cwp_list = _sparse_freqs(base_cwp, cwp_lo, cwp_hi, down=True)
            power_cap = (
                measured_total + 0.5 if measured_total > 1e-6 else float("inf")
            )
            # 裕量内：允许抬冷水（查表方向）+ 降泵，挤出正节能
            if finetune > 0:
                offsets = sorted(
                    {
                        round(base_offset, 3),
                        0.0,
                        round(finetune * 0.5, 3),
                        round(finetune, 3),
                        round(-finetune * 0.5, 3),
                    }
                )
            else:
                offsets = [round(base_offset, 3)]

        def _try(trial_src: dict[str, float | int]) -> None:
            nonlocal best, best_power, best_margin, best_indoor
            trial = self._finalize_control_params(data, trial_src)
            try:
                bd = self._predict_with_context(data, trial)
            except Exception:
                return
            margin = self._constraints.comfort_margin_penalty(
                bd.predicted_indoor_temp, outdoor, measured_indoor
            )
            if not need_recovery and not _acceptable(bd.predicted_indoor_temp):
                return
            if bd.total_power > power_cap + 1e-6:
                return
            if need_recovery:
                below_ceiling = bd.predicted_indoor_temp <= ceiling + 1e-9
                best_below = best_indoor <= ceiling + 1e-9
                # 先抢回天花板以下，再比裕量惩罚/功耗
                if below_ceiling and not best_below:
                    best = trial
                    best_power = bd.total_power
                    best_margin = margin
                    best_indoor = float(bd.predicted_indoor_temp)
                    return
                if below_ceiling == best_below:
                    if margin < best_margin - 1e-9 or (
                        abs(margin - best_margin) <= 1e-9
                        and bd.total_power < best_power - 1e-6
                    ):
                        best = trial
                        best_power = bd.total_power
                        best_margin = margin
                        best_indoor = float(bd.predicted_indoor_temp)
                return
            power_better = bd.total_power < best_power - 1e-6
            power_tie = abs(bd.total_power - best_power) <= 0.05
            pumps_lower = (
                float(trial.get("chilled_pump_freq", base_chp))
                + float(trial.get("cooling_pump_freq", base_cwp))
                < float(best.get("chilled_pump_freq", base_chp))
                + float(best.get("cooling_pump_freq", base_cwp))
                - 1e-6
            )
            # 功耗接近时：优先更靠近安全目标（更远离 26℃ 硬上限）
            target = self._constraints.safety_indoor_target(
                outdoor, measured_indoor
            )
            indoor_safer = abs(bd.predicted_indoor_temp - target) < abs(
                best_indoor - target
            ) - 1e-9
            if power_better or (power_tie and (pumps_lower or indoor_safer)):
                best = trial
                best_power = bd.total_power
                best_indoor = float(bd.predicted_indoor_temp)

        # 恢复态：少量“双泵同升 + 更冷冷水”组合包（负荷定额不动）
        if need_recovery:
            for bump in (0.0, 2.0, 5.0):
                chp = round(min(chp_hi, base_chp + bump), 2)
                cwp = round(min(cwp_hi, base_cwp + bump), 2)
                for offset in offsets[:2]:
                    trial = dict(params)
                    trial["chilled_water_temp_offset"] = offset
                    trial["chiller_load_pct"] = base_load
                    trial["chilled_pump_freq"] = chp
                    trial["cooling_pump_freq"] = cwp
                    _try(trial)

        def _run_coord_search() -> None:
            # 1) 先只动冷水/负荷（次数少）
            for offset in offsets:
                for load_delta in load_deltas:
                    trial = dict(params)
                    trial["chilled_water_temp_offset"] = offset
                    trial["chiller_load_pct"] = base_load + load_delta
                    trial["chilled_pump_freq"] = float(
                        best.get("chilled_pump_freq", base_chp)
                    )
                    trial["cooling_pump_freq"] = float(
                        best.get("cooling_pump_freq", base_cwp)
                    )
                    _try(trial)

            # 2) 坐标下降：交替调整冷冻泵、冷却泵
            for _ in range(2):
                cur_cwp = float(best.get("cooling_pump_freq", base_cwp))
                cur_off = float(best.get("chilled_water_temp_offset", base_offset))
                cur_load = float(best.get("chiller_load_pct", base_load))
                for chp in chp_list:
                    trial = dict(params)
                    trial["chilled_water_temp_offset"] = cur_off
                    trial["chiller_load_pct"] = cur_load
                    trial["chilled_pump_freq"] = chp
                    trial["cooling_pump_freq"] = cur_cwp
                    _try(trial)
                cur_chp = float(best.get("chilled_pump_freq", base_chp))
                cur_off = float(best.get("chilled_water_temp_offset", base_offset))
                cur_load = float(best.get("chiller_load_pct", base_load))
                for cwp in cwp_list:
                    trial = dict(params)
                    trial["chilled_water_temp_offset"] = cur_off
                    trial["chiller_load_pct"] = cur_load
                    trial["chilled_pump_freq"] = cur_chp
                    trial["cooling_pump_freq"] = cwp
                    _try(trial)

        _run_coord_search()

        # 两阶段：恢复成功后，同一轮立刻切换节能相（降泵/抬冷水），
        # 避免多轮闭环一直停在“恢复态、越寻越费”。
        if need_recovery and _acceptable(best_indoor):
            need_recovery = False
            base_chp = float(best.get("chilled_pump_freq", base_chp))
            base_cwp = float(best.get("cooling_pump_freq", base_cwp))
            base_load = float(best.get("chiller_load_pct", base_load))
            base_offset = float(best.get("chilled_water_temp_offset", base_offset))
            chp_list = _sparse_freqs(base_chp, chp_lo, chp_hi, down=True)
            cwp_list = _sparse_freqs(base_cwp, cwp_lo, cwp_hi, down=True)
            power_cap = (
                measured_total + 0.5 if measured_total > 1e-6 else float("inf")
            )
            if finetune > 0:
                offsets = sorted(
                    {
                        round(base_offset, 3),
                        0.0,
                        round(finetune * 0.5, 3),
                        round(finetune, 3),
                        round(-finetune * 0.5, 3),
                    }
                )
            else:
                offsets = [round(base_offset, 3)]
            _run_coord_search()

        return best

    def _prefer_current_if_no_saving(
        self,
        data: DeviceData,
        current_params: dict[str, float],
        candidate: dict[str, float],
    ) -> tuple[dict[str, float], str]:
        """仅在舒适+节能+稳定三条件同时满足时才调整主机负荷、泵频率和冷水。

        现场室温未舒适时：仍允许冷水查表±微调纠偏，但主机/泵保持当前。
        已舒适但无节能（或预测越裕量）：主机/泵/冷水全部粘住当前，禁止越调越费电。
        """
        measured_indoor = control_indoor_temp(data)
        outdoor_temp = float(data.outdoor_temp or 30.0)
        count_keys = ("chilled_pump_count", "cooling_pump_count", "cooling_tower_count")
        try:
            cur = self._finalize_control_params(data, current_params)
            cand = self._finalize_control_params(data, candidate)
            for key in count_keys:
                if key not in cand:
                    cand[key] = int(cur.get(key, 1))
            current_bd = self._predict_with_context(data, cur)
            candidate_bd = self._predict_with_context(data, cand)
        except Exception:
            return candidate, ""

        measured_total = float(data.total_power or 0.0)
        baseline_ref = (
            measured_total if measured_total > 1e-6 else current_bd.total_power
        )

        site_comfortable = self._constraints.is_in_comfort_band(measured_indoor)
        current_in_margin = self._constraints.is_within_comfort_margin(
            current_bd.predicted_indoor_temp, outdoor_temp, measured_indoor
        )
        candidate_in_margin = self._constraints.is_within_comfort_margin(
            candidate_bd.predicted_indoor_temp, outdoor_temp, measured_indoor
        )
        current_margin_penalty = self._constraints.comfort_margin_penalty(
            current_bd.predicted_indoor_temp, outdoor_temp, measured_indoor
        )
        candidate_margin_penalty = self._constraints.comfort_margin_penalty(
            candidate_bd.predicted_indoor_temp, outdoor_temp, measured_indoor
        )
        candidate_improves_margin = (
            candidate_margin_penalty < current_margin_penalty - 1e-6
        )
        # 仅接受可观测的节能；泵频相对当前下调时阈值放宽，允许小幅靠拢最低频率
        pumps_trimmed = (
            float(candidate.get("chilled_pump_freq", 0.0))
            < float(current_params.get("chilled_pump_freq", 0.0)) - 0.05
            or float(candidate.get("cooling_pump_freq", 0.0))
            < float(current_params.get("cooling_pump_freq", 0.0)) - 0.05
        )
        min_saving_kw = (
            max(0.2, baseline_ref * 0.002)
            if pumps_trimmed
            else max(0.5, baseline_ref * 0.01)
        )
        # 相对现场实测节能；若模型整体偏高，也接受“相对维持现状更省、且不超过实测过多”的降泵方案
        energy_saving = candidate_bd.total_power < baseline_ref - min_saving_kw
        if not energy_saving and pumps_trimmed:
            vs_current = candidate_bd.total_power < current_bd.total_power - min_saving_kw
            not_much_worse_than_site = (
                baseline_ref <= 1e-6
                or candidate_bd.total_power <= baseline_ref * 1.01 + 0.5
            )
            energy_saving = vs_current and not_much_worse_than_site

        def _merge_keep_current(remark: str, keep_candidate_chw: bool) -> tuple[dict[str, float], str]:
            """回退主机负荷/泵频率；无节能时连冷水一并粘住当前，避免越调越费电。"""
            merged = dict(candidate)
            merged["chiller_load_pct"] = float(
                current_params.get("chiller_load_pct", data.chiller_load or 80.0)
            )
            merged["chilled_pump_freq"] = float(
                current_params.get("chilled_pump_freq", data.chilled_pump_freq or 35.0)
            )
            merged["cooling_pump_freq"] = float(
                current_params.get("cooling_pump_freq", data.cooling_pump_freq or 35.0)
            )
            if keep_candidate_chw:
                # 室温未舒适：允许策略冷水微调纠偏，仍保留候选 offset
                pass
            else:
                off, chw = self._constraints.sticky_chilled_water_offset(
                    outdoor_temp, float(data.chilled_water_temp or 0.0)
                )
                merged["chilled_water_temp_offset"] = off
                merged["chilled_water_temp"] = chw
            merged = self._finalize_control_params(data, merged)
            return merged, remark

        # 恢复裕量时：接受“泵频上调/负荷上调”的候选，即使暂无节能
        pumps_boosted = (
            float(candidate.get("chilled_pump_freq", 0.0))
            > float(current_params.get("chilled_pump_freq", 0.0)) + 0.05
            or float(candidate.get("cooling_pump_freq", 0.0))
            > float(current_params.get("cooling_pump_freq", 0.0)) + 0.05
        )
        if (
            site_comfortable
            and not current_in_margin
            and candidate_improves_margin
            and pumps_boosted
            and candidate_bd.total_power <= baseline_ref * 1.15 + 1e-6
        ):
            # 轻微越天花板：不允许靠大幅提频“硬恢复”，优先保住节能
            ceiling = self._constraints.effective_comfort_ceiling(
                outdoor_temp, measured_indoor
            )
            overshoot = max(0.0, float(current_bd.predicted_indoor_temp) - ceiling)
            if overshoot <= 0.25 and candidate_bd.total_power > baseline_ref * 1.03 + 1e-6:
                pass  # fall through to keep-current / other branches
            else:
                return self._finalize_control_params(data, candidate), (
                    "当前室温贴近舒适上限，优先提频恢复安全距离"
                )

        # 三条件同时满足：接受完整推荐方案
        if site_comfortable and candidate_in_margin and energy_saving:
            return self._finalize_control_params(data, candidate), ""

        # 虽仍在 24~26℃，但已经越过预防性上限/下限时，舒适优先。
        # 允许推荐恢复裕量，不因短时功率上涨而继续维持在 26℃边缘。
        if site_comfortable and not current_in_margin and candidate_in_margin:
            ceiling = self._constraints.effective_comfort_ceiling(
                outdoor_temp, measured_indoor
            )
            overshoot = max(0.0, float(current_bd.predicted_indoor_temp) - ceiling)
            # 轻微越界且候选明显更费电：不接受“为 0.2℃ 提频”
            if not (
                overshoot <= 0.25
                and candidate_bd.total_power > baseline_ref * 1.03 + 1e-6
            ):
                return self._finalize_control_params(data, candidate), (
                    "当前室温已越出舒适裕量，优先恢复温度安全距离"
                )
        if (
            site_comfortable
            and not current_in_margin
            and self._constraints.is_in_comfort_band(candidate_bd.predicted_indoor_temp)
            and candidate_improves_margin
        ):
            ceiling = self._constraints.effective_comfort_ceiling(
                outdoor_temp, measured_indoor
            )
            overshoot = max(0.0, float(current_bd.predicted_indoor_temp) - ceiling)
            if not (
                overshoot <= 0.25
                and candidate_bd.total_power > baseline_ref * 1.03 + 1e-6
            ):
                return self._finalize_control_params(data, candidate), (
                    "当前室温已接近舒适上限，优先向安全裕量回调"
                )

        # 室温未舒适：可保留冷水策略纠偏，但主机/泵不跟无节能方案走
        if not site_comfortable:
            return _merge_keep_current(
                "现场室温未达适宜区间，保持当前主机负荷和泵频率",
                keep_candidate_chw=True,
            )
        if not candidate_in_margin:
            return _merge_keep_current(
                "推荐工况预测室温越出舒适裕量，保持当前主机负荷和泵频率",
                keep_candidate_chw=False,
            )
        return _merge_keep_current(
            "推荐方案无节能效果，保持当前主机负荷、泵频率和冷水",
            keep_candidate_chw=False,
        )

    def _apply_output_hard_gates(
        self,
        data: DeviceData,
        current_full: dict[str, float | int],
        candidate: dict[str, float | int],
        mode: OptimizeObjectiveMode = OBJECTIVE_TOTAL_POWER,
    ) -> tuple[dict[str, float | int], str]:
        """最后一道硬闸：预测室温/功耗/负荷超限则回退当前设定。

        冷却回水最低模式不因「预测总功耗高于实测」回退（该目标允许辅机多耗电压低回水）。
        """
        measured_total = float(data.total_power or 0.0)
        max_load = SafetyConstraints.max_chiller_load_pct()
        bkw = self._bounds_kw_for_data(data)
        outdoor = float(data.outdoor_temp or 30.0)
        load = float(data.chiller_load or 0.0)
        min_cw_mode = mode == OBJECTIVE_MIN_COOLING_WATER

        cand = self._finalize_control_params(data, candidate)
        try:
            cur_bd = self._predict_with_context(data, current_full)
            cand_bd = self._predict_with_context(data, cand)
        except Exception:
            return cand, ""

        baseline_ref = (
            measured_total if measured_total > 1e-6 else cur_bd.total_power
        )
        measured_indoor = control_indoor_temp(data) or 25.0
        reference_outdoor = float(
            getattr(data, "chiller_power_reference_outdoor_temp", 0.0) or 0.0
        )
        # 室外降温不应给“增功率恢复”开绿灯；仅升温才放宽功耗上限
        outdoor_warmer = (
            reference_outdoor > 0 and outdoor > reference_outdoor + 0.3
        )
        site_comfortable = self._constraints.is_in_comfort_band(measured_indoor)
        current_in_margin = self._constraints.is_within_comfort_margin(
            cur_bd.predicted_indoor_temp, outdoor, measured_indoor
        )
        current_margin_penalty = self._constraints.comfort_margin_penalty(
            cur_bd.predicted_indoor_temp, outdoor, measured_indoor
        )
        candidate_margin_penalty = self._constraints.comfort_margin_penalty(
            cand_bd.predicted_indoor_temp, outdoor, measured_indoor
        )
        candidate_improves_margin = (
            candidate_margin_penalty < current_margin_penalty - 1e-6
        )
        reasons: list[str] = []
        # 主机负荷定额：候选若偏离实测则回退
        if load > 0 and abs(float(cand.get("chiller_load_pct", 0.0)) - load) > 0.05:
            reasons.append("主机负荷定额不可调整")
        tower_now = float(data.cooling_tower_fan_freq or 0.0)
        if tower_now > 0 and abs(
            float(cand.get("cooling_tower_fan_freq", 0.0)) - tower_now
        ) > 0.05:
            reasons.append("冷却塔频率定额不可调整")
        if float(cand.get("chiller_load_pct", 0.0)) > max_load + 1e-6:
            reasons.append("主机负荷超过设备上限")
        if not self._constraints.is_in_comfort_band(cand_bd.predicted_indoor_temp):
            reasons.append("预测室温越出适宜区间")
        if (
            not self._constraints.is_within_comfort_margin(
                cand_bd.predicted_indoor_temp, outdoor, measured_indoor
            )
            and not (
                not current_in_margin
                and self._constraints.is_in_comfort_band(
                    cand_bd.predicted_indoor_temp
                )
                and candidate_improves_margin
            )
        ):
            reasons.append("预测室温越出舒适裕量")
        # 总电模式：有实测总功率且当前仍在舒适裕量时，预测不得超过输入。
        # 回水最低模式：允许总电升高以换更低冷却水温度。
        if not min_cw_mode:
            if measured_total > 1e-6:
                if current_in_margin and not outdoor_warmer:
                    if cand_bd.total_power > measured_total + 0.5:
                        reasons.append("预测总功耗高于实测输入")
                elif outdoor_warmer:
                    weather_allowance = max(measured_total * 0.15, 50.0)
                    if cand_bd.total_power > measured_total + weather_allowance:
                        reasons.append("室外工况变化所需功率超过安全增幅")
                else:
                    # 仅轻微越过预防天花板时，只允许很小增功率；避免为 0.2℃ 裕量把泵拉满
                    ceiling = self._constraints.effective_comfort_ceiling(
                        outdoor, measured_indoor
                    )
                    overshoot = max(0.0, float(cur_bd.predicted_indoor_temp) - ceiling)
                    if overshoot <= 0.25:
                        recovery_allowance = max(measured_total * 0.03, 12.0)
                    elif overshoot <= 0.5:
                        recovery_allowance = max(measured_total * 0.08, 25.0)
                    else:
                        recovery_allowance = max(measured_total * 0.15, 50.0)
                    if cand_bd.total_power > measured_total + recovery_allowance:
                        reasons.append("恢复舒适裕量所需功率超过安全增幅")
            elif site_comfortable and current_in_margin:
                if cand_bd.total_power > baseline_ref + 0.5:
                    reasons.append("预测总功耗高于基线")
            else:
                power_allowance = max(baseline_ref * 0.1, 50.0)
                if cand_bd.total_power > baseline_ref + power_allowance:
                    reasons.append("预测总功耗显著高于基线")

        if not reasons:
            return cand, ""

        kept = self._constraints.clip(
            {var: current_full.get(var, 0.0) for var in VAR_ORDER},
            outdoor,
            load,
            **bkw,
        )
        merged = dict(cand)
        merged.update(kept)
        for key in ("chilled_pump_count", "cooling_pump_count", "cooling_tower_count"):
            if key in current_full:
                merged[key] = current_full[key]
        # 回退时粘住当前冷水（钳到查表带），禁止 finalize(offset=0) 把 12.5 拉回 12.0 增耗
        off, chw = self._constraints.sticky_chilled_water_offset(
            outdoor, float(data.chilled_water_temp or 0.0)
        )
        merged["chilled_water_temp_offset"] = off
        merged["chilled_water_temp"] = chw
        merged = self._finalize_control_params(data, merged)
        return merged, "硬闸：" + "；".join(reasons) + "，保持现有设定"

    def _is_comfort_at_risk(self, data: DeviceData) -> bool:
        """判断当前正在下发的设定值在本工况下是否已预测舒适度越界。

        用于触发应急平滑：常规阶梯速度不足以跟上工况突变时快速纠偏。
        仅当预测室内真正越出适宜温度区间时才视为紧急。
        """
        try:
            prev = self._guard.last_output
            indoor = self._energy_model.predict(data, prev).predicted_indoor_temp
            return not self._constraints.is_in_comfort_band(indoor)
        except Exception:
            return False

    @staticmethod
    def _current_params(data: DeviceData) -> dict[str, float]:
        """从实测工况提取当前控制参数（作为节能率基线）。"""
        from app.services.power_baseline import current_operating_params

        raw = current_operating_params(data.model_dump())
        return {k: float(v) for k, v in raw.items()}

    def _ensure_clipped_params(
        self, data: DeviceData, params: dict[str, float | int]
    ) -> dict[str, float | int]:
        """按当前工况边界裁剪控制变量（含舒适区锁定与室外分档下限）。"""
        bkw = self._bounds_kw_for_data(data)
        outdoor = float(data.outdoor_temp or 30.0)
        load = float(data.chiller_load or 0.0)
        clipped = self._constraints.clip(
            {var: params.get(var, 0.0) for var in VAR_ORDER},
            outdoor,
            load,
            **bkw,
        )
        merged = dict(params)
        for var in VAR_ORDER:
            merged[var] = clipped[var]
        return self._finalize_control_params(data, merged)

    def _build_result(
        self,
        task_id: str,
        status: str,
        data: DeviceData,
        params: dict[str, float],
        start: float,
        baseline_power: float = 0.0,
        remark: str = "",
        recommended: dict[str, float] | None = None,
        forecast_indoor_load: float = 0.0,
        blackbox_baseline_power: float = 0.0,
        blackbox_saving_rate: float = 0.0,
        fallback_rule: str = "",
        objective_mode: OptimizeObjectiveMode = OBJECTIVE_TOTAL_POWER,
    ) -> OptimizeResult:
        """依据最终控制参数构造 OptimizeResult（含预测能耗与节能率）。

        params = 实发（平滑/硬闸后）；recommended = PSO 原始推荐（可选）。
        """
        params = self._ensure_clipped_params(data, params)
        # 兜底/异常路径下 params 可能缺失 chilled_water_temp，按室外温度查表补齐
        if "chilled_water_temp" not in params:
            params = dict(params)
            params = self._finalize_control_params(data, params)
        try:
            fixed_extra = {
                "chilled_pump_count": int(params.get("chilled_pump_count", 1)),
                "cooling_pump_count": int(params.get("cooling_pump_count", 1)),
                "cooling_tower_count": int(params.get("cooling_tower_count", 5)),
            }
            predict_params = {**params, **self._model_context(data, fixed_extra)}
            breakdown = self._energy_model.predict(data, predict_params)
            predicted = breakdown.total_power
        except Exception:
            breakdown = None
            predicted = 0.0

        if status == "success" and baseline_power > 1e-6 and predicted > 0:
            saving = (baseline_power - predicted) / baseline_power * 100.0
        else:
            saving = 0.0

        measured_total = float(data.total_power or 0.0)
        reference_outdoor = float(
            getattr(data, "chiller_power_reference_outdoor_temp", 0.0) or 0.0
        )
        weather_shifted = (
            reference_outdoor > 0
            and abs(float(data.outdoor_temp or 0.0) - reference_outdoor) > 0.3
        )
        # 总电模式：预测高于实测时锚定显示；回水最低模式保留真实预测（允许辅机多耗电）
        if (
            objective_mode != OBJECTIVE_MIN_COOLING_WATER
            and status == "success"
            and measured_total > 1e-6
            and predicted > measured_total + 0.5
            and not weather_shifted
        ):
            predicted = measured_total
            saving = 0.0
            if breakdown:
                from dataclasses import replace

                # 锚定总功率时按比例缩放分项，保证分项之和 = 预测总功率
                model_total = max(float(breakdown.total_power or 0.0), 1e-6)
                scale = measured_total / model_total
                breakdown = replace(
                    breakdown,
                    total_power=measured_total,
                    chiller_power=round(float(breakdown.chiller_power) * scale, 4),
                    chilled_pump_power=round(
                        float(breakdown.chilled_pump_power) * scale, 4
                    ),
                    cooling_pump_power=round(
                        float(breakdown.cooling_pump_power) * scale, 4
                    ),
                    cooling_tower_fan_power=round(
                        float(breakdown.cooling_tower_fan_power) * scale, 4
                    ),
                    terminal_fan_power=round(
                        float(breakdown.terminal_fan_power) * scale, 4
                    ),
                )
            if remark and "高于实测输入" not in remark:
                remark = f"{remark}; 预测总功耗高于实测输入，已锚定输入功率"
            elif not remark:
                remark = "预测总功耗高于实测输入，已锚定输入功率"

        measured_indoor = control_indoor_temp(data)
        display_indoor = (
            breakdown.predicted_indoor_temp if breakdown else 0.0
        )
        # 仅当预测室温越出舒适区时才用实测值替换显示值；
        # 预测室温在舒适区内时始终显示预测值，让用户看到优化效果
        if (
            breakdown
            and self._constraints.is_in_comfort_band(measured_indoor)
            and not self._constraints.is_in_comfort_band(display_indoor)
        ):
            display_indoor = measured_indoor

        chilled_pump_count = int(params.get("chilled_pump_count", 1))
        cooling_pump_count = int(params.get("cooling_pump_count", 1))
        tower_count = int(params.get("cooling_tower_count", 5))
        chilled_pump_total = (
            breakdown.chilled_pump_power if breakdown else self._pump_power("chilled", chilled_pump_count, params["chilled_pump_freq"])
        )
        cooling_pump_total = (
            breakdown.cooling_pump_power if breakdown else self._pump_power("cooling", cooling_pump_count, params["cooling_pump_freq"])
        )
        # 前端预测列展示单台水泵功率（kW）
        chilled_pump_power = chilled_pump_total / max(chilled_pump_count, 1)
        cooling_pump_power = cooling_pump_total / max(cooling_pump_count, 1)
        tower_power = (
            breakdown.cooling_tower_fan_power if breakdown else self._cooling_tower_power(tower_count)
        )

        rule_label = FALLBACK_RULES.get(fallback_rule, "")
        if rule_label and fallback_rule not in ("", "ok"):
            remark = f"[{rule_label}] {remark}".strip() if remark else f"[{rule_label}]"

        rec = recommended or {}
        rec_chw = rec.get("chilled_water_temp")
        rec_chp = rec.get("chilled_pump_freq")
        rec_cwp = rec.get("cooling_pump_freq")
        rec_tower = rec.get("cooling_tower_fan_freq")

        return OptimizeResult(
            task_id=task_id,
            status=status,
            objective_mode=objective_mode,
            chilled_water_temp=round(params["chilled_water_temp"], 2),
            chilled_water_temp_offset=round(
                float(params.get("chilled_water_temp_offset", 0.0)), 2
            ),
            chiller_load_pct=round(float(params.get("chiller_load_pct", 0.0)), 2),
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
            predicted_indoor_temp=round(display_indoor, 2),
            predicted_chiller_power=(
                round(breakdown.chiller_power, 3) if breakdown else 0.0
            ),
            predicted_cooling_water_temp=(
                round(breakdown.cooling_water_temp, 2) if breakdown else 0.0
            ),
            predicted_cop=round(breakdown.cop, 3) if breakdown else 0.0,
            energy_saving_rate=round(saving, 2),
            recommended_chilled_water_temp=(
                round(float(rec_chw), 2) if rec_chw is not None else None
            ),
            recommended_chilled_pump_freq=(
                round(float(rec_chp), 2) if rec_chp is not None else None
            ),
            recommended_cooling_pump_freq=(
                round(float(rec_cwp), 2) if rec_cwp is not None else None
            ),
            recommended_cooling_tower_fan_freq=(
                round(float(rec_tower), 2) if rec_tower is not None else None
            ),
            forecast_indoor_load=round(float(forecast_indoor_load or 0.0), 2),
            blackbox_baseline_power=round(float(blackbox_baseline_power or 0.0), 3),
            blackbox_saving_rate=round(float(blackbox_saving_rate or 0.0), 2),
            fallback_rule=fallback_rule or "",
            duration=round(time.time() - start, 4),
            optimized_at=datetime.now(),
            remark=remark,
        )

    def _fallback_result(
        self,
        task_id: str,
        status: str,
        remark: str,
        start: float,
        fallback_rule: str = "",
    ) -> OptimizeResult:
        """无有效工况时的纯兜底结果（固定参数）。"""
        params = self._guard.fallback_params(remark)
        # 兜底路径无工况数据，chilled_water_temp 用固定兜底值（8.0）
        if "chilled_water_temp" not in params:
            params["chilled_water_temp"] = self._guard.fixed_params.get(
                "chilled_water_temp", 8.0
            )
        params["chilled_pump_count"] = self._default_pump_count("chilled")
        params["cooling_pump_count"] = self._default_pump_count("cooling")
        params["cooling_tower_count"] = self._default_tower_count()
        if "chiller_load_pct" not in params:
            params["chiller_load_pct"] = 80.0
        if "chilled_water_temp_offset" not in params:
            params["chilled_water_temp_offset"] = 0.0
        chp_n = int(params["chilled_pump_count"])
        cwp_n = int(params["cooling_pump_count"])
        rule_label = FALLBACK_RULES.get(fallback_rule, "")
        if rule_label and fallback_rule not in ("", "ok"):
            remark = f"[{rule_label}] {remark}".strip() if remark else f"[{rule_label}]"
        return OptimizeResult(
            task_id=task_id,
            status=status,
            chilled_water_temp=round(params["chilled_water_temp"], 2),
            chilled_water_temp_offset=round(
                float(params.get("chilled_water_temp_offset", 0.0)), 2
            ),
            chiller_load_pct=round(float(params.get("chiller_load_pct", 80.0)), 2),
            chilled_pump_freq=round(params["chilled_pump_freq"], 2),
            chilled_pump_count=chp_n,
            chilled_pump_power=round(
                self._pump_power("chilled", chp_n, params["chilled_pump_freq"])
                / max(chp_n, 1),
                3,
            ),
            cooling_pump_freq=round(params["cooling_pump_freq"], 2),
            cooling_pump_count=cwp_n,
            cooling_pump_power=round(
                self._pump_power("cooling", cwp_n, params["cooling_pump_freq"])
                / max(cwp_n, 1),
                3,
            ),
            cooling_tower_fan_freq=round(params["cooling_tower_fan_freq"], 2),
            cooling_tower_count=int(params["cooling_tower_count"]),
            cooling_tower_power=round(
                self._cooling_tower_power(int(params["cooling_tower_count"])), 3
            ),
            predicted_power=0.0,
            energy_saving_rate=0.0,
            fallback_rule=fallback_rule or "",
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
    def _default_tower_count() -> int:
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            enabled = [t for t in eq.cooling_towers if t.enabled]
            return max(0, len(enabled))
        except Exception:
            return 5

    def _normalize_pump_powers_from_freq(self, data: DeviceData) -> DeviceData:
        """忽略输入泵 kW，按 P=P_rated×(f/f_rated)³ 重写冷冻/冷却泵功率与总功率。"""
        from app.services.power_baseline import scheme_max

        rated_freq = float(getattr(data, "pump_rated_freq", 0.0) or 0.0) or 50.0
        chp_unit = float(getattr(data, "chilled_pump_rated_power_kw", 0.0) or 0.0)
        cwp_unit = float(getattr(data, "cooling_pump_rated_power_kw", 0.0) or 0.0)
        chp_freq = float(data.chilled_pump_freq or 0.0)
        cwp_freq = float(data.cooling_pump_freq or 0.0)
        # 优先闭环/前端传入的开启台数；否则取允许方案最大台数（不是无脑装机全开）
        chp_n = int(getattr(data, "chilled_pump_running_count", 0) or 0)
        cwp_n = int(getattr(data, "cooling_pump_running_count", 0) or 0)
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            if chp_unit <= 0:
                chp_unit = float(eq.chilled_pump.motor_power_kw or 0.0)
            if cwp_unit <= 0:
                cwp_unit = float(eq.cooling_pump.motor_power_kw or 0.0)
            if chp_n <= 0 and chp_freq > 0:
                chp_n = scheme_max(
                    eq.chilled_pump.active_count_schemes, eq.chilled_pump.count
                )
            if cwp_n <= 0 and cwp_freq > 0:
                cwp_n = scheme_max(
                    eq.cooling_pump.active_count_schemes, eq.cooling_pump.count
                )
            chp_n = max(0, min(chp_n, int(eq.chilled_pump.count or chp_n)))
            cwp_n = max(0, min(cwp_n, int(eq.cooling_pump.count or cwp_n)))
        except Exception:
            if chp_n <= 0 and chp_freq > 0:
                chp_n = 1
            if cwp_n <= 0 and cwp_freq > 0:
                cwp_n = 1
        if chp_freq <= 0:
            chp_n = 0
        if cwp_freq <= 0:
            cwp_n = 0

        chp_total = self._pump_power_with_rated(
            chp_n, chp_unit, chp_freq, rated_freq
        )
        cwp_total = self._pump_power_with_rated(
            cwp_n, cwp_unit, cwp_freq, rated_freq
        )
        total = (
            float(data.chiller_power or 0.0)
            + chp_total
            + cwp_total
            + float(data.cooling_tower_fan_power or 0.0)
            + float(data.terminal_fan_power or 0.0)
        )
        return data.model_copy(
            update={
                "chilled_pump_power": round(chp_total, 3),
                "cooling_pump_power": round(cwp_total, 3),
                "chilled_pump_rated_power_kw": round(chp_unit, 3),
                "cooling_pump_rated_power_kw": round(cwp_unit, 3),
                "chilled_pump_running_count": int(chp_n),
                "cooling_pump_running_count": int(cwp_n),
                "pump_rated_freq": round(rated_freq, 3),
                "total_power": round(total, 3) if total > 1e-6 else float(data.total_power or 0.0),
            }
        )

    @staticmethod
    def _pump_power_with_rated(
        count: int, rated_unit_kw: float, freq: float, rated_freq: float = 50.0
    ) -> float:
        count = max(int(count), 0)
        f_rated = max(float(rated_freq or 50.0), 1e-6)
        ratio = max(float(freq), 0.0) / f_rated
        return max(float(rated_unit_kw), 0.0) * count * (ratio**3)

    @staticmethod
    def _pump_power(kind: str, count: int, freq: float, rated_freq: float = 50.0) -> float:
        """按推荐开启台数和频率计算水泵功率。"""
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            pump = eq.chilled_pump if kind == "chilled" else eq.cooling_pump
            count = max(0, min(int(count), pump.count))
            f_rated = max(float(rated_freq or 50.0), 1e-6)
            ratio = max(float(freq), 0.0) / f_rated
            return count * pump.motor_power_kw * (ratio ** 3)
        except Exception:
            return 0.0

    @staticmethod
    def _cooling_tower_power(count: int) -> float:
        """按推荐开启台数计算冷却塔定频总功率。"""
        count = max(int(count), 0)
        if count >= 5:
            return 70.0
        if count >= 3:
            return 70.0 * count / 5.0
        try:
            from app.services.equipment_config import equipment_config_service

            eq = equipment_config_service.get_config()
            enabled = [tower for tower in eq.cooling_towers if tower.enabled]
            count = min(count, len(enabled))
            return sum(tower.motor_power_kw for tower in enabled[:count])
        except Exception:
            return 0.0
