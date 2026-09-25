from pathlib import Path
import subprocess
import sys

from emulator.service import load_vehicles
from scripts import run_prototype


ROOT = Path(__file__).resolve().parents[1]


def test_emulator_is_enabled_in_default_compose_startup():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    emulator = compose.split("  emulator:\n", 1)[1]
    assert "profiles:" not in emulator
    assert "command: python -m emulator.service" in emulator


def test_prototype_runner_uses_the_built_in_multi_vehicle_emulator():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_prototype.py"), "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Built-in multi-vehicle NDTP emulator" in result.stdout
    assert "docker compose up -d --build" in result.stdout
    assert "github.com" not in result.stdout
    assert "18080" not in result.stdout


def test_prototype_runner_does_not_require_github_credentials_for_emulation():
    assert not hasattr(run_prototype, "github_token")
    assert not hasattr(run_prototype, "download_image_archive")
    assert not hasattr(run_prototype, "configure_emulator")


def test_emulator_uses_many_units_present_in_validate_dataset():
    vehicles = load_vehicles(ROOT / "dataset")
    assert len(vehicles) >= 10
