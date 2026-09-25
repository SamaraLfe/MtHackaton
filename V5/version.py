import re
import pickle
import numpy as np
import pandas as pd

from catboost import CatBoostRegressor
from sklearn.metrics import mean_absolute_error


# ============================================================
# CONFIG
# ============================================================

TRAIN_LABELS_PATH = "labels/labels_train.csv"
TEST_LABELS_PATH = "labels/labels_test.csv"

TRAIN_TRAFFIC_PATH = "train/train_traffic.csv"
TEST_TRAFFIC_PATH = "test/test_traffic.csv"

TRAIN_SCHEDULE_PATH = "train/train_schedule.csv"
TEST_SCHEDULE_PATH = "test/test_schedule.csv"

VALIDATE_POINTS_PATH = "validate/points.csv"
VALIDATE_TRAFFIC_PATH = "validate/traffic.csv"
VALIDATE_SCHEDULE_PATH = "validate/schedule_plan.csv"

SAMPLE_SUBMISSION_PATH = "sample_submission.csv"

MODEL_PATH = "v5_model.cbm"
FEATURE_SCHEMA_PATH = "v5_features.pkl"

TEST_PREDICTIONS_PATH = "test_predictions_v5.csv"
FEATURE_IMPORTANCE_PATH = "feature_importance_v5.csv"
SUBMISSION_PATH = "submission_v5.csv"

EARTH_RADIUS_M = 6_371_000.0

WINDOWS_MINUTES = [1, 3, 5, 10]

RANDOM_SEED = 42
N_TREES = 1500

V3_MAE = 53.311325
V41_MAE = 52.995


# ============================================================
# FEATURE SCHEMAS
#
# В V5 задаём их явно.
# Train / Test / Validate обязаны иметь одинаковые признаки.
# ============================================================

ROLLING_BASE_COLUMNS = [
    "has_gps",
    "lat",
    "lon",
    "alt",
    "speed",
    "heading",
    "gps_age_s",
    "distance_to_target_m",
]

ROLLING_WINDOW_METRICS = [
    "count",
    "speed_mean",
    "speed_median",
    "speed_min",
    "speed_max",
    "speed_std",
    "stopped_fraction",
    "moving_fraction",
    "distance_travelled_m",
    "target_progress_m",
    "progress_speed_kmh",
]

ROLLING_COLUMNS = (
    ROLLING_BASE_COLUMNS
    + [
        f"{metric}_{window}m"
        for window in WINDOWS_MINUTES
        for metric in ROLLING_WINDOW_METRICS
    ]
)

ROUTE_COLUMNS = [
    "target_schedule_index",
    "schedule_total_stops",
    "target_position_ratio",

    "prev_stop_lat",
    "prev_stop_lon",
    "planned_headway_prev_s",
    "distance_target_prev_stop_m",

    "next_stop_lat",
    "next_stop_lon",
    "planned_headway_next_s",
    "distance_target_next_stop_m",

    "stops_after_T_to_target",

    "nearest_schedule_stop_distance_m",
    "nearest_schedule_stop_index",
    "stops_remaining_est",
    "schedule_progress_ratio",

    "route_distance_to_target_m",
    "route_distance_ratio",
    "avg_segment_length_to_target_m",

    "required_route_speed_kmh",
    "planned_seconds_per_remaining_stop",
]


# ============================================================
# HELPERS
# ============================================================

def empty_feature_dict(columns):
    """
    Создаём полный набор признаков заранее.
    Так DataFrame schema не зависит от конкретного dataset.
    """
    return {
        col: np.nan
        for col in columns
    }


def normalize_bool(series):
    """
    location_valid может приехать как bool или строка.
    """
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)

    return (
        series
        .astype(str)
        .str.strip()
        .str.lower()
        .isin(["true", "1", "yes"])
    )


# ============================================================
# GEO
# ============================================================

def haversine_m(
    lat1,
    lon1,
    lat2,
    lon2,
):
    """
    Vectorized Haversine distance.
    Работает и со scalar, и с numpy/pandas.
    """

    lat1 = np.radians(lat1)
    lon1 = np.radians(lon1)

    lat2 = np.radians(lat2)
    lon2 = np.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        np.sin(dlat / 2.0) ** 2
        + np.cos(lat1)
        * np.cos(lat2)
        * np.sin(dlon / 2.0) ** 2
    )

    a = np.clip(
        a,
        0.0,
        1.0,
    )

    c = 2.0 * np.arctan2(
        np.sqrt(a),
        np.sqrt(1.0 - a),
    )

    return EARTH_RADIUS_M * c


def bearing_deg(
    lat1,
    lon1,
    lat2,
    lon2,
):
    """
    Bearing current position -> target.
    """

    lat1_r = np.radians(lat1)
    lat2_r = np.radians(lat2)

    dlon_r = np.radians(
        lon2 - lon1
    )

    x = (
        np.sin(dlon_r)
        * np.cos(lat2_r)
    )

    y = (
        np.cos(lat1_r)
        * np.sin(lat2_r)
        - np.sin(lat1_r)
        * np.cos(lat2_r)
        * np.cos(dlon_r)
    )

    result = np.degrees(
        np.arctan2(
            x,
            y,
        )
    )

    return (
        result + 360.0
    ) % 360.0


# ============================================================
# WKT
# ============================================================

def parse_point(value):
    """
    Expected:
        POINT (37.43070705 55.8040083)

    WKT:
        POINT (lon lat)
    """

    if pd.isna(value):
        return np.nan, np.nan

    match = re.search(
        r"POINT\s*\(\s*"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
        r"\s+"
        r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
        r"\s*\)",
        str(value),
        flags=re.IGNORECASE,
    )

    if match is None:
        return np.nan, np.nan

    lon = float(
        match.group(1)
    )

    lat = float(
        match.group(2)
    )

    return lon, lat


# ============================================================
# LOAD
# ============================================================

print("Loading data...")

train = pd.read_csv(
    TRAIN_LABELS_PATH
)

test = pd.read_csv(
    TEST_LABELS_PATH
)

validate = pd.read_csv(
    VALIDATE_POINTS_PATH
)


train_traffic = pd.read_csv(
    TRAIN_TRAFFIC_PATH,
    low_memory=False,
)

