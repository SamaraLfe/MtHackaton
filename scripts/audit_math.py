"""Read-only reproduction of frozen predictions, calibration and online cost.

Run from the repository root: python scripts/audit_math.py
No live APIs, telemetry, SQLite state or model artifacts are modified.
"""
import asyncio
from collections import deque
import json
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import app as backend
from ml.features import epoch, load_schedule, load_traffic
from ml.model import Predictor
from ml.service import app as ml_app
from ml.train import conformal_radius, load_split


def reproduce(predictor):
    points,features=load_split(Path('dataset'),'test')
    predicted=predictor.predict(features)
    frozen=pd.read_csv('artifacts/test_predictions.csv').set_index('sample_id')
    reference=frozen.loc[points.sample_id.astype(str),'prediction'].to_numpy()
    max_difference=float(np.max(np.abs(predicted-reference)))
    assert max_difference<1e-10, max_difference
    truth=features.target_delay_s.to_numpy(dtype=float)
    residuals=truth-predicted
    assert np.allclose(residuals,predictor.meta['calibration_residuals'],atol=1e-10,rtol=0)
    radius=conformal_radius(residuals)
    assert abs(radius-predictor.meta['interval_radius_s'])<1e-10
    threshold=float(predictor.meta['late_threshold_s'])
    probabilities=np.array([(1+np.sum(residuals>threshold-p))/(len(residuals)+2) for p in predicted])
    assert np.allclose(probabilities,frozen.loc[points.sample_id.astype(str),'late_probability'],atol=1e-12,rtol=0)
    test=dict(rows=len(points),max_prediction_difference_s=max_difference,
              mae_s=float(np.mean(np.abs(residuals))),radius_s=radius,
              interval_coverage=float(np.mean(np.abs(residuals)<=radius)),
              brier_score=float(np.mean((probabilities-(truth>threshold))**2)))
    points,features=load_split(Path('dataset'),'validate')
    predicted=predictor.predict(features)
    frozen=pd.read_csv('artifacts/submission.csv',sep=';').set_index('sample_id')
    difference=float(np.max(np.abs(predicted-frozen.loc[points.sample_id.astype(str),'prediction'].to_numpy())))
    assert difference<1e-10,difference
    return dict(test=test,validate=dict(rows=len(points),max_prediction_difference_s=difference))


async def benchmark():
    backend.schedule=load_schedule('dataset/validate/schedule_plan.csv')
    traffic=load_traffic('dataset/validate/traffic.csv')
    points=pd.read_csv('dataset/validate/points.csv').groupby('tr_id',sort=False).tail(1)
    inputs=[]
    for raw in points.to_dict('records'):
        point={key:raw[key] for key in ('tr_id','T','target_stop_id','target_time_begin','cur_dev_s')}
        t=epoch(point['T']);tr=int(point['tr_id'])
        records=traffic[(traffic.tr_id==tr)&(traffic.ts<=t)].tail(1000).to_dict('records')
        inputs.append((point,records))
    # A controlled 26-vehicle load even when archive prediction points cover
    # fewer vehicles than the two current emulator fleets.
    inputs=(inputs*((26+len(inputs)-1)//len(inputs)))[:26]
    timings=[];fleet_times=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ml_app),base_url='http://ml') as client:
        backend.client=client
        await backend.forecast(*inputs[0],'historical_replay')
        for _ in range(3):
            begin=time.perf_counter()
            for point,records in inputs:
                start=time.perf_counter()
                async with backend.lock:
                    result=await backend.forecast(point,records,'historical_replay')
                assert not result['degraded']
                timings.append((time.perf_counter()-start)*1000)
            fleet_times.append(time.perf_counter()-begin)
    forecast_only=dict(transport='in-process HTTP/ASGI; excludes Docker network and NDTP decoding/map matching',
                vehicles_per_pass=len(inputs),passes=len(fleet_times),history_limit=1000,
                single_forecast_ms=dict(mean=float(np.mean(timings)),p95=float(np.percentile(timings,95)),max=max(timings)),
                sequential_fleet_s=dict(mean=float(np.mean(fleet_times)),max=max(fleet_times)),
                source_interval_s=3.)
    routes=[];live_inputs=[]
    common_time=epoch('2026-01-06T10:00:00Z')
    for index,(raw,records) in enumerate(inputs,start=100):
        # Archive points occur at different times/days. A live fleet batch
        # must share one wall clock or late-day rejection skews the benchmark.
        shift=common_time-epoch(raw['T'])
        point={**raw,'tr_id':index,'T':backend.timestamp(common_time).isoformat(),
               'target_time_begin':backend.timestamp(epoch(raw['target_time_begin'])+shift).isoformat()}
        route=backend.schedule[backend.schedule.tr_id==int(raw['tr_id'])].assign(tr_id=index).copy()
        route['ts']+=shift
        route['time_begin']=route['ts'].map(lambda ts:backend.timestamp(ts).isoformat())
        routes.append(route)
        backend.history[index]=deque(({**record,'tr_id':index,'ts':record['ts']+shift,
                                       'event_time':backend.timestamp(record['ts']+shift).isoformat()}
                                      for record in records),maxlen=1000)
        backend.position_offsets[index]=0.
        live_inputs.append(point)
    backend.schedule=pd.concat(routes,ignore_index=True)
    backend.state.update(mode='live',ingest_paused=False)
    backend.live_schedule_day=backend.timestamp(live_inputs[0]['T']).date()
    timings=[];fleet_times=[];counts=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ml_app),base_url='http://ml') as client:
        backend.client=client
        for step in (3,6,9):
            begin=time.perf_counter();before=backend.counters['predictions']
            for point in live_inputs:
                tr=int(point['tr_id']);t=epoch(point['T'])+step
                position=backend.planned_position_at(tr,t)
                event=dict(tr_id=tr,event_time=backend.timestamp(t).isoformat(),
                           lon=position['lon'],lat=position['lat'],speed=position['speed'],
                           location_valid=True,telemetry_source='custom_ndtp_nav00')
                start=time.perf_counter()
                async with backend.lock:
                    await backend.ingest(event)
                timings.append((time.perf_counter()-start)*1000)
            counts.append(backend.counters['predictions']-before)
            fleet_times.append(time.perf_counter()-begin)
    assert counts==[26,26,26],counts
    return dict(forecast_only=forecast_only,live_pipeline=dict(
        transport='in-process HTTP/ASGI; includes ingest, map matching, features and ML; excludes NDTP decoding/Docker network',
        vehicles_per_pass=len(live_inputs),predictions_per_pass=counts,
        single_packet_ms=dict(mean=float(np.mean(timings)),p95=float(np.percentile(timings,95)),max=max(timings)),
        sequential_fleet_s=dict(mean=float(np.mean(fleet_times)),max=max(fleet_times))))


if __name__=='__main__':
    report=reproduce(Predictor('artifacts'))
    report['benchmark']=asyncio.run(benchmark())
    print(json.dumps(report,ensure_ascii=False,indent=2))
