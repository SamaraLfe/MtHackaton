from pathlib import Path
import csv
import subprocess
import sys

from scripts import run_prototype


ROOT = Path(__file__).resolve().parents[1]


def test_emulator_is_enabled_in_default_compose_startup():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    emulator = compose.split("  emulator:\n", 1)[1]
    assert "profiles:" not in emulator


def test_emulator_archive_cache_is_excluded_from_docker_build_context():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".cache" in dockerignore or ".cache/" in dockerignore


def test_prototype_runner_bootstraps_emulator_image_before_compose():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_prototype.py"), "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ndtp-telemetry-emulator:1.0" in result.stdout
    assert "dataset-v1/ndtp-telemetry-emulator.tar" in result.stdout
    assert "docker compose up -d --build" in result.stdout


def test_private_release_asset_request_uses_github_authentication():
    request = run_prototype.asset_request("secret-token")
    assert request.full_url.endswith("/releases/assets/588033020")
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert request.get_header("Accept") == "application/octet-stream"


def test_emulator_uses_unit_present_in_validate_dataset():
    configured_unit = run_prototype.EMULATOR_CONFIG["units"][0]["unitId"]
    with (ROOT / "dataset" / "validate" / "traffic.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        unit_ids = {int(row["unit_id"]) for row in csv.DictReader(source)}
    assert configured_unit in unit_ids
