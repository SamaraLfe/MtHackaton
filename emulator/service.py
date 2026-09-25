"""Deterministic multi-vehicle NDTP telemetry generator for local integration QA."""
import asyncio
import csv
import logging
import math
import os
import re
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.ndtp import crc16

LOGGER = logging.getLogger("ndtp_emulator")
NAV00_FLAGS = 64 | 32 | 128  # east, north, valid location
POINT_PATTERN = re.compile(r"POINT\s*\(\s*([-+0-9.]+)\s+([-+0-9.]+)\s*\)")
EARTH_RADIUS_M = 6_371_000.0


def build_nav00_frame(*, unit_id: int, event_time: datetime, lon: float, lat: float, speed_kmh: int) -> bytes:
    """Encode one CRC-protected NDTP 6.2 Nav00 position frame."""
    timestamp_s = int(event_time.timestamp())
    cell = struct.pack(
        "<BBIIIBBHHHHHBB",
        0,
        0,
        timestamp_s,
        round(abs(lon) * 10_000_000),
        round(abs(lat) * 10_000_000),
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
    body = struct.pack("<HHHI", 1, 101, 0, 0) + cell
    checksum = crc16(body)
    stored_checksum = ((checksum & 0xFF) << 8) | (checksum >> 8)
    header = struct.pack("<HHHHBIH", 0x7E7E, len(body), 0, stored_checksum, 2, unit_id, 0)
    return header + body


def _point(row: dict[str, str]) -> tuple[float, float] | None:
    match = POINT_PATTERN.fullmatch((row.get("geom") or "").strip())
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def load_vehicles(dataset_dir: str | Path) -> list[dict]:
    """Join known NDTP units to all scheduled vehicles with usable planned paths."""
    dataset = Path(dataset_dir)
    units: dict[int, int] = {}
    with (dataset / "validate" / "traffic.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            units.setdefault(int(row["tr_id"]), int(row["unit_id"]))

    routes: dict[int, list[tuple[str, tuple[float, float]]]] = {}
    with (dataset / "validate" / "schedule_plan.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            point = _point(row)
            if point is not None:
                routes.setdefault(int(row["tr_id"]), []).append((row["time_begin"], point))

    return [
        {
            "tr_id": tr_id,
            "unit_id": units[tr_id],
            "path": [point for _, point in sorted(stops)],
        }
        for tr_id, stops in sorted(routes.items())
        if tr_id in units and len(stops) >= 2
    ]


def haversine_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    """Return WGS84 distance in metres without a geo-library dependency."""
    lat_a, lon_a, lat_b, lon_b = map(math.radians, (lat_a, lon_a, lat_b, lon_b))
    angle = math.sin((lat_b - lat_a) / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin((lon_b - lon_a) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(angle))


def initial_vehicle_state(vehicle: dict) -> dict:
    """Create persistent motion state; a vehicle no longer jumps stop-to-stop."""
    lon, lat = vehicle["path"][0]
    return {"segment_index": 0, "segment_progress_m": 0.0, "lon": lon, "lat": lat, "speed_kmh": float(22 + vehicle["tr_id"] % 13)}


def advance_vehicle(vehicle: dict, state: dict, *, elapsed_s: float) -> tuple[float, float, int]:
    """Advance one vehicle smoothly along its cyclic planned-stop polyline."""
    path = vehicle["path"]
    target_speed = 24 + (vehicle["tr_id"] + state["segment_index"] * 5) % 12
    state["speed_kmh"] += max(-1.5, min(1.5, target_speed - state["speed_kmh"]))
    remaining_m = state["speed_kmh"] / 3.6 * elapsed_s
    while remaining_m > 0:
        index = state["segment_index"]
        start = path[index]
        end = path[(index + 1) % len(path)]
        segment_m = haversine_m(*start, *end)
        if segment_m < 0.5:
            state["segment_index"] = (index + 1) % len(path)
            state["segment_progress_m"] = 0.0
            continue
        available_m = segment_m - state["segment_progress_m"]
        step_m = min(remaining_m, available_m)
        state["segment_progress_m"] += step_m
        remaining_m -= step_m
        if state["segment_progress_m"] >= segment_m - 1e-6:
            state["segment_index"] = (index + 1) % len(path)
            state["segment_progress_m"] = 0.0
            state["lon"], state["lat"] = end
        else:
            fraction = state["segment_progress_m"] / segment_m
            state["lon"] = start[0] + (end[0] - start[0]) * fraction
            state["lat"] = start[1] + (end[1] - start[1]) * fraction
    return state["lon"], state["lat"], round(state["speed_kmh"])


async def publish_forever() -> None:
    dataset_dir = os.getenv("DATA_DIR", "dataset")
    host = os.getenv("NDTP_HOST", "backend")
    port = int(os.getenv("NDTP_PORT", "9201"))
    interval_s = max(1.0, float(os.getenv("EMULATOR_INTERVAL_S", "3")))
    max_vehicles = int(os.getenv("EMULATOR_MAX_VEHICLES", "0"))
    vehicles = load_vehicles(dataset_dir)
    if max_vehicles > 0:
        vehicles = vehicles[:max_vehicles]
    if not vehicles:
        raise RuntimeError("No mapped scheduled vehicles available for emulator")

    states = {vehicle["tr_id"]: initial_vehicle_state(vehicle) for vehicle in vehicles}
    tick = 0
    while True:
        writer = None
        try:
            _, writer = await asyncio.open_connection(host, port)
            LOGGER.info("connected host=%s port=%s vehicles=%s", host, port, len(vehicles))
            while True:
                sent_at = datetime.now(timezone.utc)
                for vehicle in vehicles:
                    lon, lat, speed_kmh = advance_vehicle(vehicle, states[vehicle["tr_id"]], elapsed_s=interval_s)
                    writer.write(build_nav00_frame(
                        unit_id=vehicle["unit_id"], event_time=sent_at,
                        lon=lon, lat=lat, speed_kmh=speed_kmh,
                    ))
                await writer.drain()
                LOGGER.info("batch_sent tick=%s vehicles=%s", tick, len(vehicles))
                tick += 1
                await asyncio.sleep(interval_s)
        except OSError as exc:
            LOGGER.warning("connection_failed host=%s port=%s error=%s", host, port, exc)
            await asyncio.sleep(min(interval_s, 5.0))
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(publish_forever())


if __name__ == "__main__":
    main()
