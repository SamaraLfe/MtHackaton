"""Build and start the complete local transport-prediction prototype."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_COMMAND = ["docker", "compose", "up", "-d", "--build"]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        print("Built-in multi-vehicle NDTP emulator: all mapped scheduled vehicles")
        print("Run:", " ".join(COMPOSE_COMMAND))
        print("The emulator connects to backend:9201 inside Docker Compose.")
        return

    run(COMPOSE_COMMAND)
    print("Dashboard: http://127.0.0.1:8080")
    print("API guide: http://127.0.0.1:8080/docs | Swagger: http://127.0.0.1:8000/docs/swagger")
    print("NDTP emulator: built-in multi-vehicle stream to backend:9201")


if __name__ == "__main__":
    main()
