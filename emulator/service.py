"""Controllable custom NDTP emulator for local QA and demo scenarios."""

from __future__ import annotations

import asyncio
import bisect
import csv
import logging
import math
import os
import re
import struct
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.ndtp import crc16


LOGGER = logging.getLogger("custom_ndtp_emulator")

NAV00_FLAGS = 64 | 32 | 128

POINT_PATTERN = re.compile(
    r"POINT\s*\(\s*([-+0-9.]+)\s+([-+0-9.]+)\s*\)"
)

EARTH_RADIUS_M = 6_371_000.0

CUSTOM_TR_ID_OFFSET = int(
    os.getenv(
        "CUSTOM_TR_ID_OFFSET",
        "1000000",
    )
)

CUSTOM_UNIT_ID_OFFSET = int(
    os.getenv(
        "CUSTOM_UNIT_ID_OFFSET",
        "100000000",
    )
)

runtime = {
    "paused": (
        os.getenv(
            "EMULATOR_START_PAUSED",
            "1",
        )
        == "1"
    ),
    "connected": False,
    "vehicles": 0,
    "batches_sent": 0,
    "packets_sent": 0,
    "last_sent_at": None,
    "last_error": None,
}

# Ephemeral debug overlay.  It lives only in the custom emulator process and
# changes neither the schedule nor the backend/ML calculations themselves.
debug_speed_overrides: dict[int, float] = {}
custom_vehicle_ids: set[int] = set()


class DebugSpeed(BaseModel):
    speed_kmh: float = Field(ge=0, le=130)


def build_nav00_frame(
    *,
    unit_id: int,
    event_time: datetime,
    lon: float,
    lat: float,
    speed_kmh: int,
) -> bytes:
    timestamp_s = int(
        event_time.timestamp()
    )

    cell = struct.pack(
        "<BBIIIBBHHHHHBB",
        0,
        0,
        timestamp_s,
        round(
            abs(lon)
            * 10_000_000
        ),
        round(
            abs(lat)
            * 10_000_000
        ),
        NAV00_FLAGS,
        0,
        speed_kmh,
        speed_kmh,
        0,
        0,
        0,
        8,
        1,
    )

    body = (
        struct.pack(
            "<HHHI",
            1,
            101,
            0,
            0,
        )
        + cell
    )

    checksum = crc16(body)

    stored_checksum = (
        (
            checksum
            & 0xFF
        )
        << 8
    ) | (
        checksum
        >> 8
    )

    header = struct.pack(
        "<HHHHBIH",
        0x7E7E,
        len(body),
        0,
        stored_checksum,
        2,
        unit_id,
        0,
    )

    return header + body


def _point(
    row: dict[str, str],
) -> tuple[float, float] | None:
    match = POINT_PATTERN.fullmatch(
        (
            row.get("geom")
            or ""
        ).strip()
    )

    if not match:
        return None

    return (
        float(
            match.group(1)
        ),
        float(
            match.group(2)
        ),
    )


def load_vehicles(
    dataset_dir: str | Path,
) -> list[dict]:
    dataset = Path(
        dataset_dir
    )

    units: dict[int, int] = {}

    with (
        dataset
        / "validate"
        / "traffic.csv"
    ).open(
        encoding="utf-8",
        newline="",
    ) as stream:
        for row in csv.DictReader(
            stream
        ):
            units.setdefault(
                int(
                    row["tr_id"]
                ),
                int(
                    row["unit_id"]
                ),
            )

    routes: dict[
        int,
        list[
            tuple[
                str,
                tuple[
                    float,
                    float,
                ],
            ]
        ],
    ] = {}

    with (
        dataset
        / "validate"
        / "schedule_plan.csv"
    ).open(
        encoding="utf-8",
        newline="",
    ) as stream:
        for row in csv.DictReader(
            stream
        ):
            point = _point(row)

            if point is None:
                continue

            routes.setdefault(
                int(
                    row["tr_id"]
                ),
                [],
            ).append(
                (
                    row[
                        "time_begin"
                    ],
                    point,
                )
            )

    return [
        {
            "base_tr_id": tr_id,

            "tr_id": (
                tr_id
                + CUSTOM_TR_ID_OFFSET
            ),

            "base_unit_id":
                units[tr_id],

            "unit_id": (
                units[tr_id]
                + CUSTOM_UNIT_ID_OFFSET
            ),

            "path": [
                point
                for _,
                point
                in sorted(
                    stops
                )
            ],
            # Relative plan timestamps let the custom source follow the same
            # ordered stop sequence that the backend uses for matching.  The
            # old constant-speed walk crossed a full day's stops in minutes,
            # which made a live position look tens of minutes early.
            "timings": [
                seconds
                for seconds, _ in _relative_route_timing(stops)
            ],
        }
        for tr_id, stops
        in sorted(
            routes.items()
        )
        if (
            tr_id in units
            and len(stops) >= 2
        )
    ]


