from pathlib import Path
import hashlib
import pandas as pd
import pytest

from ml.model import Predictor
from ml.feature_builder import build_v5_row


def test_v5_artifact_has_runtime_schema_and_catboost_model():
    predictor = Predictor("artifacts")
    assert predictor.kind == "v5"
    assert predictor.meta["version"] == "champion-b4b"
    assert predictor.meta["training"]["reference_commit"] == "b4b636b"
    assert predictor.meta["target_mode"] == "direct_delay"
    assert predictor.meta["ensemble"] is False
    assert len(predictor.v5_features) == 60
    assert "tr_id" not in predictor.v5_features
    assert "target_stop_id" not in predictor.v5_features
    assert predictor.model.feature_names_ == predictor.v5_features


def test_runtime_builds_causal_row_and_prediction():
    root=Path("dataset")
    point = pd.read_csv(root/"labels/labels_test.csv").iloc[0].to_dict()
    traffic = pd.read_csv(root/"test/traffic.csv")
    history = traffic[traffic.tr_id.astype(str) == str(point["tr_id"])]
    schedule = pd.read_csv(root/"test/schedule.csv")
    schedule = schedule[schedule.tr_id.astype(str) == str(point["tr_id"])]

    row = build_v5_row(point, history.to_dict("records"), schedule.to_dict("records"))
    prediction = Predictor("artifacts").predict(row)

    assert row.shape == (1, 62)
    assert prediction.shape == (1,)
    assert row.loc[0, "history_events"] == 392
    assert row.loc[0, "target_distance_m"] == pytest.approx(92.742748, abs=1e-6)
    assert row.loc[0, "w900_displacement_m"] == pytest.approx(89.544161, abs=1e-6)
    expected = pd.read_csv(Path("artifacts")/"test_predictions.csv").iloc[0]["prediction"]
    assert prediction[0] == pytest.approx(expected, abs=1e-12)


def test_v5_rejects_target_outside_causal_horizon():
    point = pd.read_csv(Path("dataset")/"labels/labels_test.csv").iloc[0].to_dict()
    point["target_time_begin"] = point["T"]
    with pytest.raises(ValueError, match="окне"):
        build_v5_row(point, [], [])


def test_submission_matches_b4b_reference_champion():
    submission = pd.read_csv(Path("artifacts")/"submission.csv", sep=";")
    payload = "\n".join(
        f"{sample_id};{float(prediction):.17g}"
        for sample_id, prediction in zip(
            submission.sample_id.astype(str), submission.prediction
        )
    )
    assert len(submission) == 151
    assert submission.sample_id.is_unique
    assert submission.prediction.notna().all()
    assert hashlib.sha256(payload.encode()).hexdigest() == (
        "53c01c78bcbbf6dbc209f65dcfc7363df9fed2bcb30908a76cd7365fee6012cf"
    )
