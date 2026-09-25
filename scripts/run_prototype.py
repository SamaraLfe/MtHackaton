"""Bootstrap and run the complete local prototype, including the NDTP emulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ndtp-telemetry-emulator:1.0"
ASSET_URL = (
    "https://github.com/SamaraLfe/MtHackaton/releases/download/"
    "dataset-v1/ndtp-telemetry-emulator.tar"
)
ASSET_API_URL = (
    "https://api.github.com/repos/SamaraLfe/MtHackaton/releases/assets/588033020"
)
ASSET_SHA256 = "89399e531f20a508554441f1491be5676a524fa14c05f6e10e48fd22d849a199"
CACHE_PATH = ROOT / ".cache" / "ndtp-telemetry-emulator.tar"
COMPOSE_COMMAND = ["docker", "compose", "up", "-d", "--build"]
EMULATOR_CONFIG = {
    "targetHost": "backend",
    "targetPort": 9201,
    "units": [
        {
            "unitId": 985940,
            "intervalMs": 5000,
            "autoGenerate": True,
            "cells": [],
        }
    ],
}


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=check, text=True)


def image_available() -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def github_token() -> str:
    if token := os.getenv("GITHUB_TOKEN"):
        return token
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        credentials = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if token := credentials.get("password"):
            return token
    raise RuntimeError(
        "GitHub authentication is required to download the emulator from the "
        "private repository. Configure Git credentials or GITHUB_TOKEN."
    )


def asset_request(token: str) -> urllib.request.Request:
    return urllib.request.Request(
        ASSET_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "User-Agent": "MtHackaton-prototype-runner",
        },
    )


def download_image_archive() -> Path:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and sha256(CACHE_PATH) == ASSET_SHA256:
        print(f"Using cached emulator archive: {CACHE_PATH}")
        return CACHE_PATH

    temporary = CACHE_PATH.with_suffix(".tar.part")
    temporary.unlink(missing_ok=True)
    print(f"Downloading emulator image from {ASSET_URL}")
    request = asset_request(github_token())
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)

    actual = sha256(temporary)
    if actual != ASSET_SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Emulator archive checksum mismatch: {actual}")
    temporary.replace(CACHE_PATH)
    return CACHE_PATH


def ensure_emulator_image() -> None:
    if image_available():
        print(f"Docker image is already available: {IMAGE}")
        return
    archive = download_image_archive()
    run(["docker", "load", "-i", str(archive)])
    if not image_available():
        raise RuntimeError(f"Docker image was not loaded: {IMAGE}")


def configure_emulator(timeout_s: float = 60.0) -> None:
    payload = json.dumps(EMULATOR_CONFIG).encode("utf-8")
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            "http://127.0.0.1:18080/api/config",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if 200 <= response.status < 300:
                    print("NDTP emulator configured for backend:9201")
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(2)
    raise RuntimeError(f"NDTP emulator API did not become ready: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-configure", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Ensure Docker image: {IMAGE}")
        print(f"Download if missing: {ASSET_URL}")
        print("Run:", " ".join(COMPOSE_COMMAND))
        print("Configure: POST http://127.0.0.1:18080/api/config")
        return

    ensure_emulator_image()
    run(COMPOSE_COMMAND)
    if not args.no_configure:
        configure_emulator()
    print("Dashboard: http://127.0.0.1:8080")
    print("Backend API: http://127.0.0.1:8000/docs")
    print("Emulator API: http://127.0.0.1:18080")


if __name__ == "__main__":
    main()