def _relative_route_timing(
    stops: list[tuple[str, tuple[float, float]]],
) -> list[tuple[float, tuple[float, float]]]:
    """Return monotonic seconds from the first scheduled stop."""
    if not stops:
        return []
    parsed = []
    for value, point in sorted(stops):
        try:
            parsed.append((datetime.fromisoformat(value).replace(tzinfo=None), point))
        except ValueError:
            # The dataset is controlled, but retaining a monotonic fallback
            # keeps the emulator useful with a reduced fixture.
            parsed.append((None, point))
    first = next((value for value, _ in parsed if value is not None), None)
    if first is None:
        return [(float(index * 60), point) for index, (_, point) in enumerate(parsed)]
    result = []
    previous = 0.0
    for value, point in parsed:
        seconds = previous if value is None else max(0.0, (value - first).total_seconds())
        previous = max(previous, seconds)
        result.append((previous, point))
    return result


def haversine_m(
    lon_a: float,
    lat_a: float,
    lon_b: float,
    lat_b: float,
) -> float:
    lat_a, lon_a, lat_b, lon_b = map(
        math.radians,
        (
            lat_a,
            lon_a,
            lat_b,
            lon_b,
        ),
    )

    angle = (
        math.sin(
            (
                lat_b
                - lat_a
            )
            / 2
        )
        ** 2
        + math.cos(lat_a)
        * math.cos(lat_b)
        * math.sin(
            (
                lon_b
                - lon_a
            )
            / 2
        )
        ** 2
    )

    return (
        2
        * EARTH_RADIUS_M
        * math.asin(
            math.sqrt(
                angle
            )
        )
    )


def initial_vehicle_state(
    vehicle: dict,
) -> dict:
    lon, lat = (
        vehicle["path"][0]
    )

    timings = vehicle.get("timings") or []
    if len(timings) >= 2:
        first_gap = max(1.0, timings[1] - timings[0])
        first_distance = haversine_m(*vehicle["path"][0], *vehicle["path"][1])
        planned_speed = first_distance / first_gap * 3.6
        # A small deterministic pace difference creates a meaningful signed
        # deviation without detaching the vehicle from its planned trajectory.
        pace_factor = 0.94 + (vehicle["base_tr_id"] % 9) * 0.015
        initial_speed = min(130.0, max(1.0, planned_speed * pace_factor))
    else:
        pace_factor = 1.0
        initial_speed = float(22 + vehicle["tr_id"] % 13)

    return {
        "segment_index": 0,
        "segment_progress_m": 0.0,
        "lon": lon,
        "lat": lat,
        "speed_kmh": initial_speed,
        "elapsed_route_s": 0.0,
        "pace_factor": pace_factor,
    }


def debug_route_clock(vehicle: dict, elapsed: float, elapsed_s: float, speed_kmh: float) -> float:
    """Advance by metres, then map that distance back onto the plan clock.

    One pace multiplier is incorrect across segments with different planned
    speeds. Repeated coordinates are zero-distance segments, not a division
    by zero; an entirely stationary route keeps its current clock.
    """
    path, timings = vehicle['path'], vehicle['timings']
    if speed_kmh <= 0 or elapsed_s <= 0:
        return elapsed
    distances = vehicle.get('_debug_cumulative_m')
    if distances is None:
        distances = [0.0]
        for start, end in zip(path, path[1:]):
            distances.append(distances[-1] + haversine_m(*start, *end))
        vehicle['_debug_cumulative_m'] = distances
    if distances[-1] <= 0:
        return elapsed
    index = max(0, min(len(path)-2, bisect.bisect_right(timings, elapsed)-1))
    fraction = min(1.0, max(0.0, (elapsed-timings[index])/max(1.0, timings[index+1]-timings[index])))
    position = distances[index] + fraction*(distances[index+1]-distances[index])
    position += speed_kmh/3.6*elapsed_s
    if position > distances[-1]:
        position %= distances[-1]
    if position == distances[-1]:
        return timings[-1]
    index = max(0, min(len(path)-2, bisect.bisect_right(distances, position)-1))
    fraction = (position-distances[index])/(distances[index+1]-distances[index])
    return timings[index] + fraction*(timings[index+1]-timings[index])


