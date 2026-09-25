import asyncio
import os
from pathlib import Path
import httpx
import pytest
import time
import pandas as pd
from fastapi.testclient import TestClient
from ml.service import app as ml_app
from backend import app as backend


def test_risk_and_incident_helpers():
    items=[
        {'tr_id':10,'level':'high','late_probability':.8,'prediction_s':180,'reason':'slow'},
        {'tr_id':11,'level':'low','late_probability':.1},
        {'tr_id':12,'level':'unknown','late_probability':None},
    ]
    routes=backend.route_risk(items)
    assert routes[0]['tr_id']==10 and routes[0]['level']=='high'
    assert [x['vehicle_id'] for x in backend.incidents(items)]==[10]


def test_metrics_expose_readable_v5_summary():
    with TestClient(backend.app) as client:
        response=client.get('/api/metrics')
        assert response.status_code==200
        data=response.json()
        assert data['v5']['model']=='v5'
        assert data['v5']['features']==104
        assert data['v5']['test_points']==353
        assert data['v5']['mae_s']<data['v5']['baseline_mae_s']
        assert data['v5']['improvement_pct']>0
        assert data['readable']['coverage'].endswith('%')
        assert len(data['feature_importance'])==6


def test_what_if_reduces_projected_risk():
    with TestClient(backend.app) as client:
        backend.vehicles.clear()
        backend.vehicles[10]={'tr_id':10,'level':'high','late_probability':.8,'prediction_s':180}
        response=client.post('/api/what-if',json={'extra_vehicles':1})
        assert response.status_code==200
        data=response.json()
        assert data['projected'][0]['projected_late_probability']==.68
        backend.vehicles.clear()

def test_map_match_and_admin_role_gate():
    with TestClient(backend.app) as client:
        saved=backend.schedule
        backend.schedule=pd.DataFrame([
            dict(tt_action_item_id=7,tr_id=1,ts=100,time_begin='2026-01-06 10:00:00',lon=37.6,lat=55.7,building_address='Stop A'),
            dict(tt_action_item_id=8,tr_id=1,ts=200,time_begin='2026-01-06 10:10:00',lon=37.61,lat=55.71,building_address='Stop B'),
        ])
        matched=client.post('/api/map-match',json={'tr_id':1,'lon':37.6001,'lat':55.7001}).json()
        assert matched['stop_id']==7 and matched['distance_m']<20
        assert client.post('/api/admin/simulation',json={'role':'dispatcher','tr_id':1}).status_code==403
        backend.schedule=saved


def test_dispatcher_can_queue_and_read_driver_instruction():
    with TestClient(backend.app) as client:
        backend.driver_commands.clear()
        response=client.post('/api/driver-commands',json={
            'role':'dispatcher','dispatcher_id':'dispatcher-01','tr_id':131672,'action':'accelerate_safely',
            'message':'При возможности сократить отставание без нарушения безопасности.'
        })
        assert response.status_code==201,response.text
        command=response.json()
        assert command['status']=='queued_for_integration'
        assert command['channel']=='local_dispatch_outbox'
        assert command['dispatcher']['id']=='dispatcher-01'
        commands=client.get('/api/driver-commands',params={'tr_id':131672})
        assert commands.status_code==200
        assert commands.json()['items'][0]['id']==command['id']


def test_control_room_loads_leaflet_stylesheet():
    with TestClient(backend.app) as client:
        response=client.get('/')
        assert response.status_code==200
        assert '/static/vendor/leaflet/leaflet.css' in response.text


def test_map_module_tolerates_dispatcher_page_without_legacy_controls():
    source=(Path('dashboard')/'map.js').read_text(encoding='utf-8')
    assert "getElementById('map-all-routes')?.checked" in source
    assert "getElementById('map-empty')?.classList" in source


def test_replay_mode_populates_a_multi_vehicle_historical_snapshot():
    with TestClient(backend.app) as client:
        response=client.post('/api/mode',json={'mode':'replay'})
        assert response.status_code==200
        snapshot=client.get('/api/state').json()
        assert len(snapshot['vehicles'])==11
        assert {v['source'] for v in snapshot['vehicles']}=={'historical_archive'}
        assert all(v['prediction_s'] is not None for v in snapshot['vehicles'])


def test_dispatcher_profiles_are_available_for_local_workspaces():
    with TestClient(backend.app) as client:
        response=client.get('/api/dispatchers')
        assert response.status_code==200
        profiles=response.json()['profiles']
        assert len(profiles)>=2
        assert {'id','name','role'}<=set(profiles[0])


def test_ml_rejects_nonfinite_and_missing_features():
    with TestClient(ml_app) as client:
        assert client.get('/health').status_code==200
        assert client.post('/predict',json={'rows':[{}]}).status_code==422

@pytest.mark.skipif(not Path(os.getenv('DATA_DIR','dataset')).exists(),reason='Dataset required')
def test_replay_live_and_degradation():
    with TestClient(backend.app) as client:
        original=backend.client
        backend.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=ml_app),base_url='http://ml')
        try:
            assert client.get('/openapi.json').status_code==200
            assert client.get('/').status_code==200
            r=client.post('/api/replay/step')
            assert r.status_code==200,r.text
            result=r.json()['prediction']
            assert 600<result['horizon_s']<=900
            assert not result['degraded']
            # All supplied points must work, including different stop IDs tied
            # at the earliest scheduled timestamp.
            for _ in range(len(backend.points)-1):
                r=client.post('/api/replay/step')
                assert r.status_code==200,r.text
            assert client.post('/api/replay/step').json()['done']
            assert client.post('/api/mode',json={'mode':'live'}).status_code==200
            assert client.post('/api/replay/step').status_code==409
            assert client.post('/api/telemetry',json=[dict(tr_id=1,event_time='bad',lon=37,lat=55,speed=10)]).status_code==422
            assert client.post('/api/telemetry',json=[dict(tr_id=1,event_time='2099-01-01',lon=37,lat=55,speed=10)]).status_code==422
            # Real-time branch uses observed stop proximity, then the independent ML API.
            saved_schedule=backend.schedule
            now=int(time.time())-2
            backend.schedule=pd.DataFrame([
                dict(tt_action_item_id=1,tr_id=1,ts=now-45,time_begin=pd.Timestamp(now-45,unit='s',tz='UTC').isoformat(),lon=37.6,lat=55.7,building_address='Past stop'),
                dict(tt_action_item_id=2,tr_id=1,ts=now+720,time_begin=pd.Timestamp(now+720,unit='s',tz='UTC').isoformat(),lon=37.61,lat=55.71,building_address='Future stop')])
            event=dict(tr_id=1,event_time=pd.Timestamp(now,unit='s',tz='UTC').isoformat(),lon=37.6,lat=55.7,speed=0)
            assert client.post('/api/telemetry',json=[event]).status_code==200
            live=client.get('/api/state').json()['vehicles'][0]
            assert live['source']=='live' and live['features']['cur_dev_s']==45
            assert live['horizon_s']==720 and not live['degraded']
            backend.schedule=saved_schedule
            client.post('/api/mode',json={'mode':'replay'})
            async def failed(request):raise httpx.ConnectError('offline')
            backend.client=httpx.AsyncClient(transport=httpx.MockTransport(failed))
            result=client.post('/api/replay/step').json()['prediction']
            assert result['degraded'] and result['late_probability'] is None and result['level']=='unknown'
        finally:backend.client=original
