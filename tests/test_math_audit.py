"""Numerical regressions for telemetry cadence, time, geometry and decisions."""
import asyncio
from collections import defaultdict, deque

import httpx
import numpy as np
import pandas as pd
import pytest

from backend import app as backend
from emulator import service as emulator
from ml.feature_builder import build_v5_row, haversine_m
from ml.features import build_one, epoch, haversine, timestamp
from ml.model import Predictor


def iso(seconds):
    return pd.Timestamp(seconds, unit='s', tz='UTC').isoformat()


@pytest.fixture
def fleet(monkeypatch):
    start = epoch('2026-01-06T10:00:00Z')
    route = pd.DataFrame([
        dict(tr_id=1, tt_action_item_id=i+1, ts=start+dt,
             time_begin=iso(start+dt), lon=37.+i*.01, lat=55., building_address=str(i))
        for i, dt in enumerate((0, 720, 1440, 2160))
    ])
    monkeypatch.setattr(backend, 'schedule', route)
    monkeypatch.setattr(backend, 'live_schedule_day', timestamp(iso(start)).date())
    monkeypatch.setattr(backend, 'history', defaultdict(lambda: deque(maxlen=1000)))
    for name in ('vehicles','deviations','position_offsets','position_states','last_forecast','counters'):
        monkeypatch.setattr(backend, name, defaultdict(int) if name=='counters' else {})
    monkeypatch.setattr(backend, 'state', {'mode':'live'})
    monkeypatch.setattr(backend, 'last_telemetry_received_at', 0.)
    monkeypatch.setattr(backend, 'ml_retry_after', 0.)
    monkeypatch.setattr(backend, 'LIVE_FORECAST_INTERVAL_S', 1.)
    return start


@pytest.mark.parametrize('value',[1767665400,1767665400.,np.int64(1767665400),np.float64(1767665400)])
def test_numeric_time_is_unix_seconds_not_nanoseconds(value):
    assert epoch(value)==1767665400
    assert timestamp(value).isoformat()=='2026-01-06T05:10:00+03:00'


@pytest.mark.parametrize('event',['2026-09-25T00:30:00+03:00','2026-09-25T23:30:00+03:00'])
def test_night_plan_has_one_consistent_epoch(event):
    original=pd.DataFrame([
        dict(ts=epoch('2026-01-05T22:30:00Z'),time_begin='2026-01-05 22:30:00'),
        dict(ts=epoch('2026-01-06T01:30:00Z'),time_begin='2026-01-06 01:30:00'),
    ])
    aligned=backend.align_schedule_to_event_day(original,event)
    assert timestamp(aligned.iloc[0].time_begin).date()==timestamp(event).date()
    assert all(epoch(row.time_begin)==row.ts for row in aligned.itertuples())
    assert aligned.iloc[1].ts-aligned.iloc[0].ts==10800
    pd.testing.assert_frame_equal(aligned,backend.align_schedule_to_event_day(aligned,event))


def fake_forecaster(calls):
    async def predict(point, records, source):
        calls.append(dict(point))
        result=dict(point,source=source,prediction_s=30.,late_probability=.2,
                    current_deviation_s=point['cur_dev_s'],degraded=False,
                    horizon_s=epoch(point['target_time_begin'])-epoch(point['T']))
        backend.vehicles[int(point['tr_id'])]=result
        return result
    return predict


def event(start, dt=0., valid=True, lon=37.):
    return dict(tr_id=1,event_time=iso(start+dt),lon=lon,lat=55.,speed=10.,location_valid=valid)


def test_live_forecast_follows_packets_without_duplicate_inference(fleet,monkeypatch):
    calls=[]
    monkeypatch.setattr(backend,'forecast',fake_forecaster(calls))
    async def run():
        for dt in (0.,0.,.5,1.,3.,6.):
            await backend.ingest(event(fleet,dt))
    asyncio.run(run())
    assert [epoch(p['T'])-fleet for p in calls]==[0.,1.,3.,6.]
    assert len(backend.history[1])==5
    assert backend.counters['late_or_duplicate_packets']==1