test_traffic = pd.read_csv(
    TEST_TRAFFIC_PATH,
    low_memory=False,
)

validate_traffic = pd.read_csv(
    VALIDATE_TRAFFIC_PATH,
    low_memory=False,
)


train_schedule = pd.read_csv(
    TRAIN_SCHEDULE_PATH,
    low_memory=False,
)

test_schedule = pd.read_csv(
    TEST_SCHEDULE_PATH,
    low_memory=False,
)

validate_schedule = pd.read_csv(
    VALIDATE_SCHEDULE_PATH,
    low_memory=False,
)


print("\nShapes:")

print(
    "Train:",
    train.shape
)

print(
    "Test:",
    test.shape
)

print(
    "Validate:",
    validate.shape
)

print(
    "Train traffic:",
    train_traffic.shape
)

print(
    "Test traffic:",
    test_traffic.shape
)

print(
    "Validate traffic:",
    validate_traffic.shape
)

print(
    "Train schedule:",
    train_schedule.shape
)

print(
    "Test schedule:",
    test_schedule.shape
)

print(
    "Validate schedule:",
    validate_schedule.shape
)


# ============================================================
# REQUIRED COLUMN CHECK
# ============================================================

POINT_REQUIRED_COLUMNS = {
    "sample_id",
    "tr_id",
    "T",
    "target_stop_id",
    "target_time_begin",
    "cur_dev_s",
}

TRAFFIC_REQUIRED_COLUMNS = {
    "tr_id",
    "event_time",
    "location_valid",
    "lat",
    "lon",
    "alt",
    "speed",
    "heading",
}

SCHEDULE_REQUIRED_COLUMNS = {
    "tt_action_item_id",
    "tr_id",
    "time_begin",
    "geom",
}


def check_columns(
    df,
    required,
    name,
):

    missing = (
        required
        - set(df.columns)
    )

    if missing:
        raise ValueError(
            f"{name}: missing columns: "
            f"{sorted(missing)}"
        )


check_columns(
    train,
    POINT_REQUIRED_COLUMNS
    | {"target_delay_s"},
    "train labels",
)

check_columns(
    test,
    POINT_REQUIRED_COLUMNS
    | {"target_delay_s"},
    "test labels",
)

check_columns(
    validate,
    POINT_REQUIRED_COLUMNS,
    "validate points",
)


for df, name in [
    (
        train_traffic,
        "train traffic",
    ),
    (
        test_traffic,
        "test traffic",
    ),
    (
        validate_traffic,
        "validate traffic",
    ),
]:

    check_columns(
        df,
        TRAFFIC_REQUIRED_COLUMNS,
        name,
    )


for df, name in [
    (
        train_schedule,
        "train schedule",
    ),
    (
        test_schedule,
        "test schedule",
    ),
    (
        validate_schedule,
        "validate schedule",
    ),
]:

    check_columns(
        df,
        SCHEDULE_REQUIRED_COLUMNS,
        name,
    )


# ============================================================
# DATETIME + IDs
# ============================================================

for df in [
    train,
    test,
    validate,
]:

    df["T"] = pd.to_datetime(
        df["T"]
    )

    df["target_time_begin"] = (
        pd.to_datetime(
            df["target_time_begin"]
        )
    )

    df["tr_id"] = (
        df["tr_id"]
        .astype(str)
    )

    df["target_stop_id"] = (
        df["target_stop_id"]
        .astype(str)
    )


for df in [
    train_traffic,
    test_traffic,
    validate_traffic,
]:

    df["event_time"] = (
        pd.to_datetime(
            df["event_time"]
        )
    )

    df["tr_id"] = (
        df["tr_id"]
        .astype(str)
    )


# ============================================================
# PREPARE TRAFFIC
# ============================================================

def prepare_traffic(df):

    df = df.copy()

    df["location_valid"] = (
        normalize_bool(
            df["location_valid"]
        )
    )

    valid = df[
        df["location_valid"]
        & df["lat"].notna()
        & df["lon"].notna()
    ].copy()

    valid = (
        valid
        .sort_values(
            [
                "tr_id",
                "event_time",
            ]
        )
        .reset_index(drop=True)
    )

    return valid


train_gps = prepare_traffic(
    train_traffic
)

test_gps = prepare_traffic(
    test_traffic
)

validate_gps = prepare_traffic(
    validate_traffic
)


print("\nValid GPS:")

print(
    f"Train:    "
    f"{len(train_gps)} / "
    f"{len(train_traffic)} "
    f"({len(train_gps)/len(train_traffic):.2%})"
)

print(
    f"Test:     "
    f"{len(test_gps)} / "
    f"{len(test_traffic)} "
    f"({len(test_gps)/len(test_traffic):.2%})"
)

print(
    f"Validate: "
    f"{len(validate_gps)} / "
    f"{len(validate_traffic)} "
    f"({len(validate_gps)/len(validate_traffic):.2%})"
)


# ============================================================
# PREPARE SCHEDULE
#
# ВАЖНО:
# time_fact_begin НЕ ИСПОЛЬЗУЕМ.
# ============================================================

def prepare_schedule(df):

    df = df.copy()

    df["tr_id"] = (
        df["tr_id"]
        .astype(str)
    )

    df["target_stop_id"] = (
        df["tt_action_item_id"]
        .astype(str)
    )

    df["time_begin"] = (
        pd.to_datetime(
            df["time_begin"]
        )
    )

    coords = (
        df["geom"]
        .apply(parse_point)
    )

    df["stop_lon"] = (
        coords.apply(
            lambda x: x[0]
        )
    )

    df["stop_lat"] = (
        coords.apply(
            lambda x: x[1]
        )
    )

    df = (
        df
        .sort_values(
            [
                "tr_id",
                "time_begin",
            ]
        )
        .reset_index(drop=True)
    )

    return df


train_schedule = prepare_schedule(
    train_schedule
)

test_schedule = prepare_schedule(
    test_schedule
)

validate_schedule = prepare_schedule(
    validate_schedule
)


print("\nSchedule geometry coverage:")

print(
    "Train:",
    f"{train_schedule['stop_lat'].notna().mean():.2%}"
)

print(
    "Test:",
    f"{test_schedule['stop_lat'].notna().mean():.2%}"
)

