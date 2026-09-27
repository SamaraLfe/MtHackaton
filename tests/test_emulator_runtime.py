from pathlib import Path
import asyncio
import subprocess
import sys

import httpx
import pytest

from backend import app as backend
from emulator.service import load_vehicles
from emulator import service as custom_emulator
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


def test_prototype_startup_explicitly_resumes_the_original_emulator(monkeypatch):
    requested_states = []

    async def configure(enabled=None):
        requested_states.append(enabled)
        return {"status": "running", "units": 13}

    monkeypatch.setattr(backend, "official_emulator_config", configure)

    result = asyncio.run(backend.warm_official_source())

    assert result == {"status": "running", "units": 13}
    assert requested_states == [True]


@pytest.mark.parametrize(('payload','expected'),[
    ({'paused':False,'connected':False},'running'),
    ({'paused':False,'connected':True},'running'),
    ({'paused':True,'connected':True},'paused'),
    ({'paused':True,'connected':False},'paused'),
    ({'status':'running','paused':False,'connected':False},'running'),
    ({'status':'unavailable','detail':'connection refused'},'unavailable'),
    ({},'unknown'),
])
def test_custom_generation_status_is_independent_of_ndtp_connection(monkeypatch,payload,expected):
    async def custom_call(path):
        assert path=='/status'
        return payload
    async def official_config():return {'status':'running'}
    monkeypatch.setattr(backend,'custom_emulator_call',custom_call)
    monkeypatch.setattr(backend,'official_emulator_config',official_config)
    result=asyncio.run(backend.emulator_status())
    source=next(source for source in result['sources'] if source['id']=='custom-emulator')
    assert source['status']==expected
    assert source['details']==payload


def test_pause_stop_and_resume_apis_never_become_unknown_during_reconnect(monkeypatch):
    monkeypatch.setattr(custom_emulator,'runtime',{**custom_emulator.runtime,'paused':False,'connected':False})
    monkeypatch.setattr(backend,'get_dispatcher',lambda _: {'id':'dispatcher-01'})
    async def official_config():return {'status':'running'}
    monkeypatch.setattr(backend,'official_emulator_config',official_config)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=custom_emulator.app),base_url='http://custom') as custom_client:
            monkeypatch.setattr(backend,'client',custom_client)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend.app),base_url='http://backend') as api:
                for action,expected in [('pause','paused'),('resume','running'),('stop','paused'),('resume','running'),('resume','running')]:
                    response=await api.post(f'/api/admin/emulators/custom-emulator/{action}',json={'dispatcher_id':'dispatcher-01'})
                    assert response.status_code==200
                    assert response.json()['custom']['status']==expected
                    response=await api.get('/api/admin/emulators')
                    assert response.status_code==200
                    source=response.json()['sources'][0]
                    assert source['status']==expected
                    assert source['details']['status']==expected
                    assert source['details']['connected'] is False
    asyncio.run(scenario())
