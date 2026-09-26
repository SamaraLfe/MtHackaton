"""Build and start the complete transport-prediction prototype."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
import urllib.error
import urllib.request
import http.client
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EMULATOR_IMAGE = "ndtp-telemetry-emulator:1.0"

EMULATOR_TAR = (
    ROOT
    / ".cache"
    / "ndtp-telemetry-emulator.tar"
)

COMPOSE_COMMAND = [
    "docker",
    "compose",
    "up",
    "-d",
    "--build",
]


def run(
    command: list[str],
) -> subprocess.CompletedProcess[str]:
    print(
        "+",
        " ".join(command),
        flush=True,
    )

    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
    )


def emulator_image_exists() -> bool:
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            EMULATOR_IMAGE,
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    return result.returncode == 0


def ensure_emulator_image() -> None:
    if emulator_image_exists():
        print(
            f"Emulator image found: "
            f"{EMULATOR_IMAGE}"
        )
        return

    if not EMULATOR_TAR.exists():
        raise RuntimeError(
            "Original NDTP emulator image is missing.\n"
            f"Expected archive: {EMULATOR_TAR}"
        )

    print(
        "Loading original NDTP emulator..."
    )

    run(
        [
            "docker",
            "load",
            "-i",
            str(EMULATOR_TAR),
        ]
    )


def wait_for_url(
    url: str,
    timeout_s: float = 120,
) -> None:
    deadline = (
        time.monotonic()
        + timeout_s
    )

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                url,
                timeout=2,
            ) as response:
                if (
                    200
                    <= response.status
                    < 300
                ):
                    return

        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
        ):
            pass

        time.sleep(1)

    raise RuntimeError(
        f"Service did not become ready: {url}"
    )


def load_emulator_units() -> list[dict]:
    schedule_path = (
        ROOT
        / "dataset"
        / "validate"
        / "schedule_plan.csv"
    )

    traffic_path = (
        ROOT
        / "dataset"
        / "validate"
        / "traffic.csv"
    )

    schedule_ids = set()

    with schedule_path.open(
        encoding="utf-8",
        newline="",
    ) as stream:
        for row in csv.DictReader(stream):
            schedule_ids.add(
                int(row["tr_id"])
            )

    units_by_trip: dict[int, int] = {}

    with traffic_path.open(
        encoding="utf-8",
        newline="",
    ) as stream:
        for row in csv.DictReader(stream):
            tr_id = int(
                row["tr_id"]
            )

            if (
                tr_id not in schedule_ids
                or tr_id in units_by_trip
            ):
                continue

            units_by_trip[tr_id] = int(
                row["unit_id"]
            )

    return [
        {
            "unitId": unit_id,
            "intervalMs": 5000,
            "autoGenerate": True,
            "cells": [],
        }
        for tr_id, unit_id
        in sorted(
            units_by_trip.items()
        )
    ]


def configure_emulator() -> None:
    units = load_emulator_units()

    if not units:
        raise RuntimeError(
            "No NDTP units found in validate dataset"
        )

    config = {
        "targetHost": "backend",
        "targetPort": 9201,
        "units": units,
    }

    payload = json.dumps(
        config,
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        "http://127.0.0.1:18080/api/config",
        data=payload,
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=10,
    ) as response:
        body = response
        body.read()

    print(
        f"Original NDTP emulator configured: "
        f"{len(units)} vehicles"
    )


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    if args.dry_run:
        print(
            f"Emulator image: "
            f"{EMULATOR_IMAGE}"
        )

        print(
            f"Emulator archive: "
            f"{EMULATOR_TAR}"
        )

        print(
            "Run:",
            " ".join(
                COMPOSE_COMMAND
            ),
        )

        print(
            "Original emulator will be "
            "configured automatically."
        )

        return

    ensure_emulator_image()

    run(
        COMPOSE_COMMAND
    )

    print(
        "Waiting for backend..."
    )

    wait_for_url(
        "http://127.0.0.1:8000/health/ready"
    )

    print(
        "Waiting for original NDTP emulator..."
    )

    wait_for_url(
        "http://127.0.0.1:18080/api/cells"
    )

    configure_emulator()

    print()
    print(
        "Prototype is ready."
    )
    print(
        "Dashboard: "
        "http://127.0.0.1:8080"
    )
    print(
        "API guide: "
        "http://127.0.0.1:8080/docs"
    )
    print(
        "Swagger: "
        "http://127.0.0.1:8000/docs/swagger"
    )
    print(
        "NDTP emulator: "
        "http://127.0.0.1:18080"
    )


if __name__ == "__main__":
    main()