print(
    "Validate:",
    f"{validate_schedule['stop_lat'].notna().mean():.2%}"
)


# ============================================================
# TARGET COORDINATES
#
# Join preferably by:
# tr_id + target_stop_id + target_time_begin/time_begin
#
# Это надёжнее, чем только stop ID, если ID повторяется.
# ============================================================

def add_target_coordinates(
    points,
    schedule,
):

    result = points.copy()

    schedule_lookup = (
        schedule[
            [
                "tr_id",
                "target_stop_id",
                "time_begin",
                "stop_lat",
                "stop_lon",
            ]
        ]
        .rename(
            columns={
                "time_begin":
                    "target_time_begin",

                "stop_lat":
                    "target_lat",

                "stop_lon":
                    "target_lon",
            }
        )
    )

    # Exact trip/time match first.
    result = result.merge(
        schedule_lookup,
        on=[
            "tr_id",
            "target_stop_id",
            "target_time_begin",
        ],
        how="left",
        validate="many_to_one",
    )

    return result


train = add_target_coordinates(
    train,
    train_schedule
)

test = add_target_coordinates(
    test,
    test_schedule
)

validate = add_target_coordinates(
    validate,
    validate_schedule
)


print("\nTarget geometry coverage:")

print(
    "Train:",
    f"{train['target_lat'].notna().mean():.2%}"
)

print(
    "Test:",
    f"{test['target_lat'].notna().mean():.2%}"
)

print(
    "Validate:",
    f"{validate['target_lat'].notna().mean():.2%}"
)


# We expect complete target matching.
assert (
    train["target_lat"]
    .notna()
    .all()
)

assert (
    test["target_lat"]
    .notna()
    .all()
)

assert (
    validate["target_lat"]
    .notna()
    .all()
)


# ============================================================
# BASIC FEATURES
# ============================================================

def make_basic_features(df):

    X = pd.DataFrame(
        index=df.index
    )

    X["cur_dev_s"] = (
        df["cur_dev_s"]
    )

    X["horizon_s"] = (
        df["target_time_begin"]
        - df["T"]
    ).dt.total_seconds()

    X["hour"] = (
        df["T"].dt.hour
    )

    X["minute"] = (
        df["T"].dt.minute
    )

    X["second"] = (
        df["T"].dt.second
    )

    X["minute_of_day"] = (
        df["T"].dt.hour * 60
        + df["T"].dt.minute
        + df["T"].dt.second / 60.0
    )

    X["day_of_week"] = (
        df["T"].dt.dayofweek
    )

    X["target_hour"] = (
        df["target_time_begin"]
        .dt.hour
    )

    X["target_minute"] = (
        df["target_time_begin"]
        .dt.minute
    )

    X["target_minute_of_day"] = (
        df["target_time_begin"].dt.hour * 60
        + df["target_time_begin"].dt.minute
        + df["target_time_begin"].dt.second / 60.0
    )

    X["time_sin"] = np.sin(
        2.0
        * np.pi
        * X["minute_of_day"]
        / 1440.0
    )

    X["time_cos"] = np.cos(
        2.0
        * np.pi
        * X["minute_of_day"]
        / 1440.0
    )

    X["tr_id"] = (
        df["tr_id"]
        .astype(str)
    )

    X["target_stop_id"] = (
        df["target_stop_id"]
        .astype(str)
    )

    return X


# ============================================================
# TELEMETRY FEATURES
#
# V2 + V3 + V4
#
# Для каждой prediction point:
# ONLY event_time <= T
# ============================================================

