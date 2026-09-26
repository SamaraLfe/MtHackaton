"""Causal GPS features used by both training and online inference.

The builder intentionally contains no target or future telemetry leakage. For
every forecast point only packets with ``event_time <= T`` are visible. The
feature contract reproduces the strongest artifact from commit ``b4b636b``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

EARTH_RADIUS_M = 6_371_000.0
WINDOW_SECONDS = (60, 180, 300, 600, 900)

BASE_FEATURES = [
    "cur_dev_s", "horizon_s", "hour_sin", "hour_cos", "history_events",
    "last_valid_age_s", "last_speed", "last_heading_sin",
    "last_heading_cos", "target_distance_m",
]

WINDOW_FEATURES = [
    metric
    for window in WINDOW_SECONDS
    for metric in (
        f"w{window}_events", f"w{window}_valid_events",
        f"w{window}_valid_fraction", f"w{window}_speed_mean",
        f"w{window}_speed_median", f"w{window}_speed_std",
        f"w{window}_speed_min", f"w{window}_speed_max",
        f"w{window}_stopped_fraction", f"w{window}_displacement_m",
    )
]
FEATURE_COLUMNS = BASE_FEATURES + WINDOW_FEATURES


def normalize_bool(series: pd.Series) -> pd.Series:
    """Normalize boolean/string validity flags without treating NaN as true."""
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin(("true", "1", "yes"))


def parse_point(value: object) -> tuple[float, float]:
    """Parse a WKT ``POINT (lon lat)`` into ``(lat, lon)``."""
    match = re.search(
        r"POINT\s*\(\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*\)",
        str(value), flags=re.IGNORECASE,
    )
    if match is None:
        return np.nan, np.nan
    return float(match.group(2)), float(match.group(1))


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres for scalars or arrays."""
    lat1_r, lon1_r = np.radians(lat1), np.radians(lon1)
    lat2_r, lon2_r = np.radians(lat2), np.radians(lon2)
    value = (
        np.sin((lat2_r - lat1_r) / 2.0) ** 2
        + np.cos(lat1_r) * np.cos(lat2_r)
        * np.sin((lon2_r - lon1_r) / 2.0) ** 2
    )
    return EARTH_RADIUS_M * 2.0 * np.arcsin(np.sqrt(np.clip(value, 0.0, 1.0)))


def _utc(value: object) -> pd.Timestamp:
    result = pd.Timestamp(value)
    return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")


def prepare_traffic(frame: pd.DataFrame) -> pd.DataFrame:
    """Parse, validate and stably order raw telemetry packets."""
    result = frame.copy()
    required = {"tr_id", "event_time", "location_valid", "lat", "lon", "speed"}
    missing = required - set(result.columns)
    if missing:
        raise ValueError(f"Telemetry columns are missing: {sorted(missing)}")
    if "heading" not in result:
        result["heading"] = 0.0
    result["tr_id"] = result["tr_id"].astype(str)
    result["event_time"] = pd.to_datetime(result["event_time"], utc=True, format="mixed")
    result["_valid"] = (
        normalize_bool(result["location_valid"])
        & pd.to_numeric(result["lat"], errors="coerce").between(-90.0, 90.0)
        & pd.to_numeric(result["lon"], errors="coerce").between(-180.0, 180.0)
    )
    return result.sort_values(["tr_id", "event_time"], kind="mergesort").reset_index(drop=True)


def prepare_schedule(frame: pd.DataFrame) -> pd.DataFrame:
    """Parse the allow-listed planned fields used to locate a target stop."""
    result = frame.copy()
    required = {"tr_id", "tt_action_item_id", "time_begin"}
    missing = required - set(result.columns)
    if missing:
        raise ValueError(f"Schedule columns are missing: {sorted(missing)}")
    result["tr_id"] = result["tr_id"].astype(str)
    result["target_stop_id"] = result["tt_action_item_id"].astype(str)
    result["time_begin"] = pd.to_datetime(result["time_begin"], utc=True, format="mixed")
    if "geom" in result:
        coordinates = result["geom"].map(parse_point)
        result["stop_lat"] = coordinates.map(lambda pair: pair[0])
        result["stop_lon"] = coordinates.map(lambda pair: pair[1])
    elif {"lat", "lon"} <= set(result.columns):
        result["stop_lat"] = pd.to_numeric(result["lat"], errors="coerce")
        result["stop_lon"] = pd.to_numeric(result["lon"], errors="coerce")
    else:
        raise ValueError("Schedule requires geom or lat/lon coordinates")
    return result.sort_values(["tr_id", "time_begin"], kind="mergesort").reset_index(drop=True)


def _target_coordinates(point: Mapping[str, object], schedule: pd.DataFrame) -> tuple[float, float]:
    candidates = schedule[schedule["target_stop_id"] == str(point["target_stop_id"])].copy()
    if candidates.empty:
        return np.nan, np.nan
    candidates["_time_error"] = (candidates["time_begin"] - _utc(point["target_time_begin"])).abs()
    target = candidates.sort_values("_time_error", kind="mergesort").iloc[0]
    return float(target["stop_lat"]), float(target["stop_lon"])


def _empty_features() -> dict[str, float]:
    return {name: np.nan for name in FEATURE_COLUMNS}


