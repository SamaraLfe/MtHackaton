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