def build_telemetry_features(
    points,
    gps,
    dataset_name,
):

    print(
        f"\nBuilding telemetry features: "
        f"{dataset_name}"
    )

    gps_groups = {
        tr_id:
            group
            .sort_values("event_time")
            .reset_index(drop=True)

        for tr_id, group
        in gps.groupby(
            "tr_id",
            sort=False
        )
    }

    features = []

    total = len(points)

    future_violations = 0

    for row_num, (_, row) in enumerate(
        points.iterrows()
    ):

        if (
            row_num % 500 == 0
            or row_num == total - 1
        ):

            print(
                f"  {row_num + 1}/{total}"
            )

        # Full schema from the beginning.
        f = empty_feature_dict(
            ROLLING_COLUMNS
        )

        f["has_gps"] = 0

        tr_id = str(
            row["tr_id"]
        )

        T = row["T"]

        target_lat = (
            row["target_lat"]
        )

        target_lon = (
            row["target_lon"]
        )

        vehicle = (
            gps_groups.get(
                tr_id
            )
        )

        if (
            vehicle is None
            or vehicle.empty
        ):
            features.append(f)
            continue

        # ---------------------------------------------
        # Efficient cutoff:
        # rightmost event_time <= T
        # ---------------------------------------------

        event_times = (
            vehicle["event_time"]
            .to_numpy(
                dtype="datetime64[ns]"
            )
        )

        cutoff = np.searchsorted(
            event_times,
            np.datetime64(T),
            side="right",
        )

        if cutoff == 0:
            features.append(f)
            continue

        history = (
            vehicle.iloc[
                :cutoff
            ]
        )

        last = history.iloc[-1]

        # Safety.
        if last["event_time"] > T:
            future_violations += 1
            features.append(f)
            continue

        f["has_gps"] = 1

        f["lat"] = (
            last["lat"]
        )

        f["lon"] = (
            last["lon"]
        )

        f["alt"] = (
            last["alt"]
        )

        f["speed"] = (
            last["speed"]
        )

        f["heading"] = (
            last["heading"]
        )

        f["gps_age_s"] = (
            T
            - last["event_time"]
        ).total_seconds()

        current_distance = (
            haversine_m(
                last["lat"],
                last["lon"],
                target_lat,
                target_lon,
            )
        )

        f[
            "distance_to_target_m"
        ] = current_distance

        # ---------------------------------------------
        # ROLLING WINDOWS
        # ---------------------------------------------

        for window in WINDOWS_MINUTES:

            prefix = (
                f"{window}m"
            )

            start_time = (
                T
                - pd.Timedelta(
                    minutes=window
                )
            )

            # history already guarantees <= T
            w = history[
                history["event_time"]
                >= start_time
            ]

            f[
                f"count_{prefix}"
            ] = len(w)

            if w.empty:
                continue

            speed = (
                pd.to_numeric(
                    w["speed"],
                    errors="coerce",
                )
            )

            f[
                f"speed_mean_{prefix}"
            ] = speed.mean()

            f[
                f"speed_median_{prefix}"
            ] = speed.median()

            f[
                f"speed_min_{prefix}"
            ] = speed.min()

            f[
                f"speed_max_{prefix}"
            ] = speed.max()

            f[
                f"speed_std_{prefix}"
            ] = speed.std()

            valid_speed = (
                speed.notna()
            )

            if valid_speed.any():

                f[
                    f"stopped_fraction_{prefix}"
                ] = (
                    speed[
                        valid_speed
                    ] < 1.0
                ).mean()

                f[
                    f"moving_fraction_{prefix}"
                ] = (
                    speed[
                        valid_speed
                    ] >= 5.0
                ).mean()

            # -----------------------------------------
            # DISTANCE TRAVELLED
            # -----------------------------------------

            if len(w) >= 2:

                lat1 = (
                    w["lat"]
                    .iloc[:-1]
                    .to_numpy(
                        dtype=float
                    )
                )

                lon1 = (
                    w["lon"]
                    .iloc[:-1]
                    .to_numpy(
                        dtype=float
                    )
                )

                lat2 = (
                    w["lat"]
                    .iloc[1:]
                    .to_numpy(
                        dtype=float
                    )
                )

                lon2 = (
                    w["lon"]
                    .iloc[1:]
                    .to_numpy(
                        dtype=float
                    )
                )

                segments = (
                    haversine_m(
                        lat1,
                        lon1,
                        lat2,
                        lon2,
                    )
                )

                travelled = float(
                    np.nansum(
                        segments
                    )
                )

            else:

                travelled = 0.0

            f[
                f"distance_travelled_m_{prefix}"
            ] = travelled

            # -----------------------------------------
            # TARGET PROGRESS
            # -----------------------------------------

            first = w.iloc[0]

            start_distance = (
                haversine_m(
                    first["lat"],
                    first["lon"],
                    target_lat,
                    target_lon,
                )
            )

            progress = (
                start_distance
                - current_distance
            )

            f[
                f"target_progress_m_{prefix}"
            ] = progress

            elapsed_s = (
                last["event_time"]
                - first["event_time"]
            ).total_seconds()

            if elapsed_s > 0:

                f[
                    f"progress_speed_kmh_{prefix}"
                ] = (
                    progress
                    / elapsed_s
                    * 3.6
                )

        features.append(f)

    result = pd.DataFrame(
        features,
        columns=ROLLING_COLUMNS,
        index=points.index,
    )

    print(
        f"{dataset_name} telemetry schema:",
        result.shape,
    )

    print(
        f"{dataset_name} future violations:",
        future_violations,
    )

    assert future_violations == 0

    print(
        f"{dataset_name} GPS coverage:",
        f"{result['has_gps'].mean():.2%}"
    )

    return result


train_roll = build_telemetry_features(
    train,
    train_gps,
    "TRAIN",
)

test_roll = build_telemetry_features(
    test,
    test_gps,
    "TEST",
)

validate_roll = build_telemetry_features(
    validate,
    validate_gps,
    "VALIDATE",
)


# ============================================================
# TELEMETRY SCHEMA CHECK
# ============================================================

assert (
    train_roll.columns.tolist()
    == test_roll.columns.tolist()
    == validate_roll.columns.tolist()
)

print(
    "\nTelemetry schema alignment: OK"
)

print(
    "Telemetry features:",
    len(
        train_roll.columns
    )
)


# ============================================================
# V5 ROUTE FEATURES
#
# Только PLAN schedule.
#
# Мы не используем фактические времена прибытия.
# ============================================================

