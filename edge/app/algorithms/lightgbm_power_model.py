"""LightGBM 黑盒功率预测模型。

用现场运行数据学习「工况/设定 → 总功率（或主机功率）」。
不替代 PSO：寻优仍由 PSO 搜索参数，本模型可提供能耗估计。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

# 与寻优决策/边界相关的特征（表格工况）
FEATURE_COLUMNS: tuple[str, ...] = (
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
)

TARGET_TOTAL = "total_power"
TARGET_CHILLER = "chiller_power"
SUPPORTED_TARGETS = (TARGET_TOTAL, TARGET_CHILLER)


@dataclass
class LightGBMTrainMetrics:
    """训练评估指标。"""

    n_train: int = 0
    n_valid: int = 0
    n_test: int = 0
    mae: float = 0.0
    rmse: float = 0.0
    mape: float = 0.0
    r2: float = 0.0
    target: str = TARGET_TOTAL
    trained_at: str = ""
    feature_columns: list[str] = field(default_factory=list)
    best_iteration: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LightGBMPredictResult:
    """单条预测结果。"""

    predicted_power: float
    target: str
    model_loaded: bool
    features_used: dict[str, float] = field(default_factory=dict)


class _SklearnPowerBackend:
    """sklearn HistGradientBoosting 适配层（LightGBM 原生库崩溃时的回退）。"""

    def __init__(self, estimator: Any, best_iteration: int = 0) -> None:
        self.estimator = estimator
        self.best_iteration = int(best_iteration or 0)

    def predict(self, x: Any, num_iteration: int | None = None) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        return np.asarray(self.estimator.predict(arr), dtype=np.float64)


class LightGBMPowerModel:
    """功率回归封装：优先 LightGBM，Windows 原生崩溃时自动回退 HistGradientBoosting。"""

    def __init__(self, model_dir: str | Path | None = None) -> None:
        root = Path(model_dir) if model_dir else Path("data/ml")
        self.model_dir = root
        self.model_path = root / "lightgbm_power_model.txt"
        self.sklearn_model_path = root / "lightgbm_power_model.joblib"
        self.meta_path = root / "lightgbm_power_meta.json"
        self._model = None
        self._backend: str = ""
        self._meta: dict[str, Any] = {}
        self._load_if_exists()

    # ---------- 对外属性 ----------

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    @property
    def metrics(self) -> dict[str, Any]:
        return dict(self._meta.get("metrics") or {})

    @property
    def target(self) -> str:
        return str(self._meta.get("target") or TARGET_TOTAL)

    @property
    def feature_columns(self) -> list[str]:
        cols = self._meta.get("feature_columns") or list(FEATURE_COLUMNS)
        return [str(c) for c in cols]

    def status(self) -> dict[str, Any]:
        path = ""
        if self._backend == "sklearn" and self.sklearn_model_path.exists():
            path = str(self.sklearn_model_path.resolve())
        elif self.model_path.exists():
            path = str(self.model_path.resolve())
        return {
            "ready": self.is_ready,
            "model_path": path,
            "target": self.target,
            "feature_columns": self.feature_columns,
            "metrics": self.metrics,
            "lightgbm_available": _lightgbm_available(),
            "backend": self._backend or self._meta.get("backend") or "",
            "lightgbm_native_ok": _prefer_lightgbm_native(),
            "backend_policy": _backend_policy_reason(),
        }

    # ---------- 数据准备 ----------

    @staticmethod
    def rows_to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
        """将字典行转为 DataFrame，并补齐特征/目标列。"""
        if not rows:
            raise ValueError("训练数据为空")
        df = pd.DataFrame(rows)
        for col in FEATURE_COLUMNS:
            if col not in df.columns:
                df[col] = 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in (TARGET_TOTAL, TARGET_CHILLER, "chilled_pump_power", "cooling_pump_power", "cooling_tower_fan_power"):
            if col not in df.columns:
                df[col] = 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 台数缺省：频率>0 视为至少 1 台
        chp_cnt = df["chilled_pump_running_count"].fillna(0)
        cwp_cnt = df["cooling_pump_running_count"].fillna(0)
        df["chilled_pump_running_count"] = np.where(
            chp_cnt > 0, chp_cnt, np.where(df["chilled_pump_freq"].fillna(0) > 0, 1, 0)
        )
        df["cooling_pump_running_count"] = np.where(
            cwp_cnt > 0, cwp_cnt, np.where(df["cooling_pump_freq"].fillna(0) > 0, 1, 0)
        )

        # 总功率缺失时用分项求和
        parts = (
            df["chiller_power"].fillna(0)
            + df["chilled_pump_power"].fillna(0)
            + df["cooling_pump_power"].fillna(0)
            + df["cooling_tower_fan_power"].fillna(0)
            + df["terminal_fan_power"].fillna(0)
        )
        missing_total = ~np.isfinite(df[TARGET_TOTAL]) | (df[TARGET_TOTAL] <= 0)
        df.loc[missing_total, TARGET_TOTAL] = parts[missing_total]
        return df

    @staticmethod
    def _finite_mask(df: pd.DataFrame, target: str) -> pd.Series:
        mask = np.isfinite(df[target].to_numpy(dtype=float)) & (df[target] > 0)
        for col in FEATURE_COLUMNS:
            mask = mask & np.isfinite(df[col].to_numpy(dtype=float))
        return pd.Series(mask, index=df.index)

    # ---------- 训练 ----------

    def train(
        self,
        rows: list[dict[str, Any]],
        *,
        target: str = TARGET_TOTAL,
        valid_ratio: float = 0.15,
        test_ratio: float = 0.15,
        num_boost_round: int = 300,
        early_stopping_rounds: int = 40,
        seed: int = 42,
    ) -> LightGBMTrainMetrics:
        """按时间顺序切分训练/验证/测试，训练功率回归模型。"""
        if target not in SUPPORTED_TARGETS:
            raise ValueError(f"不支持的目标列: {target}，可选 {SUPPORTED_TARGETS}")

        df = self.rows_to_dataframe(rows)
        mask = self._finite_mask(df, target)
        df = df.loc[mask].reset_index(drop=True)
        if len(df) < 30:
            raise ValueError(f"有效样本过少（{len(df)}），至少需要约 30 条对齐记录")

        n = len(df)
        n_test = max(1, int(n * test_ratio))
        n_valid = max(1, int(n * valid_ratio))
        n_train = n - n_valid - n_test
        if n_train < 20:
            raise ValueError(f"训练集过少（{n_train}），请增加数据量")

        # 按行序近似时间序，避免随机打乱泄漏未来工况
        train_df = df.iloc[:n_train]
        valid_df = df.iloc[n_train : n_train + n_valid]
        test_df = df.iloc[n_train + n_valid :]

        features = list(FEATURE_COLUMNS)
        x_train = train_df[features].to_numpy(dtype=np.float64, copy=True)
        y_train = train_df[target].to_numpy(dtype=np.float64, copy=True)
        x_valid = valid_df[features].to_numpy(dtype=np.float64, copy=True)
        y_valid = valid_df[target].to_numpy(dtype=np.float64, copy=True)
        x_test = test_df[features].to_numpy(dtype=np.float64, copy=True)
        y_test = test_df[target].to_numpy(dtype=np.float64, copy=True)

        params = {
            "objective": "regression",
            "metric": "l2",
            "learning_rate": 0.05,
            "num_leaves": 31,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "min_data_in_leaf": 10,
            "verbosity": -1,
            "seed": seed,
            "num_threads": _lgbm_num_threads(),
            "force_col_wise": True,
        }

        backend = "lightgbm"
        model: Any = None
        best_iteration = 0
        prefer_lgbm = _prefer_lightgbm_native()
        if prefer_lgbm:
            try:
                model, best_iteration = self._train_lightgbm(
                    x_train,
                    y_train,
                    x_valid,
                    y_valid,
                    features=features,
                    params=params,
                    num_boost_round=num_boost_round,
                    early_stopping_rounds=early_stopping_rounds,
                )
                backend = "lightgbm"
            except Exception as e:
                logger.warning("LightGBM 训练失败，回退 sklearn HistGradientBoosting: {}", e)
                backend = "sklearn"
                model, best_iteration = self._train_sklearn(
                    x_train,
                    y_train,
                    x_valid,
                    y_valid,
                    num_boost_round=num_boost_round,
                    early_stopping_rounds=early_stopping_rounds,
                    seed=seed,
                )
        else:
            backend = "sklearn"
            logger.warning(
                "当前环境不优先使用 LightGBM 原生库（{}），改用 sklearn HistGradientBoostingRegressor",
                _backend_policy_reason(),
            )
            model, best_iteration = self._train_sklearn(
                x_train,
                y_train,
                x_valid,
                y_valid,
                num_boost_round=num_boost_round,
                early_stopping_rounds=early_stopping_rounds,
                seed=seed,
            )

        pred = self._predict_raw(model, x_test, best_iteration)
        metrics = self._eval_metrics(
            y_test,
            np.asarray(pred, dtype=float),
            n_train=n_train,
            n_valid=len(valid_df),
            n_test=len(test_df),
            target=target,
            best_iteration=int(best_iteration or 0),
            feature_columns=features,
        )

        self._model = model
        self._backend = backend
        self._meta = {
            "target": target,
            "feature_columns": features,
            "metrics": metrics.to_dict(),
            "params": params if backend == "lightgbm" else {
                "backend": "sklearn_hist_gradient_boosting",
                "learning_rate": 0.05,
                "max_iter": num_boost_round,
                "max_leaf_nodes": 31,
                "min_samples_leaf": 10,
                "seed": seed,
            },
            "backend": backend,
        }
        self.save()
        logger.info(
            "功率模型已训练: backend={}, target={}, n={}, MAE={:.3f}, RMSE={:.3f}, R2={:.4f}",
            backend,
            target,
            n,
            metrics.mae,
            metrics.rmse,
            metrics.r2,
        )
        return metrics

    @staticmethod
    def _train_lightgbm(
        x_train: np.ndarray,
        y_train: np.ndarray,
        x_valid: np.ndarray,
        y_valid: np.ndarray,
        *,
        features: list[str],
        params: dict[str, Any],
        num_boost_round: int,
        early_stopping_rounds: int,
    ) -> tuple[Any, int]:
        lgb = _require_lightgbm()
        train_set = lgb.Dataset(x_train, label=y_train, feature_name=features, free_raw_data=False)
        valid_set = lgb.Dataset(
            x_valid, label=y_valid, reference=train_set, feature_name=features, free_raw_data=False
        )
        callbacks = [
            lgb.early_stopping(early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=0),
        ]
        model = lgb.train(
            params,
            train_set,
            num_boost_round=num_boost_round,
            valid_sets=[valid_set],
            valid_names=["valid"],
            callbacks=callbacks,
        )
        return model, int(model.best_iteration or 0)

    @staticmethod
    def _train_sklearn(
        x_train: np.ndarray,
        y_train: np.ndarray,
        x_valid: np.ndarray,
        y_valid: np.ndarray,
        *,
        num_boost_round: int,
        early_stopping_rounds: int,
        seed: int,
    ) -> tuple[_SklearnPowerBackend, int]:
        from sklearn.ensemble import HistGradientBoostingRegressor

        est = HistGradientBoostingRegressor(
            loss="squared_error",
            learning_rate=0.05,
            max_iter=max(50, int(num_boost_round)),
            max_leaf_nodes=31,
            min_samples_leaf=10,
            early_stopping=True,
            validation_fraction=None,
            n_iter_no_change=max(5, int(early_stopping_rounds)),
            random_state=seed,
        )
        # sklearn 1.4+：用显式验证集做早停
        try:
            est.fit(x_train, y_train, X_val=x_valid, y_val=y_valid)
        except TypeError:
            est.set_params(validation_fraction=0.15, early_stopping=True)
            x_all = np.vstack([x_train, x_valid])
            y_all = np.concatenate([y_train, y_valid])
            est.fit(x_all, y_all)
        best_iteration = int(getattr(est, "n_iter_", 0) or 0)
        return _SklearnPowerBackend(est, best_iteration=best_iteration), best_iteration

    @staticmethod
    def _predict_raw(model: Any, x: Any, best_iteration: int = 0) -> np.ndarray:
        if isinstance(model, _SklearnPowerBackend):
            return model.predict(x)
        return np.asarray(
            model.predict(x, num_iteration=best_iteration or getattr(model, "best_iteration", 0)),
            dtype=np.float64,
        )
    @staticmethod
    def _eval_metrics(
        y_true: np.ndarray,
        y_pred: np.ndarray,
        *,
        n_train: int,
        n_valid: int,
        n_test: int,
        target: str,
        best_iteration: int,
        feature_columns: list[str],
    ) -> LightGBMTrainMetrics:
        err = y_pred - y_true
        mae = float(np.mean(np.abs(err)))
        rmse = float(np.sqrt(np.mean(err**2)))
        denom = np.clip(np.abs(y_true), 1e-6, None)
        mape = float(np.mean(np.abs(err) / denom) * 100.0)
        ss_res = float(np.sum(err**2))
        ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
        return LightGBMTrainMetrics(
            n_train=n_train,
            n_valid=n_valid,
            n_test=n_test,
            mae=round(mae, 4),
            rmse=round(rmse, 4),
            mape=round(mape, 4),
            r2=round(r2, 6),
            target=target,
            trained_at=datetime.now(timezone.utc).isoformat(),
            feature_columns=list(feature_columns),
            best_iteration=best_iteration,
        )

    # ---------- 预测 ----------

    def predict_one(self, row: dict[str, Any]) -> LightGBMPredictResult:
        if not self.is_ready:
            raise RuntimeError("模型未训练或未加载，请先调用 /api/v1/ml-power/train")
        df = self.rows_to_dataframe([row])
        cols = self.feature_columns
        x = df[cols].to_numpy(dtype=np.float64, copy=True)
        best_iter = int(getattr(self._model, "best_iteration", 0) or 0)
        pred = float(self._predict_raw(self._model, x, best_iter)[0])
        if not math.isfinite(pred):
            pred = 0.0
        features_used = {c: float(df.iloc[0][c]) for c in cols}
        return LightGBMPredictResult(
            predicted_power=round(max(pred, 0.0), 3),
            target=self.target,
            model_loaded=True,
            features_used=features_used,
        )

    def predict_many(self, rows: list[dict[str, Any]]) -> list[float]:
        if not self.is_ready:
            raise RuntimeError("模型未训练或未加载")
        if not rows:
            return []
        df = self.rows_to_dataframe(rows)
        best_iter = int(getattr(self._model, "best_iteration", 0) or 0)
        pred = self._predict_raw(
            self._model,
            df[self.feature_columns].to_numpy(dtype=np.float64, copy=True),
            best_iter,
        )
        return [round(max(float(v), 0.0), 3) for v in pred]

    # ---------- 持久化 ----------

    def save(self) -> None:
        if self._model is None:
            raise RuntimeError("无模型可保存")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(self._model, _SklearnPowerBackend):
            import joblib

            joblib.dump(self._model.estimator, self.sklearn_model_path)
            # 避免旧 LightGBM 文本模型误加载
            if self.model_path.exists():
                try:
                    self.model_path.unlink()
                except OSError:
                    pass
        else:
            self._model.save_model(str(self.model_path))
            if self.sklearn_model_path.exists():
                try:
                    self.sklearn_model_path.unlink()
                except OSError:
                    pass
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(self._meta, f, ensure_ascii=False, indent=2)

    def _load_if_exists(self) -> None:
        if self.meta_path.exists():
            try:
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    self._meta = json.load(f) or {}
            except Exception as e:
                logger.warning("读取功率模型元数据失败: {}", e)
                self._meta = {}

        backend = str(self._meta.get("backend") or "")
        if self.sklearn_model_path.exists() and (backend == "sklearn" or not self.model_path.exists()):
            try:
                import joblib

                est = joblib.load(self.sklearn_model_path)
                best_iteration = int((self._meta.get("metrics") or {}).get("best_iteration") or 0)
                self._model = _SklearnPowerBackend(est, best_iteration=best_iteration)
                self._backend = "sklearn"
                logger.info("已加载 sklearn 功率模型: {}", self.sklearn_model_path)
                return
            except Exception as e:
                logger.warning("加载 sklearn 功率模型失败: {}", e)
                self._model = None
                self._backend = ""

        if not self.model_path.exists():
            return
        if not _lightgbm_available():
            logger.warning("检测到已保存的 LightGBM 模型，但未安装 lightgbm 包")
            return
        try:
            import lightgbm as lgb

            self._model = lgb.Booster(model_file=str(self.model_path))
            self._backend = "lightgbm"
            if not self._meta:
                self._meta = {
                    "target": TARGET_TOTAL,
                    "feature_columns": list(FEATURE_COLUMNS),
                    "metrics": {},
                    "backend": "lightgbm",
                }
            logger.info("已加载 LightGBM 功率模型: {}", self.model_path)
        except Exception as e:
            logger.warning("加载 LightGBM 模型失败: {}", e)
            self._model = None
            self._backend = ""
            self._meta = {}


def _lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401

        return True
    except Exception:
        return False


def _lgbm_num_threads() -> int:
    """Linux 现场可用多线程；Windows 开发机强制单线程降低崩溃概率。"""
    import os
    import sys

    if sys.platform.startswith("win"):
        return 1
    raw = os.environ.get("AC_LGBM_NUM_THREADS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, (os.cpu_count() or 2) - 0)


def _prefer_lightgbm_native() -> bool:
    """是否优先尝试 LightGBM 原生训练。

    策略：
    - 默认优先 LightGBM（Linux 现场与已修复的 Windows 环境）
    - 可用 AC_ML_BACKEND=lightgbm|sklearn 覆盖
    - 训练失败时仍会自动回退 sklearn
    """
    import os

    if not _lightgbm_available():
        return False
    backend = os.environ.get("AC_ML_BACKEND", "").strip().lower()
    if backend == "sklearn":
        return False
    if backend == "lightgbm":
        return True
    if os.environ.get("AC_DISABLE_LIGHTGBM_NATIVE", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("AC_FORCE_LIGHTGBM", "").strip().lower() in ("1", "true", "yes"):
        return True
    return True


def _backend_policy_reason() -> str:
    """说明当前为何优先 / 不优先 LightGBM。"""
    import os

    if not _lightgbm_available():
        return "未安装 lightgbm 包"
    if os.environ.get("AC_ML_BACKEND", "").strip().lower() == "sklearn":
        return "AC_ML_BACKEND=sklearn"
    if os.environ.get("AC_DISABLE_LIGHTGBM_NATIVE", "").strip().lower() in ("1", "true", "yes"):
        return "AC_DISABLE_LIGHTGBM_NATIVE=1"
    if os.environ.get("AC_ML_BACKEND", "").strip().lower() == "lightgbm":
        return "AC_ML_BACKEND=lightgbm"
    if os.environ.get("AC_FORCE_LIGHTGBM", "").strip().lower() in ("1", "true", "yes"):
        return "AC_FORCE_LIGHTGBM=1"
    return "默认优先 LightGBM（失败自动回退 sklearn）"


# 兼容旧测试名
def _lightgbm_native_ok() -> bool:
    return _prefer_lightgbm_native()


def _require_lightgbm():
    try:
        import lightgbm as lgb

        return lgb
    except Exception as e:
        raise RuntimeError(
            "未安装 lightgbm，请执行: pip install lightgbm scikit-learn"
        ) from e
