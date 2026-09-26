"""Build and start the complete transport-prediction prototype."""

from __future__ import annotations

import argparse
import subprocess
import time
import urllib.error
import urllib.request
import http.client
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

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


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    args = parser.parse_args()

    if args.dry_run:
        print("Built-in multi-vehicle NDTP emulator is included in compose.")
        print(
            "Run:",
            " ".join(
                COMPOSE_COMMAND
            ),
        )

        print("custom-emulator starts from the validate schedule and can be paused in /admin.")

        return

    run(
        COMPOSE_COMMAND
    )

    print(
        "Waiting for backend..."
    )

    wait_for_url(
        "http://127.0.0.1:8000/health/ready"
    )

    print("Waiting for built-in custom-emulator...")
    wait_for_url("http://127.0.0.1:18081/health")

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
    print("Code docs: http://127.0.0.1:8080/code/")
    print(
        "Swagger: "
        "http://127.0.0.1:8000/docs/swagger"
    )
    print("custom-emulator: http://127.0.0.1:18081")


if __name__ == "__main__":
    main()
