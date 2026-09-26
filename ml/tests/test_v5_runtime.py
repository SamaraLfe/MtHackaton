from pathlib import Path
import pandas as pd
import pytest

from ml.model import Predictor
from ml.v5_features import build_v5_row


def test_v5_artifact_has_runtime_schema_and_catboost_model():
    predictor = Predictor("artifacts")
    assert predictor.kind == "v5"
    assert len(predictor.v5_features) == 102
    assert "tr_id" not in predictor.v5_features
    assert "target_stop_id" not in predictor.v5_features
    assert predictor.model.feature_names_ == predictor.v5_features


def test_v5_runtime_builds_causal_row_and_prediction():
    root=Path("dataset")
    point = pd.read_csv(root/"labels/labels_test.csv").iloc[0].to_dict()
    traffic = pd.read_csv(root/"test/traffic.csv")
    history = traffic[traffic.tr_id.astype(str) == str(point["tr_id"])]
    schedule = pd.read_csv(root/"test/schedule.csv")
    schedule = schedule[schedule.tr_id.astype(str) == str(point["tr_id"])]

    row = build_v5_row(point, history.to_dict("records"), schedule.to_dict("records"))
    prediction = Predictor("artifacts").predict(row)

    assert row.shape == (1, 104)
    assert prediction.shape == (1,)
    assert pd.notna(prediction[0])


def test_v5_rejects_target_outside_causal_horizon():
    point = pd.read_csv(Path("dataset")/"labels/labels_test.csv").iloc[0].to_dict()
    point["target_time_begin"] = point["T"]
    with pytest.raises(ValueError, match="окне"):
        build_v5_row(point, [], [])
