"""Simulate five minutes offline with real V5, without mutating live services."""
import asyncio
from collections import Counter
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import app as backend
from emulator.service import advance_vehicle, initial_vehicle_state, load_vehicles
from ml.features import epoch, load_schedule, timestamp
from ml.service import app as ml_app


async def main():
    backend.schedule=load_schedule('dataset/validate/schedule_plan.csv')
    backend.schedule['tr_id']+=backend.CUSTOM_TR_ID_OFFSET
    backend.state.update(mode='live',ingest_paused=False)
    start=epoch('2026-01-06T10:00:00Z')
    backend.live_schedule_day=timestamp(start).date()
    fleet=load_vehicles('dataset')
    states={v['tr_id']:initial_vehicle_state(v,chaos_enabled=True,seed=42) for v in fleet}
    snapshots=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ml_app),base_url='http://ml') as client:
        backend.client=client
        for elapsed in range(3,301,3):
            for vehicle in fleet:
                tr=vehicle['tr_id'];lon,lat,speed=advance_vehicle(vehicle,states[tr],elapsed_s=3)
                await backend.ingest(dict(tr_id=tr,event_time=timestamp(start+elapsed).isoformat(),
                    lon=lon,lat=lat,speed=speed,location_valid=True,telemetry_source='custom_ndtp_nav00',simulated=True))
            if elapsed%60==0:
                deviations=[v['current_deviation_s'] for v in backend.vehicles.values() if v.get('current_deviation_s') is not None]
                snapshots.append(dict(elapsed_s=elapsed,levels=dict(Counter(v.get('level') for v in backend.vehicles.values())),
                    phases=dict(Counter(s['chaos_phase'] for s in states.values())),
                    deviation_s=dict(min=min(deviations,default=None),max=max(deviations,default=None))))
    assert any(s['levels'].get('medium',0)+s['levels'].get('high',0)>0 for s in snapshots),snapshots
    assert backend.counters['ml_failures']==0
    print(json.dumps(dict(vehicles=len(fleet),simulated_seconds=300,snapshots=snapshots,
                         ml_failures=backend.counters['ml_failures']),ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
