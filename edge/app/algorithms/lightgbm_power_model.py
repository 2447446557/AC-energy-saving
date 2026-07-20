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


class LightGBMPowerModel:
    """LightGBM 功率回归封装：训练 / 预测 / 落盘。"""

    def __init__(self, model_dir: str | Path | None = None) -> None:
        root = Path(model_dir) if model_dir else Path("data/ml")
        self.model_dir = root
        self.model_path = root / "lightgbm_power_model.txt"
        self.meta_path = root / "lightgbm_power_meta.json"
        self._model = None
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
        return {
            "ready": self.is_ready,
            "model_path": str(self.model_path.resolve()) if self.model_path.exists() else "",
            "target": self.target,
            "feature_columns": self.feature_columns,
            "metrics": self.metrics,
            "lightgbm_available": _lightgbm_available(),
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
        """按时间顺序切分训练/验证/测试，训练 LightGBM 回归模型。"""
        lgb = _require_lightgbm()
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
        x_train = train_df[features]
        y_train = train_df[target]
        x_valid = valid_df[features]
        y_valid = valid_df[target]
        x_test = test_df[features]
        y_test = test_df[target]

        train_set = lgb.Dataset(x_train, label=y_train, feature_name=features)
        valid_set = lgb.Dataset(x_valid, label=y_valid, reference=train_set, feature_name=features)

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
        }
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

        pred = model.predict(x_test, num_iteration=model.best_iteration)
        metrics = self._eval_metrics(
            y_test.to_numpy(dtype=float),
            np.asarray(pred, dtype=float),
            n_train=n_train,
            n_valid=len(valid_df),
            n_test=len(test_df),
            target=target,
            best_iteration=int(model.best_iteration or 0),
            feature_columns=features,
        )

        self._model = model
        self._meta = {
            "target": target,
            "feature_columns": features,
            "metrics": metrics.to_dict(),
            "params": params,
        }
        self.save()
        logger.info(
            "LightGBM 功率模型已训练: target={}, n={}, MAE={:.3f}, RMSE={:.3f}, R2={:.4f}",
            target,
            n,
            metrics.mae,
            metrics.rmse,
            metrics.r2,
        )
        return metrics

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
        x = df[cols]
        pred = float(self._model.predict(x, num_iteration=self._model.best_iteration)[0])
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
        pred = self._model.predict(df[self.feature_columns], num_iteration=self._model.best_iteration)
        return [round(max(float(v), 0.0), 3) for v in pred]

    # ---------- 持久化 ----------

    def save(self) -> None:
        if self._model is None:
            raise RuntimeError("无模型可保存")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._model.save_model(str(self.model_path))
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(self._meta, f, ensure_ascii=False, indent=2)

    def _load_if_exists(self) -> None:
        if not self.model_path.exists():
            return
        if not _lightgbm_available():
            logger.warning("检测到已保存的 LightGBM 模型，但未安装 lightgbm 包")
            return
        try:
            import lightgbm as lgb

            self._model = lgb.Booster(model_file=str(self.model_path))
            if self.meta_path.exists():
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    self._meta = json.load(f) or {}
            else:
                self._meta = {
                    "target": TARGET_TOTAL,
                    "feature_columns": list(FEATURE_COLUMNS),
                    "metrics": {},
                }
            logger.info("已加载 LightGBM 功率模型: {}", self.model_path)
        except Exception as e:
            logger.warning("加载 LightGBM 模型失败: {}", e)
            self._model = None
            self._meta = {}


def _lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401

        return True
    except Exception:
        return False


def _require_lightgbm():
    try:
        import lightgbm as lgb

        return lgb
    except Exception as e:
        raise RuntimeError(
            "未安装 lightgbm，请执行: pip install lightgbm scikit-learn"
        ) from e