def advance_vehicle(
    vehicle: dict,
    state: dict,
    *,
    elapsed_s: float,
) -> tuple[
    float,
    float,
    int,
]:
    path = vehicle["path"]

    timings = vehicle.get("timings") or []
    if len(timings) == len(path) and len(path) >= 2:
        # Follow the planned stop clock instead of traversing the entire day
        # at a fixed road speed.  This is still a synthetic source, but its
        # points now stay on the same segment the backend forecasts.
        route_duration = max(1.0, timings[-1])
        debug_speed = debug_speed_overrides.get(vehicle["tr_id"])
        if debug_speed is not None:
            state['elapsed_route_s'] = debug_route_clock(vehicle,state['elapsed_route_s'],elapsed_s,debug_speed)
        else:
            state["elapsed_route_s"] += elapsed_s * state.get("pace_factor", 1.0)
        if state["elapsed_route_s"] > route_duration:
            state["elapsed_route_s"] %= route_duration
        elapsed = state["elapsed_route_s"]
        index = max(0, min(len(path) - 2, bisect.bisect_right(timings, elapsed) - 1))
        while index < len(path) - 2 and timings[index + 1] <= timings[index]:
            index += 1
        start_time = timings[index]
        end_time = max(start_time + 1.0, timings[index + 1])
        fraction = min(1.0, max(0.0, (elapsed - start_time) / (end_time - start_time)))
        start, end = path[index], path[index + 1]
        state["segment_index"] = index
        state["segment_progress_m"] = haversine_m(*start, *end) * fraction
        state["lon"] = start[0] + (end[0] - start[0]) * fraction
        state["lat"] = start[1] + (end[1] - start[1]) * fraction
        planned_speed = haversine_m(*start, *end) / (end_time - start_time) * 3.6
        # Nav00 speed is decoded into the backend's 0..130 km/h telemetry
        # contract. A few plan rows contain unrealistically short gaps; keep
        # those synthetic packets valid while preserving the route geometry.
        target_speed = (
            debug_speed
            if debug_speed is not None
            else min(130.0, max(1.0, planned_speed * state.get("pace_factor", 1.0)))
        )
        # A debug value takes effect on the very next Nav00 packet.  In the
        # normal path retain a natural-looking gradual speed adjustment.
        if debug_speed is not None:
            state["speed_kmh"] = target_speed
        else:
            state["speed_kmh"] += max(-1.5, min(1.5, target_speed - state["speed_kmh"]))
        return state["lon"], state["lat"], round(state["speed_kmh"])

    debug_speed = debug_speed_overrides.get(vehicle["tr_id"])
    target_speed = debug_speed if debug_speed is not None else (
        24
        + (
            vehicle["tr_id"]
            + state[
                "segment_index"
            ]
            * 5
        )
        % 12
    )

    if debug_speed is not None:
        state["speed_kmh"] = target_speed
    else:
        state["speed_kmh"] += max(
            -1.5,
            min(
                1.5,
                target_speed
                - state[
                    "speed_kmh"
                ],
            ),
        )

    remaining_m = (
        state["speed_kmh"]
        / 3.6
        * elapsed_s
    )

    while remaining_m > 0:
        index = state[
            "segment_index"
        ]

        start = path[index]

        end = path[
            (
                index + 1
            )
            % len(path)
        ]

        segment_m = haversine_m(
            *start,
            *end,
        )

        if segment_m < 0.5:
            state[
                "segment_index"
            ] = (
                index + 1
            ) % len(path)

            state[
                "segment_progress_m"
            ] = 0.0

            continue

        available_m = (
            segment_m
            - state[
                "segment_progress_m"
            ]
        )

        step_m = min(
            remaining_m,
            available_m,
        )

        state[
            "segment_progress_m"
        ] += step_m

        remaining_m -= step_m

        if (
            state[
                "segment_progress_m"
            ]
            >= segment_m
            - 1e-6
        ):
            state[
                "segment_index"
            ] = (
                index + 1
            ) % len(path)

            state[
                "segment_progress_m"
            ] = 0.0

            (
                state["lon"],
                state["lat"],
            ) = end

        else:
            fraction = (
                state[
                    "segment_progress_m"
                ]
                / segment_m
            )

            state["lon"] = (
                start[0]
                + (
                    end[0]
                    - start[0]
                )
                * fraction
            )

            state["lat"] = (
                start[1]
                + (
                    end[1]
                    - start[1]
                )
                * fraction
            )

    return (
        state["lon"],
        state["lat"],
        round(
            state[
                "speed_kmh"
            ]
        ),
    )