def test_target_leaving_strict_window_is_cleared_even_inside_rate_limit(fleet,monkeypatch):
    calls=[]
    monkeypatch.setattr(backend,'forecast',fake_forecaster(calls))
    async def run():
        await backend.ingest(event(fleet,119.5))
        await backend.ingest(event(fleet,120.))
    asyncio.run(run())
    assert len(calls)==1
    assert backend.vehicles[1]['prediction_s'] is None


@pytest.mark.parametrize('valid,lon',[(False,37.),(True,39.)])
def test_bad_position_cannot_resurrect_previous_deviation(fleet,monkeypatch,valid,lon):
    monkeypatch.setattr(backend,'forecast',fake_forecaster([]))
    async def run():
        await backend.ingest(event(fleet))
        assert 1 in backend.deviations
        await backend.ingest(event(fleet,.5,valid,lon))
    asyncio.run(run())
    assert 1 not in backend.deviations
    result=backend.operational_vehicle(1,fleet+.5)
    assert result['current_deviation_s'] is None
    assert result['position_match'] is None


def test_yesterdays_packet_cannot_reanchor_fleet(fleet):
    original=backend.schedule.copy()
    asyncio.run(backend.ingest(event(fleet,-86400)))
    pd.testing.assert_frame_equal(backend.schedule,original)
    assert backend.last_telemetry_received_at==0.
    assert not backend.history[1]


def test_synthetic_clock_does_not_jump_when_calendar_rolls_over(fleet,monkeypatch):
    start=epoch('2026-01-06T20:59:57Z')  # three seconds before Moscow midnight
    backend.schedule['ts']+=start-fleet
    backend.schedule['time_begin']=backend.schedule.ts.map(iso)
    monkeypatch.setattr(backend,'forecast',fake_forecaster([]))
    async def run():
        await backend.ingest({**event(start),'telemetry_source':'custom_ndtp_nav00'})
        await backend.ingest({**event(start,6),'telemetry_source':'custom_ndtp_nav00'})
    asyncio.run(run())
    assert backend.live_schedule_day==timestamp(iso(start+6)).date()
    assert backend.vehicles[1]['current_deviation_s']==pytest.approx(6.,abs=.1)
    assert backend.position_offsets[1]==-86400.


def test_real_first_packet_preserves_actual_delay_but_emulator_calibrates(fleet):
    real=backend.estimate_position(1,37.005,55.,fleet+360+45,speed=10,calibrate_start=False)
    assert real['deviation_s']==pytest.approx(45,abs=.1)
    backend.position_offsets.clear();backend.position_states.clear()
    synthetic=backend.estimate_position(1,37.005,55.,fleet+360+45,speed=10,calibrate_start=True)
    assert synthetic['deviation_s']==0.


def test_reserve_remaining_distance_is_not_multiplied_twice(fleet):
    vehicle=dict(tr_id=1,lon=37.005,lat=55.,target_stop_id=2,current_deviation_s=300,
                 prediction_s=10,horizon_s=720,position_match=backend.match_stop(1,37.005,55.))
    placement=backend.reserve_placement(vehicle)
    expected=haversine(37.005,55.,37.01,55.)
    assert placement['distance_to_target_m']==pytest.approx(expected,abs=.1)
    assert placement['reserve_eta_s']==pytest.approx(expected/(placement['reserve_speed_kmh']/3.6),abs=.1)
    assert placement['slack_s']==pytest.approx(720-placement['reserve_eta_s'],abs=.1)
    assert 0<=placement['after_prediction_s']<=10
    assert placement['relief_s']<=10


def test_remaining_horizon_counts_down_without_changing_model_input(fleet):
    backend.vehicles[1]=dict(source='live',T=iso(fleet),target_time_begin=iso(fleet+720),
                            horizon_s=720,position_time=fleet,lon=37.,lat=55.,prediction_s=100)
    result=backend.operational_vehicle(1,fleet+4)
    assert result['horizon_s']==720
    assert result['remaining_horizon_s']==716
    assert result['forecast_age_s']==4
    assert backend.vehicles[1]['horizon_s']==720


def test_reserve_temporal_match_accounts_for_current_delay(fleet):
    vehicle=dict(tr_id=1,T=iso(fleet+405),position_time=fleet+410,target_time_begin=iso(fleet+720),
                 target_stop_id=2,current_deviation_s=50,prediction_s=100,horizon_s=310,
                 lon=37.005,lat=55.,position_match=backend.match_stop(1,37.005,55.))
    placement=backend.reserve_placement(vehicle)
    assert placement['lon']==pytest.approx(37.005)
    assert placement['distance_to_target_m']==pytest.approx(haversine(37.005,55.,37.01,55.),abs=.1)
    assert placement['track'][-1]['lon']==37.01