def build_route_features(
    points,
    schedule,
    telemetry,
    dataset_name,
):

    print(
        f"\nBuilding V5 route features: "
        f"{dataset_name}"
    )

    schedule_groups = {
        tr_id:
            group
            .sort_values("time_begin")
            .reset_index(drop=True)

        for tr_id, group
        in schedule.groupby(
            "tr_id",
            sort=False
        )
    }

    features = []

    total = len(points)

    target_not_found = 0

    for row_num, (idx, row) in enumerate(
        points.iterrows()
    ):

        if (
            row_num % 500 == 0
            or row_num == total - 1
        ):

            print(
                f"  {row_num + 1}/{total}"
            )

        f = empty_feature_dict(
            ROUTE_COLUMNS
        )

        tr_id = str(
            row["tr_id"]
        )

        target_id = str(
            row["target_stop_id"]
        )

        T = row["T"]

        target_time = (
            row["target_time_begin"]
        )

        schedule_tr = (
            schedule_groups.get(
                tr_id
            )
        )

        if (
            schedule_tr is None
            or schedule_tr.empty
        ):

            target_not_found += 1
            features.append(f)
            continue

        # ---------------------------------------------
        # Locate exact target.
        #
        # Use ID + closest planned time.
        # ---------------------------------------------

        candidate_positions = np.flatnonzero(
            (
                schedule_tr[
                    "target_stop_id"
                ].to_numpy()
                == target_id
            )
        )

        if (
            len(candidate_positions)
            == 0
        ):

            target_not_found += 1
            features.append(f)
            continue

        candidate_times = (
            schedule_tr.iloc[
                candidate_positions
            ]["time_begin"]
        )

        time_diff = np.abs(
            (
                candidate_times
                - target_time
            ).dt.total_seconds()
            .to_numpy()
        )

        best_candidate = int(
            np.nanargmin(
                time_diff
            )
        )

        target_pos = int(
            candidate_positions[
                best_candidate
            ]
        )

        # ---------------------------------------------
        # Exact target planned time consistency
        # ---------------------------------------------

        matched_target = (
            schedule_tr.iloc[
                target_pos
            ]
        )

        target_time_error_s = abs(
            (
                matched_target[
                    "time_begin"
                ]
                - target_time
            ).total_seconds()
        )

        # This isn't a feature.
        # It's just a sanity check.
        if target_time_error_s > 1:
            # Still continue: some source timestamps may
            # theoretically differ slightly.
            pass

        # ---------------------------------------------
        # General route position
        # ---------------------------------------------

        f[
            "target_schedule_index"
        ] = target_pos

        f[
            "schedule_total_stops"
        ] = len(
            schedule_tr
        )

        f[
            "target_position_ratio"
        ] = (
            target_pos
            / max(
                len(schedule_tr) - 1,
                1
            )
        )

        # ---------------------------------------------
        # Previous target-adjacent stop
        # ---------------------------------------------

        if target_pos > 0:

            prev_stop = (
                schedule_tr.iloc[
                    target_pos - 1
                ]
            )

            f[
                "prev_stop_lat"
            ] = prev_stop[
                "stop_lat"
            ]

            f[
                "prev_stop_lon"
            ] = prev_stop[
                "stop_lon"
            ]

            f[
                "planned_headway_prev_s"
            ] = (
                target_time
                - prev_stop[
                    "time_begin"
                ]
            ).total_seconds()

            f[
                "distance_target_prev_stop_m"
            ] = haversine_m(
                row["target_lat"],
                row["target_lon"],
                prev_stop["stop_lat"],
                prev_stop["stop_lon"],
            )

        # ---------------------------------------------
        # Next target-adjacent stop
        # ---------------------------------------------

        if (
            target_pos
            < len(schedule_tr) - 1
        ):

            next_stop = (
                schedule_tr.iloc[
                    target_pos + 1
                ]
            )

            f[
                "next_stop_lat"
            ] = next_stop[
                "stop_lat"
            ]

            f[
                "next_stop_lon"
            ] = next_stop[
                "stop_lon"
            ]

            f[
                "planned_headway_next_s"
            ] = (
                next_stop[
                    "time_begin"
                ]
                - target_time
            ).total_seconds()

            f[
                "distance_target_next_stop_m"
            ] = haversine_m(
                row["target_lat"],
                row["target_lon"],
                next_stop["stop_lat"],
                next_stop["stop_lon"],
            )

        # ---------------------------------------------
        # Planned stops whose planned time is after T
        # and no later than target.
        # ---------------------------------------------

        to_target = (
            schedule_tr.iloc[
                :target_pos + 1
            ]
        )

        future_to_target = (
            to_target[
                (
                    to_target[
                        "time_begin"
                    ] > T
                )
                &
                (
                    to_target[
                        "time_begin"
                    ] <= target_time
                )
            ]
        )

        f[
            "stops_after_T_to_target"
        ] = len(
            future_to_target
        )

        # ---------------------------------------------
        # Current GPS
        # ---------------------------------------------

        current_lat = (
            telemetry.loc[
                idx,
                "lat"
            ]
        )

        current_lon = (
            telemetry.loc[
                idx,
                "lon"
            ]
        )

        if (
            pd.isna(current_lat)
            or pd.isna(current_lon)
        ):

            features.append(f)
            continue

        # ---------------------------------------------
        # IMPORTANT:
        #
        # Search nearest schedule stop only among stops
        # at/before target.
        #
        # This is PLAN geometry, not factual future data.
        # ---------------------------------------------

        candidate_stops = (
            schedule_tr.iloc[
                :target_pos + 1
            ]
        )

        valid_geometry = (
            candidate_stops[
                "stop_lat"
            ].notna()
            &
            candidate_stops[
                "stop_lon"
            ].notna()
        )

        if not valid_geometry.any():

            features.append(f)
            continue

        valid_positions = (
            np.flatnonzero(
                valid_geometry.to_numpy()
            )
        )

        valid_stops = (
            candidate_stops.iloc[
                valid_positions
            ]
        )

        distances = (
            haversine_m(
                current_lat,
                current_lon,
                valid_stops[
                    "stop_lat"
                ].to_numpy(
                    dtype=float
                ),
                valid_stops[
                    "stop_lon"
                ].to_numpy(
                    dtype=float
                ),
            )
        )

        nearest_in_valid = int(
            np.nanargmin(
                distances
            )
        )

        nearest_local_pos = int(
            valid_positions[
                nearest_in_valid
            ]
        )

        nearest_distance = float(
            distances[
                nearest_in_valid
            ]
        )

        f[
            "nearest_schedule_stop_distance_m"
        ] = nearest_distance

        f[
            "nearest_schedule_stop_index"
        ] = nearest_local_pos

        stops_remaining = max(
            target_pos
            - nearest_local_pos,
            0,
        )

        f[
            "stops_remaining_est"
        ] = stops_remaining

        f[
            "schedule_progress_ratio"
        ] = (
            nearest_local_pos
            / max(
                target_pos,
                1
            )
        )

        # ---------------------------------------------
        # APPROXIMATE ROUTE DISTANCE
        #
        # GPS -> nearest planned stop
        # +
        # sum stop-to-stop distances until target.
        # ---------------------------------------------

        route_distance = (
            nearest_distance
        )

        if (
            nearest_local_pos
            < target_pos
        ):

            segment = (
                schedule_tr.iloc[
                    nearest_local_pos:
                    target_pos + 1
                ]
            )

            segment = segment[
                segment["stop_lat"].notna()
                & segment["stop_lon"].notna()
            ]

            if len(segment) >= 2:

                segment_distances = (
                    haversine_m(
                        segment["stop_lat"]
                        .iloc[:-1]
                        .to_numpy(
                            dtype=float
                        ),

                        segment["stop_lon"]
                        .iloc[:-1]
                        .to_numpy(
                            dtype=float
                        ),

                        segment["stop_lat"]
                        .iloc[1:]
                        .to_numpy(
                            dtype=float
                        ),

                        segment["stop_lon"]
                        .iloc[1:]
                        .to_numpy(
                            dtype=float
                        ),
                    )
                )

                route_distance += float(
                    np.nansum(
                        segment_distances
                    )
                )

        f[
            "route_distance_to_target_m"
        ] = route_distance

        # ---------------------------------------------
        # Route/direct ratio
        # ---------------------------------------------

        direct_distance = (
            telemetry.loc[
                idx,
                "distance_to_target_m"
            ]
        )

        if (
            pd.notna(
                direct_distance
            )
            and direct_distance > 1.0
        ):

            f[
                "route_distance_ratio"
            ] = (
                route_distance
                / direct_distance
            )

        # ---------------------------------------------
        # Average distance per remaining segment
        # ---------------------------------------------

        if stops_remaining > 0:

            f[
                "avg_segment_length_to_target_m"
            ] = (
                route_distance
                / stops_remaining
            )

        # ---------------------------------------------
        # Required route speed
        # ---------------------------------------------

        horizon_s = (
            target_time - T
        ).total_seconds()

        if horizon_s > 0:

            f[
                "required_route_speed_kmh"
            ] = (
                route_distance
                / horizon_s
                * 3.6
            )

        if (
            stops_remaining > 0
            and horizon_s > 0
        ):

            f[
                "planned_seconds_per_remaining_stop"
            ] = (
                horizon_s
                / stops_remaining
            )

        features.append(f)

    result = pd.DataFrame(
        features,
        columns=ROUTE_COLUMNS,
        index=points.index,
    )

    print(
        f"{dataset_name} route schema:",
        result.shape,
    )

    print(
        f"{dataset_name} target not found:",
        target_not_found,
    )

    return result