def build_feature_row(
    point: Mapping[str, object],
    traffic: pd.DataFrame,
    schedule: pd.DataFrame,
    *,
    enforce_horizon: bool = True,
) -> dict[str, float]:
    """Build the 60-feature champion row for one forecast point."""
    prediction_time = _utc(point["T"])
    target_time = _utc(point["target_time_begin"])
    horizon_s = float((target_time - prediction_time).total_seconds())
    if enforce_horizon and not 600.0 < horizon_s <= 900.0:
        raise ValueError("Целевая остановка должна находиться в окне (T+10, T+15] минут")

    features = _empty_features()
    seconds_of_day = prediction_time.hour * 3600 + prediction_time.minute * 60 + prediction_time.second
    angle = 2.0 * np.pi * seconds_of_day / 86_400.0
    features.update(
        cur_dev_s=float(point["cur_dev_s"]), horizon_s=horizon_s,
        hour_sin=float(np.sin(angle)), hour_cos=float(np.cos(angle)),
    )

    history = traffic[traffic["event_time"] <= prediction_time]
    features["history_events"] = float(len(history))
    valid_history = history[history["_valid"]]
    target_lat, target_lon = _target_coordinates(point, schedule)
    if not valid_history.empty:
        last = valid_history.iloc[-1]
        heading = float(last["heading"])
        features.update(
            last_valid_age_s=float((prediction_time - last["event_time"]).total_seconds()),
            last_speed=float(last["speed"]),
            last_heading_sin=float(np.sin(np.radians(heading))),
            last_heading_cos=float(np.cos(np.radians(heading))),
            target_distance_m=float(haversine_m(last["lat"], last["lon"], target_lat, target_lon)),
        )

    for window_s in WINDOW_SECONDS:
        prefix = f"w{window_s}_"
        window = history[history["event_time"] > prediction_time - pd.Timedelta(seconds=window_s)]
        valid = window[window["_valid"]]
        speeds = pd.to_numeric(valid["speed"], errors="coerce")
        features[prefix + "events"] = float(len(window))
        features[prefix + "valid_events"] = float(len(valid))
        features[prefix + "valid_fraction"] = float(len(valid) / len(window)) if len(window) else 0.0
        if valid.empty:
            continue
        features[prefix + "speed_mean"] = float(speeds.mean())
        features[prefix + "speed_median"] = float(speeds.median())
        features[prefix + "speed_std"] = float(speeds.std(ddof=0))
        features[prefix + "speed_min"] = float(speeds.min())
        features[prefix + "speed_max"] = float(speeds.max())
        features[prefix + "stopped_fraction"] = float((speeds <= 2.0).mean())
        if len(valid) >= 2:
            first, last = valid.iloc[0], valid.iloc[-1]
            features[prefix + "displacement_m"] = float(
                haversine_m(first["lat"], first["lon"], last["lat"], last["lon"])
            )
    return features


def build_feature_table(
    points: pd.DataFrame,
    traffic: pd.DataFrame,
    schedule: pd.DataFrame,
    *,
    enforce_horizon: bool = True,
) -> pd.DataFrame:
    """Build an ordered feature table for a dataset split."""
    points = points.copy()
    points["tr_id"] = points["tr_id"].astype(str)
    traffic = prepare_traffic(traffic) if "_valid" not in traffic.columns else traffic.copy()
    schedule = prepare_schedule(schedule) if "target_stop_id" not in schedule.columns else schedule.copy()
    traffic_groups = {key: group for key, group in traffic.groupby("tr_id", sort=False)}
    schedule_groups = {key: group for key, group in schedule.groupby("tr_id", sort=False)}
    empty_traffic, empty_schedule = traffic.iloc[:0], schedule.iloc[:0]

    rows: list[dict[str, object]] = []
    for point in points.to_dict("records"):
        vehicle = str(point["tr_id"])
        row: dict[str, object] = {
            "sample_id": str(point["sample_id"]), "tr_id": float(point["tr_id"]),
        }
        row.update(build_feature_row(
            point, traffic_groups.get(vehicle, empty_traffic),
            schedule_groups.get(vehicle, empty_schedule), enforce_horizon=enforce_horizon,
        ))
        if "target_delay_s" in point:
            row["target_delay_s"] = float(point["target_delay_s"])
        rows.append(row)
    columns = ["sample_id", "tr_id", *FEATURE_COLUMNS]
    if "target_delay_s" in points.columns:
        columns.append("target_delay_s")
    return pd.DataFrame(rows, columns=columns)


def build_v5_row(
    point: Mapping[str, object],
    history: Iterable[Mapping[str, object]],
    schedule: Iterable[Mapping[str, object]],
) -> pd.DataFrame:
    """Runtime adapter used by the ML HTTP service for one raw request."""
    point_frame = pd.DataFrame([dict(point)])
    point_frame = point_frame.drop(columns=["target_delay_s"], errors="ignore")
    if "sample_id" not in point_frame:
        point_frame["sample_id"] = f"{point['tr_id']}_{int(_utc(point['T']).timestamp())}"
    traffic_frame = pd.DataFrame(list(history))
    schedule_frame = pd.DataFrame(list(schedule))
    if traffic_frame.empty:
        traffic_frame = pd.DataFrame(columns=(
            "tr_id", "event_time", "location_valid", "lat", "lon", "speed", "heading",
        ))
    if schedule_frame.empty:
        schedule_frame = pd.DataFrame(columns=(
            "tr_id", "tt_action_item_id", "time_begin", "geom",
        ))
    if "tr_id" not in traffic_frame:
        traffic_frame["tr_id"] = str(point["tr_id"])
    if "tr_id" not in schedule_frame:
        schedule_frame["tr_id"] = str(point["tr_id"])
    return build_feature_table(point_frame, traffic_frame, schedule_frame)