def test_action_case_numeric_time_uses_seconds_not_nanoseconds():
    assert backend._moscow_time(1767665400).timestamp()==1767665400


@pytest.mark.parametrize('state',['paused','historical','offline','waiting'])
def test_unsafe_action_never_uses_non_live_evidence(state):
    vehicle=dict(prediction_s=300,late_probability=.8,current_deviation_s=100,
                 horizon_s=720,connection_state=state)
    assert not backend.driver_decision(vehicle,'accelerate_safely')['allowed']


def test_decision_uses_remaining_horizon_and_does_not_treat_zero_as_missing():
    vehicle=dict(prediction_s=300,late_probability=.8,current_deviation_s=0,horizon_s=720,connection_state='live')
    assert backend.driver_decision(vehicle,'accelerate_safely')['expected_effect']['recoverable_delay_s']==30
    vehicle['remaining_horizon_s']=599
    assert not backend.driver_decision(vehicle,'accelerate_safely')['allowed']


def test_kpis_exclude_degraded_and_invalid_predictions():
    base=dict(tr_id=1,prediction_s=300,late_probability=.5,level='medium')
    items=[base,{**base,'tr_id':2,'degraded':True},{**base,'tr_id':3,'stale':True},
           {**base,'tr_id':4,'prediction_s':np.nan},{**base,'tr_id':5,'on_route':False}]
    result=backend.business_kpis(items)
    assert result['active_vehicles']==4
    assert result['forecasted_vehicles']==1
    assert result['coverage_pct']==25.
    assert result['expected_delay_minutes']==2.5


def test_shared_ml_failure_backoff_recovers_and_does_not_slide(fleet,monkeypatch):
    clock=[100.];requests=[]
    monkeypatch.setattr(backend.time,'monotonic',lambda:clock[0])
    def respond(request):
        requests.append(request)
        if len(requests)==1:raise httpx.ConnectError('down')
        return httpx.Response(200,json={'predictions':[dict(prediction_s=30,late_probability=.2,lower_s=-10,upper_s=70)]})
    point=dict(tr_id=1,T=iso(fleet),target_stop_id=2,target_time_begin=iso(fleet+720),cur_dev_s=0)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(backend,'client',client)
            assert (await backend.forecast(point,[],'live'))['degraded']
            clock[0]=101.
            assert (await backend.forecast(point,[],'live'))['degraded']
            assert backend.ml_retry_after==105.
            clock[0]=105.
            assert not (await backend.forecast(point,[],'live'))['degraded']
    asyncio.run(run())
    assert len(requests)==2
    assert backend.counters['ml_backoff_skips']==1


@pytest.mark.parametrize('response',[
    {}, {'predictions':[]}, {'predictions':[{'prediction_s':30}]},
    {'predictions':[{'prediction_s':30,'late_probability':2}]},
])
def test_invalid_ml_response_is_explicit_fallback_not_broken_telemetry(fleet,monkeypatch,response):
    point=dict(tr_id=1,T=iso(fleet),target_stop_id=2,target_time_begin=iso(fleet+720),cur_dev_s=45)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=response))) as client:
            monkeypatch.setattr(backend,'client',client)
            return await backend.forecast(point,[],'live')
    result=asyncio.run(run())
    assert result['degraded'] and result['level']=='unknown'
    assert result['late_probability'] is None


def test_ml_request_error_does_not_suspend_predictions_for_other_vehicles(fleet,monkeypatch):
    point=dict(tr_id=1,T=iso(fleet),target_stop_id=2,target_time_begin=iso(fleet+720),cur_dev_s=45)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(422,json={}))) as client:
            monkeypatch.setattr(backend,'client',client)
            assert (await backend.forecast(point,[],'live'))['degraded']
    asyncio.run(run())
    assert backend.ml_retry_after==0.