train_route = build_route_features(
    train,
    train_schedule,
    train_roll,
    "TRAIN",
)

test_route = build_route_features(
    test,
    test_schedule,
    test_roll,
    "TEST",
)

validate_route = build_route_features(
    validate,
    validate_schedule,
    validate_roll,
    "VALIDATE",
)


# ============================================================
# ROUTE SCHEMA CHECK
# ============================================================

assert (
    train_route.columns.tolist()
    == test_route.columns.tolist()
    == validate_route.columns.tolist()
)

print(
    "\nRoute schema alignment: OK"
)

print(
    "Route features:",
    len(
        train_route.columns
    )
)


# ============================================================
# FINAL FEATURE MATRIX
# ============================================================

def build_feature_matrix(
    points,
    telemetry,
    route,
):

    X = make_basic_features(
        points
    )

    # ========================================================
    # V2 — last telemetry
    # ========================================================

    X["lat"] = (
        telemetry["lat"]
    )

    X["lon"] = (
        telemetry["lon"]
    )

    X["alt"] = (
        telemetry["alt"]
    )

    X["speed"] = (
        telemetry["speed"]
    )

    X["gps_age_s"] = (
        telemetry["gps_age_s"]
    )

    X["has_gps"] = (
        telemetry["has_gps"]
    )

    heading = (
        telemetry["heading"]
    )

    heading_rad = np.deg2rad(
        heading
    )

    X["heading_sin"] = np.sin(
        heading_rad
    )

    X["heading_cos"] = np.cos(
        heading_rad
    )

    X["speed_is_zero"] = (
        telemetry["speed"].notna()
        & (
            telemetry["speed"]
            < 1.0
        )
    ).astype(int)

    # ========================================================
    # V3 — target geometry
    # ========================================================

    X["target_lat"] = (
        points["target_lat"]
    )

    X["target_lon"] = (
        points["target_lon"]
    )

    X["distance_to_target_m"] = (
        telemetry[
            "distance_to_target_m"
        ]
    )

    X["distance_to_target_km"] = (
        X["distance_to_target_m"]
        / 1000.0
    )

    horizon_s = (
        points["target_time_begin"]
        - points["T"]
    ).dt.total_seconds()

    valid_horizon = (
        horizon_s > 0
    )

    X["required_speed_kmh"] = np.nan

    X.loc[
        valid_horizon,
        "required_speed_kmh"
    ] = (
        X.loc[
            valid_horizon,
            "distance_to_target_m"
        ]
        / horizon_s.loc[
            valid_horizon
        ]
        * 3.6
    )

    current_speed_ms = (
        telemetry["speed"]
        / 3.6
    )

    valid_speed = (
        current_speed_ms > 0.5
    )

    X[
        "eta_current_speed_s"
    ] = np.nan

    X.loc[
        valid_speed,
        "eta_current_speed_s"
    ] = (
        X.loc[
            valid_speed,
            "distance_to_target_m"
        ]
        / current_speed_ms.loc[
            valid_speed
        ]
    )

    X[
        "eta_minus_horizon_s"
    ] = (
        X["eta_current_speed_s"]
        - horizon_s
    )

    bearing = bearing_deg(
        telemetry["lat"],
        telemetry["lon"],
        points["target_lat"],
        points["target_lon"],
    )

    bearing_rad = np.deg2rad(
        bearing
    )

    X["bearing_sin"] = np.sin(
        bearing_rad
    )

    X["bearing_cos"] = np.cos(
        bearing_rad
    )

    X["heading_error_deg"] = np.abs(
        (
            heading
            - bearing
            + 180.0
        ) % 360.0 - 180.0
    )

    X["heading_target_cos"] = (
        np.cos(
            np.deg2rad(
                heading
                - bearing
            )
        )
    )

    # ========================================================
    # GPS freshness
    # ========================================================

    gps_age = (
        telemetry[
            "gps_age_s"
        ]
    )

    X["gps_age_log"] = np.log1p(
        gps_age.clip(
            lower=0
        )
    )

    X["gps_fresh_30s"] = (
        gps_age.notna()
        & (
            gps_age <= 30
        )
    ).astype(int)

    X["gps_fresh_60s"] = (
        gps_age.notna()
        & (
            gps_age <= 60
        )
    ).astype(int)

    X["gps_stale_5min"] = (
        gps_age.notna()
        & (
            gps_age > 300
        )
    ).astype(int)

    X["gps_stale_15min"] = (
        gps_age.notna()
        & (
            gps_age > 900
        )
    ).astype(int)

    # ========================================================
    # V4 rolling
    # ========================================================

    excluded = set(
        ROLLING_BASE_COLUMNS
    )

    for col in ROLLING_COLUMNS:

        if col not in excluded:

            X[col] = (
                telemetry[col]
            )

    # ========================================================
    # V5 route
    # ========================================================

    for col in ROUTE_COLUMNS:

        X[
            f"route_{col}"
        ] = route[col]

    return X


X_train = build_feature_matrix(
    train,
    train_roll,
    train_route,
)

X_test = build_feature_matrix(
    test,
    test_roll,
    test_route,
)

X_validate = build_feature_matrix(
    validate,
    validate_roll,
    validate_route,
)


y_train = (
    train[
        "target_delay_s"
    ]
    .astype(float)
)

