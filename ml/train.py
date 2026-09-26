"""Reproduce, evaluate and package the strongest submitted CatBoost model."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import catboost
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from ml.feature_builder import FEATURE_COLUMNS, build_feature_table

MODEL_VERSION = "champion-b4b"
REFERENCE_COMMIT = "b4b636b"
LATE_THRESHOLD_S = 120.0
NOMINAL_COVERAGE = 0.90


def mae(actual, prediction) -> float:
    return float(np.mean(np.abs(np.asarray(actual, dtype=float) - np.asarray(prediction, dtype=float))))


def conformal_radius(residuals, coverage: float = NOMINAL_COVERAGE) -> float:
    values = np.sort(np.abs(np.asarray(residuals, dtype=float)))
    rank = min(len(values) - 1, int(np.ceil((len(values) + 1) * coverage)) - 1)
    return float(values[rank])


def make_model() -> CatBoostRegressor:
    """Return the deterministic hyperparameters of the reference champion."""
    return CatBoostRegressor(
        iterations=500,
        depth=6,
        learning_rate=0.04,
        l2_leaf_reg=5,
        loss_function="MAE",
        eval_metric="MAE",
        random_seed=42,
        verbose=False,
        allow_writing_files=False,
        thread_count=4,
    )


def load_split(root: Path, split: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    if split == "train":
        points_path, schedule_name = root / "labels/labels_train.csv", "schedule.csv"
    elif split == "test":
        points_path, schedule_name = root / "labels/labels_test.csv", "schedule.csv"
    else:
        points_path, schedule_name = root / "validate/points.csv", "schedule_plan.csv"
    points = pd.read_csv(points_path)
    features = build_feature_table(
        points,
        pd.read_csv(root / split / "traffic.csv", low_memory=False),
        pd.read_csv(root / split / schedule_name, low_memory=False),
    )
    return points, features


def group_holdout(features: pd.DataFrame) -> dict[str, object]:
    """Evaluate transfer to unseen real vehicle IDs; synthetic clones are excluded."""
    real = features[pd.to_numeric(features["tr_id"]) < 9_000_000].copy()
    vehicles = np.array(sorted(real["tr_id"].astype(int).unique()))
    folds = []
    for number, held_out in enumerate(np.array_split(vehicles, 5), start=1):
        validation = real["tr_id"].astype(int).isin(held_out)
        training = ~validation
        model = make_model()
        model.fit(real.loc[training, FEATURE_COLUMNS], real.loc[training, "target_delay_s"])
        prediction = model.predict(real.loc[validation, FEATURE_COLUMNS])
        truth = real.loc[validation, "target_delay_s"].to_numpy(dtype=float)
        persistence = real.loc[validation, "cur_dev_s"].to_numpy(dtype=float)
        folds.append({
            "fold": number,
            "vehicles": held_out.tolist(),
            "rows": int(validation.sum()),
            "mae_cur_dev_s": mae(truth, persistence),
            "mae_catboost": mae(truth, prediction),
        })
    rows = sum(item["rows"] for item in folds)
    return {
        "method": "5 folds by real tr_id; synthetic rows excluded",
        "folds": folds,
        "weighted": {
            "mae_cur_dev_s": sum(item["rows"] * item["mae_cur_dev_s"] for item in folds) / rows,
            "mae_catboost": sum(item["rows"] * item["mae_catboost"] for item in folds) / rows,
        },
    }


def semantic_submission_hash(frame: pd.DataFrame) -> str:
    payload = "\n".join(
        f"{sample_id};{float(prediction):.17g}"
        for sample_id, prediction in zip(frame["sample_id"].astype(str), frame["prediction"])
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="dataset")
    parser.add_argument("--out", default="artifacts")
    parser.add_argument("--skip-group-holdout", action="store_true")
    args = parser.parse_args()
    root, output = Path(args.data), Path(args.out)
    output.mkdir(parents=True, exist_ok=True)

    train_points, train = load_split(root, "train")
    test_points, test = load_split(root, "test")
    validate_points, validate = load_split(root, "validate")

    model = make_model()
    model.fit(
        train[FEATURE_COLUMNS], train["target_delay_s"],
        eval_set=(test[FEATURE_COLUMNS], test["target_delay_s"]),
        early_stopping_rounds=80, use_best_model=True,
    )
    if model.tree_count_ != 500:
        raise RuntimeError(f"Reference model must retain all 500 trees, got {model.tree_count_}")

    started = time.perf_counter()
    test_prediction = model.predict(test[FEATURE_COLUMNS])
    batch_ms = (time.perf_counter() - started) * 1_000.0
    validate_prediction = model.predict(validate[FEATURE_COLUMNS])
    truth = test["target_delay_s"].to_numpy(dtype=float)
    residuals = truth - test_prediction
    radius = conformal_radius(residuals)
    late_probability = np.asarray([
        (1 + np.sum(residuals > LATE_THRESHOLD_S - prediction)) / (len(residuals) + 2)
        for prediction in test_prediction
    ])
    actual_late = truth > LATE_THRESHOLD_S

    template = pd.read_csv(root / "sample_submission.csv", sep=";")
    submission = pd.DataFrame({
        "sample_id": validate_points["sample_id"].astype(str),
        "prediction": validate_prediction,
    }).set_index("sample_id").loc[template["sample_id"].astype(str)].reset_index()
    if submission["prediction"].isna().any() or not submission["sample_id"].is_unique:
        raise RuntimeError("Submission contract failed")

    metadata = {
        "selected": "v5",
        "version": MODEL_VERSION,
        "target_mode": "direct_delay",
        "ensemble": False,
        "features": FEATURE_COLUMNS,
        "calibration_residuals": residuals.tolist(),
        "interval_radius_s": radius,
        "late_threshold_s": LATE_THRESHOLD_S,
        "nominal_coverage": NOMINAL_COVERAGE,
        "training": {
            "rows": len(train), "iterations": model.tree_count_,
            "reference_commit": REFERENCE_COMMIT,
            "test_role": "labelled evaluation and non-binding early-stopping monitor",
            "identifiers_excluded": ["target_stop_id", "tr_id"],
        },
    }
    holdout = None if args.skip_group_holdout else group_holdout(train)
    report = {
        "model": MODEL_VERSION,
        "reference_commit": REFERENCE_COMMIT,
        "target_mode": "direct_delay",
        "rows": {"train": len(train), "test": len(test), "validate": len(validate)},
        "feature_count": len(FEATURE_COLUMNS),
        "mae_test": {
            "zero": mae(truth, np.zeros(len(test))),
            "cur_dev_s": mae(truth, test["cur_dev_s"]),
            "catboost": mae(truth, test_prediction),
        },
        "interval_coverage": float(np.mean(np.abs(residuals) <= radius)),
        "interval_radius_s": radius,
        "brier_score": float(np.mean((late_probability - actual_late) ** 2)),
        "batch_inference_ms": batch_ms,
        "best_iteration": model.tree_count_ - 1,
        "leaderboard": {
            "status": "user-reported best commit",
            "commit": REFERENCE_COMMIT,
            "score": 1.0,
        },
        "real_vehicle_group_holdout": holdout,
        "warning": (
            "The supplied test and validate telemetry files are identical and also occur in train. "
            "Test MAE is a pipeline diagnostic, not an independent estimate for a new day."
        ),
    }

    model.save_model(str(output / "model.cbm"))
    (output / "model.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "feature_schema.json").write_text(json.dumps({
        "version": MODEL_VERSION, "features": FEATURE_COLUMNS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    submission.to_csv(output / "submission.csv", sep=";", index=False)
    pd.DataFrame({
        "feature": FEATURE_COLUMNS, "importance": model.get_feature_importance(),
    }).sort_values("importance", ascending=False).to_csv(output / "feature_importance.csv", index=False)
    pd.DataFrame({
        "sample_id": test_points["sample_id"], "actual": truth,
        "cur_dev_s": test["cur_dev_s"], "prediction": test_prediction,
        "abs_error": np.abs(residuals), "lower_s": test_prediction - radius,
        "upper_s": test_prediction + radius, "late_probability": late_probability,
    }).to_csv(output / "test_predictions.csv", index=False)
    (output / "environment.json").write_text(json.dumps({
        "numpy": np.__version__, "pandas": pd.__version__, "catboost": catboost.__version__,
    }, indent=2), encoding="utf-8")
    (output / "verification.json").write_text(json.dumps({
        "reference_commit": REFERENCE_COMMIT,
        "submission_semantic_sha256": semantic_submission_hash(
            pd.read_csv(output / "submission.csv", sep=";")
        ),
        "rows": len(submission), "sample_ids_unique": bool(submission["sample_id"].is_unique),
        "finite_predictions": bool(np.isfinite(submission["prediction"]).all()),
    }, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
