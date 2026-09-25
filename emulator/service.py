"""Deterministic multi-vehicle NDTP telemetry generator for local integration QA."""
import asyncio
import csv
import logging
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

    routes: dict[int, list[tuple[float, float]]] = {}
    with (dataset / "validate" / "schedule_plan.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            point = _point(row)
            if point is not None:
                routes.setdefault(int(row["tr_id"]), []).append(point)

    return [
        {"tr_id": tr_id, "unit_id": units[tr_id], "path": path}
        for tr_id, path in sorted(routes.items())
        if tr_id in units and len(path) >= 2
    ]


def vehicle_position(vehicle: dict, tick: int) -> tuple[float, float, int]:
    """Select a reproducible moving position and plausible speed for one vehicle."""
    path = vehicle["path"]
    index = (tick + vehicle["tr_id"] % len(path)) % len(path)
    lon, lat = path[index]
    speed_kmh = 18 + (vehicle["tr_id"] + tick * 3) % 27
    return lon, lat, speed_kmh


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

    tick = 0
    while True:
        writer = None
        try:
            _, writer = await asyncio.open_connection(host, port)
            LOGGER.info("connected host=%s port=%s vehicles=%s", host, port, len(vehicles))
            while True:
                sent_at = datetime.now(timezone.utc)
                for vehicle in vehicles:
                    lon, lat, speed_kmh = vehicle_position(vehicle, tick)
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