y_test = (
    test[
        "target_delay_s"
    ]
    .astype(float)
)


# ============================================================
# FINAL SCHEMA DIAGNOSTICS
# ============================================================

print(
    "\n================ FEATURE SCHEMA ================"
)

print(
    "Train:",
    X_train.shape
)

print(
    "Test:",
    X_test.shape
)

print(
    "Validate:",
    X_validate.shape
)


train_cols = (
    X_train.columns.tolist()
)

test_cols = (
    X_test.columns.tolist()
)

validate_cols = (
    X_validate.columns.tolist()
)


print(
    "\nOnly in TRAIN vs TEST:",
    sorted(
        set(train_cols)
        - set(test_cols)
    )
)

print(
    "Only in TEST vs TRAIN:",
    sorted(
        set(test_cols)
        - set(train_cols)
    )
)

print(
    "Only in TRAIN vs VALIDATE:",
    sorted(
        set(train_cols)
        - set(validate_cols)
    )
)

print(
    "Only in VALIDATE vs TRAIN:",
    sorted(
        set(validate_cols)
        - set(train_cols)
    )
)


# Train schema is authoritative.
X_test = X_test.reindex(
    columns=X_train.columns
)

X_validate = (
    X_validate.reindex(
        columns=X_train.columns
    )
)


assert (
    X_train.columns.tolist()
    == X_test.columns.tolist()
)

assert (
    X_train.columns.tolist()
    == X_validate.columns.tolist()
)


print(
    "\nFeature schema alignment: OK"
)

print(
    "Total features:",
    X_train.shape[1]
)


# ============================================================
# CATEGORICAL SAFETY
#
# CatBoost dislikes NaN categorical values.
# ============================================================

CAT_FEATURES = [
    "tr_id",
    "target_stop_id",
]


for X in [
    X_train,
    X_test,
    X_validate,
]:

    for col in CAT_FEATURES:

        X[col] = (
            X[col]
            .fillna("__MISSING__")
            .astype(str)
        )


# ============================================================
# INFINITY SAFETY
# ============================================================

for X in [
    X_train,
    X_test,
    X_validate,
]:

    numeric_cols = (
        X.select_dtypes(
            include=[
                np.number
            ]
        ).columns
    )

    X[
        numeric_cols
    ] = (
        X[
            numeric_cols
        ]
        .replace(
            [
                np.inf,
                -np.inf,
            ],
            np.nan,
        )
    )


# ============================================================
# MISSING VALUE REPORT
# ============================================================

print(
    "\nTEST missing features TOP 15:"
)

print(
    X_test
    .isna()
    .sum()
    .sort_values(
        ascending=False
    )
    .head(15)
)


print(
    "\nVALIDATE missing features TOP 15:"
)

print(
    X_validate
    .isna()
    .sum()
    .sort_values(
        ascending=False
    )
    .head(15)
)


# ============================================================
# BASELINES
# ============================================================

baseline_test = (
    mean_absolute_error(
        y_test,
        test["cur_dev_s"],
    )
)

print(
    "\ncur_dev_s TEST MAE:",
    f"{baseline_test:.3f}"
)


# ============================================================
# V5 MODEL
#
# IMPORTANT:
# fixed 1500 trees.
#
# Test is NOT eval_set.
# Test does NOT determine best iteration.
# ============================================================

print(
    "\n================ V5 TRAIN ================"
)


model = CatBoostRegressor(

    loss_function="MAE",

    iterations=N_TREES,

    learning_rate=0.03,

    depth=6,

    l2_leaf_reg=5,

    random_seed=RANDOM_SEED,

    verbose=100,

    allow_writing_files=False,
)


model.fit(
    X_train,
    y_train,
    cat_features=CAT_FEATURES,
)


# ============================================================
# LOCAL TEST
# ============================================================

test_prediction = (
    model.predict(
        X_test
    )
)


test_mae = (
    mean_absolute_error(
        y_test,
        test_prediction,
    )
)


print("\n")
print("=" * 65)
print("V5 LOCAL RESULT")
print("=" * 65)

print(
    f"cur_dev_s:    "
    f"{baseline_test:.3f} sec"
)

print(
    f"V3:           "
    f"{V3_MAE:.3f} sec"
)

print(
    f"V4.1:         "
    f"{V41_MAE:.3f} sec"
)

print(
    f"V5:           "
    f"{test_mae:.3f} sec"
)

print("-" * 65)

print(
    f"V5 vs V4.1:   "
    f"{V41_MAE - test_mae:+.3f} sec"
)

print(
    f"V5 vs V3:     "
    f"{V3_MAE - test_mae:+.3f} sec"
)

print("=" * 65)


# ============================================================
# FEATURE IMPORTANCE
# ============================================================

importance = pd.DataFrame(
    {
        "feature":
            X_train.columns,

        "importance":
            model.get_feature_importance(),
    }
)

importance = (
    importance
    .sort_values(
        "importance",
        ascending=False,
    )
    .reset_index(drop=True)
)


print(
    "\n=========== TOP 50 FEATURES ==========="
)

print(
    importance
    .head(50)
    .to_string(
        index=False
    )
)


print(
    "\n=========== V5 ROUTE FEATURES ==========="
)

route_importance = (
    importance[
        importance[
            "feature"
        ].str.startswith(
            "route_"
        )
    ]
)


print(
    route_importance
    .to_string(
        index=False
    )
)


# ============================================================
# TEST ERROR ANALYSIS
# ============================================================

test_result = (
    test[
        [
            "sample_id",
            "tr_id",
            "T",
            "target_stop_id",
            "cur_dev_s",
            "target_delay_s",
        ]
    ]
    .copy()
)


test_result[
    "prediction"
] = test_prediction


test_result[
    "abs_error"
] = np.abs(
    test_result[
        "target_delay_s"
    ]
    - test_result[
        "prediction"
    ]
)


test_result[
    "baseline_abs_error"
] = np.abs(
    test_result[
        "target_delay_s"
    ]
    - test_result[
        "cur_dev_s"
    ]
)


test_result[
    "improvement_vs_baseline"
] = (
    test_result[
        "baseline_abs_error"
    ]
    - test_result[
        "abs_error"
    ]
)