@pytest.mark.parametrize('prediction',[-1000.,0.,120.,1000.])
def test_probability_threshold_and_intervals_match_ml_exactly(prediction,monkeypatch):
    meta=dict(calibration_residuals=[-100.,0.,100.],interval_radius_s=50.,late_threshold_s=120.)
    monkeypatch.setattr(backend,'model_meta',meta)
    predictor=Predictor.__new__(Predictor);predictor.meta=meta
    predictor.predict_v5=lambda *args:np.array([prediction])
    ml=predictor.forecast_v5([{}],[[]],[[]])[0]
    api=backend.calibrated_uncertainty(prediction)
    assert api['late_probability']==ml['late_probability']
    assert 0<ml['late_probability']<1
    assert api['lower_s']==ml['lower_s']==prediction-50
    assert api['upper_s']==ml['upper_s']==prediction+50
    if prediction==120:assert ml['late_probability']==2/5  # strict >, not >=


@pytest.mark.parametrize('lon,lat,lon2,lat2',[(37.,55.,37.01,55.01),(0.,0.,180.,0.),(179.9,10.,-179.9,10.),(0.,90.,0.,-90.)])
def test_coordinate_order_and_distance_units_agree(lon,lat,lon2,lat2):
    distance=haversine(lon,lat,lon2,lat2)
    assert distance==pytest.approx(haversine_m(lat,lon,lat2,lon2),abs=1e-6)
    assert distance==pytest.approx(emulator.haversine_m(lon,lat,lon2,lat2),abs=1e-6)
    assert np.isfinite(distance) and distance>=0


def test_v5_features_are_causal_and_use_population_std():
    start=epoch('2026-01-06T10:00:00Z')
    point=dict(tr_id=1,T=iso(start),target_stop_id=2,target_time_begin=iso(start+720),cur_dev_s=30)
    rows=[{**event(start,dt), 'speed':speed} for dt,speed in [(-60,100),(-30,0),(0,20)]]
    baseline=build_v5_row(point,rows,[])
    future=build_v5_row({**point,'target_delay_s':999999},rows+[event(start,1)],[])
    pd.testing.assert_frame_equal(baseline,future)
    assert baseline.loc[0,'w60_events']==2  # (T-window,T], not inclusive left
    assert baseline.loc[0,'w60_speed_mean']==10
    assert baseline.loc[0,'w60_speed_std']==10
    assert baseline.loc[0,'w60_stopped_fraction']==.5
    compact=build_one(point,[{**r,'ts':epoch(r['event_time'])} for r in rows[1:]])
    assert compact['speed_trend']==40  # km/h per minute, not per second


@pytest.mark.parametrize('repeats',[False,True])
def test_debug_speed_is_constant_across_different_segment_speeds(repeats,monkeypatch):
    path=[(0.,0.),(.001,0.),(.003,0.)]
    timings=[0.,100.,110.]
    if repeats:
        path.insert(1,path[0]);timings=[0.,20.,100.,110.]
    vehicle=dict(tr_id=1,base_tr_id=1,path=path,timings=timings)
    state=emulator.initial_vehicle_state(vehicle)
    monkeypatch.setitem(emulator.debug_speed_overrides,1,36.)
    lon,lat,speed=emulator.advance_vehicle(vehicle,state,elapsed_s=20)
    assert emulator.haversine_m(0.,0.,lon,lat)==pytest.approx(200,abs=1e-6)
    assert speed==36
    # Travel multiple loops without hanging or losing the distance remainder.
    lon,lat,speed=emulator.advance_vehicle(vehicle,state,elapsed_s=200)
    length=emulator.haversine_m(*path[0],*path[-1])
    assert emulator.haversine_m(0.,0.,lon,lat)==pytest.approx(2200%length,abs=1e-6)


def test_debug_stationary_geometry_cannot_divide_by_zero(monkeypatch):
    vehicle=dict(tr_id=1,base_tr_id=1,path=[(0.,0.),(0.,0.)],timings=[0.,100.])
    state=emulator.initial_vehicle_state(vehicle)
    monkeypatch.setitem(emulator.debug_speed_overrides,1,130.)
    assert emulator.advance_vehicle(vehicle,state,elapsed_s=100000)[:2]==(0.,0.)


def test_predictor_rejects_truncated_parallel_inputs():
    predictor=Predictor.__new__(Predictor);predictor.kind='v5'
    with pytest.raises(ValueError,match='equal length'):
        predictor.predict_v5([{},{}],[[]],[[],[]])