async def publish_forever() -> None:
    dataset_dir = os.getenv(
        "DATA_DIR",
        "dataset",
    )

    host = os.getenv(
        "NDTP_HOST",
        "backend",
    )

    port = int(
        os.getenv(
            "NDTP_PORT",
            "9201",
        )
    )

    interval_s = max(
        1.0,
        float(
            os.getenv(
                "EMULATOR_INTERVAL_S",
                "3",
            )
        ),
    )

    max_vehicles = int(
        os.getenv(
            "EMULATOR_MAX_VEHICLES",
            "0",
        )
    )

    vehicles = load_vehicles(
        dataset_dir
    )

    if max_vehicles > 0:
        vehicles = vehicles[
            :max_vehicles
        ]

    if not vehicles:
        raise RuntimeError(
            "No mapped scheduled vehicles "
            "available for emulator"
        )

    runtime["vehicles"] = len(
        vehicles
    )
    custom_vehicle_ids.clear()
    custom_vehicle_ids.update(vehicle["tr_id"] for vehicle in vehicles)

    states = {
        vehicle["tr_id"]:
            initial_vehicle_state(
                vehicle
            )
        for vehicle
        in vehicles
    }

    while True:

        # Пока emulator выключен,
        # TCP соединение вообще
        # не держим.
        if runtime["paused"]:
            runtime[
                "connected"
            ] = False

            await asyncio.sleep(
                0.25
            )

            continue

        writer = None

        try:
            _, writer = await asyncio.open_connection(
                host,
                port,
            )

            runtime[
                "connected"
            ] = True

            runtime[
                "last_error"
            ] = None

            LOGGER.info(
                "connected host=%s "
                "port=%s vehicles=%s",
                host,
                port,
                len(vehicles),
            )

            while (
                not runtime[
                    "paused"
                ]
            ):
                sent_at = (
                    datetime.now(
                        timezone.utc
                    )
                )

                for vehicle in vehicles:
                    (
                        lon,
                        lat,
                        speed_kmh,
                    ) = advance_vehicle(
                        vehicle,
                        states[
                            vehicle[
                                "tr_id"
                            ]
                        ],
                        elapsed_s=(
                            interval_s
                        ),
                    )

                    writer.write(
                        build_nav00_frame(
                            unit_id=(
                                vehicle[
                                    "unit_id"
                                ]
                            ),
                            event_time=(
                                sent_at
                            ),
                            lon=lon,
                            lat=lat,
                            speed_kmh=(
                                speed_kmh
                            ),
                        )
                    )

                await writer.drain()

                runtime[
                    "batches_sent"
                ] += 1

                runtime[
                    "packets_sent"
                ] += len(
                    vehicles
                )

                runtime[
                    "last_sent_at"
                ] = (
                    sent_at.isoformat()
                )

                await asyncio.sleep(
                    interval_s
                )

        except (
            OSError,
            ConnectionError,
        ) as exc:
            runtime[
                "last_error"
            ] = str(exc)

            LOGGER.warning(
                "connection_failed "
                "host=%s port=%s "
                "error=%s",
                host,
                port,
                exc,
            )

            await asyncio.sleep(
                min(
                    interval_s,
                    5.0,
                )
            )

        finally:
            runtime[
                "connected"
            ] = False

            if writer is not None:
                writer.close()

                try:
                    await writer.wait_closed()
                except OSError:
                    pass


@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
    task = asyncio.create_task(
        publish_forever()
    )

    try:
        yield

    finally:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Custom NDTP Emulator",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
    }


@app.get("/status")
async def status():
    return dict(
        runtime
    )


@app.post("/pause")
async def pause():
    runtime["paused"] = True

    return {
        "status": "paused",
        **runtime,
    }


@app.post("/resume")
async def resume():
    runtime["paused"] = False

    return {
        "status": "running",
        **runtime,
    }


@app.post("/debug/vehicles/{tr_id}/speed")
async def set_debug_speed(tr_id: int, body: DebugSpeed):
    """Apply an in-memory speed overlay to one custom-emulator vehicle."""
    if tr_id not in custom_vehicle_ids:
        raise HTTPException(404, "Custom emulator vehicle not found")
    debug_speed_overrides[tr_id] = body.speed_kmh
    return {
        "tr_id": tr_id,
        "speed_kmh": body.speed_kmh,
        "debug": True,
        "persistent": False,
    }


@app.delete("/debug/vehicles/{tr_id}/speed")
async def clear_debug_speed(tr_id: int):
    if tr_id not in custom_vehicle_ids:
        raise HTTPException(404, "Custom emulator vehicle not found")
    debug_speed_overrides.pop(tr_id, None)
    return {
        "tr_id": tr_id,
        "debug": False,
        "persistent": False,
    }


def main() -> None:
    logging.basicConfig(
        level=os.getenv(
            "LOG_LEVEL",
            "INFO",
        ),
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(name)s "
            "%(message)s"
        ),
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "EMULATOR_HTTP_PORT",
                "18081",
            )
        ),
    )


if __name__ == "__main__":
    main()