print(
    "\nTEST error percentiles:"
)

print(
    test_result[
        "abs_error"
    ].describe(
        percentiles=[
            0.50,
            0.75,
            0.90,
            0.95,
            0.99,
        ]
    )
)


# ============================================================
# VALIDATE PREDICTION
# ============================================================

print(
    "\nPredicting VALIDATE..."
)


validate_prediction = (
    model.predict(
        X_validate
    )
)


validate_prediction = (
    np.asarray(
        validate_prediction,
        dtype=float,
    )
)


assert (
    len(validate_prediction)
    == len(validate)
)

assert (
    np.isfinite(
        validate_prediction
    ).all()
)


print(
    "\nValidate prediction statistics:"
)

print(
    pd.Series(
        validate_prediction
    ).describe(
        percentiles=[
            0.01,
            0.05,
            0.25,
            0.50,
            0.75,
            0.95,
            0.99,
        ]
    )
)


# ============================================================
# COMPARE VALIDATE PREDICTION WITH cur_dev_s
#
# Target неизвестен, поэтому это только sanity check.
# ============================================================

validate_delta = (
    validate_prediction
    - validate[
        "cur_dev_s"
    ].to_numpy(
        dtype=float
    )
)


print(
    "\nValidate prediction - cur_dev_s:"
)

print(
    pd.Series(
        validate_delta
    ).describe(
        percentiles=[
            0.01,
            0.05,
            0.25,
            0.50,
            0.75,
            0.95,
            0.99,
        ]
    )
)


# ============================================================
# SAVE LOCAL RESULTS
# ============================================================

test_result.to_csv(
    TEST_PREDICTIONS_PATH,
    index=False,
)


importance.to_csv(
    FEATURE_IMPORTANCE_PATH,
    index=False,
)


# ============================================================
# SAMPLE SUBMISSION
#
# README:
# delimiter = ;
# columns = sample_id;prediction
# ============================================================

sample_submission = pd.read_csv(
    SAMPLE_SUBMISSION_PATH,
    sep=";",
)


print(
    "\nSample submission shape:",
    sample_submission.shape
)

print(
    "Sample submission columns:",
    sample_submission.columns.tolist()
)


required_submission_columns = {
    "sample_id",
    "prediction",
}


if (
    set(
        sample_submission.columns
    )
    != required_submission_columns
):

    raise ValueError(
        "Unexpected sample_submission columns: "
        f"{sample_submission.columns.tolist()}"
    )


# ============================================================
# BUILD PREDICTION TABLE
# ============================================================

prediction_table = pd.DataFrame(
    {
        "sample_id":
            validate[
                "sample_id"
            ].astype(str),

        "prediction":
            validate_prediction,
    }
)


if not (
    prediction_table[
        "sample_id"
    ].is_unique
):

    duplicated = (
        prediction_table[
            prediction_table[
                "sample_id"
            ].duplicated(
                keep=False
            )
        ]
    )

    raise ValueError(
        "Duplicate validate sample_id:\n"
        f"{duplicated}"
    )


# ============================================================
# TEMPLATE IS AUTHORITATIVE ORDER
# ============================================================

template = (
    sample_submission[
        [
            "sample_id"
        ]
    ]
    .copy()
)


template[
    "sample_id"
] = (
    template[
        "sample_id"
    ].astype(str)
)


submission = (
    template.merge(
        prediction_table,
        on="sample_id",
        how="left",
        validate="one_to_one",
    )
)


# ============================================================
# SUBMISSION CHECKS
# ============================================================

print(
    "\n================ SUBMISSION CHECKS ================"
)


assert (
    list(
        submission.columns
    )
    == [
        "sample_id",
        "prediction",
    ]
)


assert (
    len(submission)
    == len(
        sample_submission
    )
)


assert (
    len(submission)
    == len(validate)
)


assert (
    submission[
        "sample_id"
    ].is_unique
)


missing_predictions = (
    submission[
        "prediction"
    ].isna().sum()
)


assert (
    missing_predictions
    == 0
)


assert (
    np.isfinite(
        submission[
            "prediction"
        ].to_numpy(
            dtype=float
        )
    ).all()
)


template_ids = set(
    template[
        "sample_id"
    ]
)


validate_ids = set(
    validate[
        "sample_id"
    ].astype(str)
)


assert (
    template_ids
    == validate_ids
)


print(
    "Submission checks: OK"
)

print(
    "Rows:",
    len(submission)
)

print(
    "Unique sample_id:",
    submission[
        "sample_id"
    ].nunique()
)

print(
    "Missing predictions:",
    missing_predictions
)


# ============================================================
# SAVE SUBMISSION
# ============================================================

submission.to_csv(
    SUBMISSION_PATH,
    sep=";",
    index=False,
    encoding="utf-8",
)


# ============================================================
# SAVE MODEL
# ============================================================

model.save_model(
    MODEL_PATH
)


# ============================================================
# SAVE FEATURE SCHEMA
#
# Это пригодится для inference / retraining.
# ============================================================

feature_metadata = {

    "version":
        "V5",

    "features":
        X_train.columns.tolist(),

    "categorical_features":
        CAT_FEATURES,

    "rolling_columns":
        ROLLING_COLUMNS,

    "route_columns":
        ROUTE_COLUMNS,

    "windows_minutes":
        WINDOWS_MINUTES,

    "iterations":
        N_TREES,

    "learning_rate":
        0.03,

    "depth":
        6,

    "l2_leaf_reg":
        5,

    "random_seed":
        RANDOM_SEED,
}


with open(
    FEATURE_SCHEMA_PATH,
    "wb",
) as file:

    pickle.dump(
        feature_metadata,
        file,
    )


# ============================================================
# FINAL OUTPUT
# ============================================================

print(
    "\n================ SAVED ================"
)

print(
    f" - {MODEL_PATH}"
)

print(
    f" - {FEATURE_SCHEMA_PATH}"
)

print(
    f" - {FEATURE_IMPORTANCE_PATH}"
)

print(
    f" - {TEST_PREDICTIONS_PATH}"
)

print(
    f" - {SUBMISSION_PATH}"
)


print(
    "\nSubmission preview:"
)

print(
    submission
    .head(10)
    .to_string(
        index=False
    )
)


print(
    "\nDone."
)