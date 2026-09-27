import struct
import numpy as np
import pandas as pd
import pytest
from ml.features import build_one, epoch, load_schedule
from backend.app import align_schedule_to_event_day
from backend.ndtp import decode, crc16

def point(minutes=12):
    return dict(tr_id=1,T='2026-01-06 10:00:00',target_time_begin=f'2026-01-06 10:{minutes:02d}:00',cur_dev_s=30,target_stop_id=7)

def row(seconds,speed=10,valid=True):
    return dict(ts=epoch('2026-01-06 10:00:00')+seconds,lon=37.6,lat=55.7,speed=speed,location_valid=valid)

def test_future_does_not_change_features():
    before=build_one(point(),[row(-30),row(0)])
    after=build_one(point(),[row(-30),row(0),row(1,120),row(800,0)])
    assert before==after

@pytest.mark.parametrize('minutes',[0,9,10,16])
def test_horizon_rejects(minutes):
    with pytest.raises(ValueError):build_one(point(minutes),[])

@pytest.mark.parametrize('minutes',[11,12,15])
def test_horizon_valid(minutes):
    assert build_one(point(minutes),[])['horizon_s']==60*minutes

def test_timezones_match_sample_identifier():
    assert epoch('2026-01-06 02:10:00')==1767665400
    assert epoch('2026-01-06T05:10:00+03:00')==1767665400

def test_missing_gps_and_long_gaps():
    assert build_one(point(),[row(0,valid=False)])['missing_gps']==1
    assert build_one(point(),[row(-200,0),row(0,0)])['idle_s']==0

def frame(payload,service=1,kind=101):
    body=struct.pack('<HHHI',service,kind,1,1)+payload
    crc=crc16(body);crc=((crc&255)<<8)|(crc>>8)
    return struct.pack('<HHHHBIH',0x7e7e,len(body),0,crc,2,123,0)+body

def navigation(bits=224):
    return bytes([0,0])+struct.pack('<IIIBBHHHHHBB',1767665400,376000000,557000000,bits,100,20,25,90,10,150,10,1)

def test_ndtp_decode_and_crc():
    raw=frame(navigation());r=decode(raw)[0]
    assert r['lon']==37.6 and r['lat']==55.7 and r['speed']==20 and r['location_valid']
    assert decode(frame(navigation(128)))[0]['lat']==-55.7
    with pytest.raises(ValueError):decode(raw[:-1]+bytes([raw[-1]^1]))
    with pytest.raises(ValueError):decode(raw[:-4])

def test_ndtp_optional_unknown_and_truncated():
    assert len(decode(frame(navigation()+bytes([255,0,1,2]))))==1
    with pytest.raises(ValueError):decode(frame(bytes([0,0,1,2])))

def test_ndtp_handshake():
    assert decode(frame(struct.pack('<HHHIII',6,2,0,123,65535,0),service=0,kind=100))==[]

def test_schedule_cannot_read_actuals():
    from io import StringIO
    text=pd.DataFrame([dict(tt_action_item_id=7,tr_id=1,time_begin='2026-01-06 10:12:00',time_fact_begin='2099-01-01',geom='POINT (37.6 55.7)',building_address='Stop')]).to_csv(index=False)
    assert 'time_fact_begin' not in load_schedule(StringIO(text)).columns

def test_live_schedule_can_be_anchored_to_event_day():
    schedule = pd.DataFrame([{
        'tt_action_item_id': 7, 'tr_id': 1,
        'time_begin': '2026-01-06 10:12:00',
        'ts': epoch('2026-01-06 10:12:00'),
        'geom': 'POINT (37.6 55.7)', 'building_address': 'Stop',
        'lon': 37.6, 'lat': 55.7,
    }])
    aligned = align_schedule_to_event_day(schedule, '2026-09-25 10:00:00')
    assert aligned.iloc[0].time_begin.startswith('2026-09-25 10:12:00')
    assert aligned.iloc[0].ts == epoch('2026-09-25 10:12:00')


def test_slow_stop_deviation_uses_live_calibration():
    """A slow packet at a stop must not compare live time to the old plan day."""
    from backend import app as backend

    saved_schedule = backend.schedule
    saved_offsets = dict(backend.position_offsets)
    saved_states = dict(backend.position_states)
    saved_deviations = dict(backend.deviations)
    try:
        backend.schedule = pd.DataFrame([
            dict(tt_action_item_id=1, tr_id=99, ts=1000, time_begin='1970-01-01 00:16:40', lon=37.60, lat=55.70, building_address='A'),
            dict(tt_action_item_id=2, tr_id=99, ts=1600, time_begin='1970-01-01 00:26:40', lon=37.61, lat=55.71, building_address='B'),
            dict(tt_action_item_id=3, tr_id=99, ts=2200, time_begin='1970-01-01 00:36:40', lon=37.62, lat=55.72, building_address='C'),
        ])
        backend.position_offsets.clear()
        backend.position_states.clear()
        backend.deviations.clear()
        match = backend.estimate_position(99, 37.60, 55.70, 10_000, speed=1)
        assert match is not None
        assert match['match_kind'] == 'observed_slow_stop'
        assert abs(match['deviation_s']) < 1
        assert match['expected_ts'] == 10_000
    finally:
        backend.schedule = saved_schedule
        backend.position_offsets.clear(); backend.position_offsets.update(saved_offsets)
        backend.position_states.clear(); backend.position_states.update(saved_states)
        backend.deviations.clear(); backend.deviations.update(saved_deviations)


def test_slow_stop_on_long_route_does_not_become_multi_hour_delay():
    """A restarted synthetic route may be less than one full loop behind the plan."""
    from backend import app as backend

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(backend, 'schedule', pd.DataFrame([
            dict(tt_action_item_id=1, tr_id=99, ts=1000, time_begin='1970-01-01 00:16:40', lon=37.60, lat=55.70, building_address='A'),
            dict(tt_action_item_id=2, tr_id=99, ts=4600, time_begin='1970-01-01 01:16:40', lon=37.61, lat=55.71, building_address='B'),
            dict(tt_action_item_id=3, tr_id=99, ts=8200, time_begin='1970-01-01 02:16:40', lon=37.62, lat=55.72, building_address='C'),
        ]))
        monkeypatch.setattr(backend, 'position_offsets', {})
        monkeypatch.setattr(backend, 'position_states', {})
        monkeypatch.setattr(backend, 'deviations', {})

        match = backend.estimate_position(99, 37.60, 55.70, 4700, speed=1)
        assert match is not None
        assert match['match_kind'] == 'observed_slow_stop'
        assert abs(match['deviation_s']) < 1
        assert backend.position_offsets[99] == 3700
    finally:
        monkeypatch.undo()


def test_repeated_geometry_uses_temporal_segment_hint(monkeypatch):
    """A duplicate coordinate in a loop must resolve to the timed segment."""
    from backend import app as backend

    monkeypatch.setattr(backend, 'schedule', pd.DataFrame([
        dict(tt_action_item_id=1, tr_id=99, ts=1000, time_begin='1970-01-01 00:16:40', lon=37.60, lat=55.70, building_address='A'),
        dict(tt_action_item_id=2, tr_id=99, ts=1600, time_begin='1970-01-01 00:26:40', lon=37.61, lat=55.71, building_address='B'),
        dict(tt_action_item_id=3, tr_id=99, ts=2200, time_begin='1970-01-01 00:36:40', lon=37.60, lat=55.70, building_address='A again'),
    ]))
    monkeypatch.setattr(backend, 'position_offsets', {})
    monkeypatch.setattr(backend, 'position_states', {})
    monkeypatch.setattr(backend, 'deviations', {})

    projected = backend.planned_position_at(99, 2150)
    match = backend.estimate_position(
        99,
        projected['lon'],
        projected['lat'],
        2150,
        speed=10,
        segment_hint=projected['segment_index'],
    )
    assert projected['segment_index'] == 1
    assert match['segment_index'] == 1
    assert match['deviation_s'] == pytest.approx(0, abs=.1)


def test_synthetic_zero_length_segment_uses_time_not_fake_stop(monkeypatch):
    """A duplicate stop coordinate must keep the planned time interpolation."""
    from backend import app as backend

    monkeypatch.setattr(backend, 'schedule', pd.DataFrame([
        dict(tt_action_item_id=1, tr_id=99, ts=1000, time_begin='1970-01-01 00:16:40', lon=37.60, lat=55.70, building_address='A'),
        dict(tt_action_item_id=2, tr_id=99, ts=1600, time_begin='1970-01-01 00:26:40', lon=37.61, lat=55.71, building_address='B'),
        dict(tt_action_item_id=3, tr_id=99, ts=2200, time_begin='1970-01-01 00:36:40', lon=37.61, lat=55.71, building_address='B again'),
        dict(tt_action_item_id=4, tr_id=99, ts=2800, time_begin='1970-01-01 00:46:40', lon=37.62, lat=55.72, building_address='C'),
    ]))
    monkeypatch.setattr(backend, 'position_offsets', {})
    monkeypatch.setattr(backend, 'position_states', {})
    monkeypatch.setattr(backend, 'deviations', {})

    projected = backend.planned_position_at(99, 1900)
    match = backend.estimate_position(
        99,
        projected['lon'],
        projected['lat'],
        1900,
        speed=1,
        segment_hint=projected['segment_index'],
        fraction_hint=projected['fraction'],
        allow_slow_stop=False,
    )
    assert projected['segment_index'] == 1
    assert match['segment_index'] == 1
    assert match['match_kind'] == 'planned_trajectory_segment'
    assert match['deviation_s'] == pytest.approx(0, abs=.1)


def test_submission_complete_and_finite():
    from pathlib import Path
    p=Path('artifacts/submission.csv')
    if not p.exists():pytest.skip('Run training first')
    result=pd.read_csv(p,sep=';')
    assert list(result)==['sample_id','prediction']
    assert result.sample_id.is_unique and np.isfinite(result.prediction).all()


def test_slow_motion_mid_segment_is_not_stop_arrival(monkeypatch):
    from backend import app as backend
    monkeypatch.setattr(backend, 'schedule', pd.DataFrame([
        dict(tt_action_item_id=1, tr_id=99, ts=1000, lon=37.60, lat=55.70, building_address='A'),
        dict(tt_action_item_id=2, tr_id=99, ts=1600, lon=37.62, lat=55.72, building_address='B'),
    ]))
    for name in ('position_offsets', 'position_states', 'deviations'):
        monkeypatch.setattr(backend, name, {})
    result = backend.estimate_position(99, 37.61, 55.71, 10000, speed=1)
    assert result['match_kind'] == 'planned_trajectory_segment'
    assert result['estimated'] is True
    assert result['deviation_s'] == pytest.approx(0, abs=.1)
    # Stationary for 30 seconds on a segment accumulates 30 seconds of delay.
    result = backend.estimate_position(99, 37.61, 55.71, 10030, speed=1)
    assert result['deviation_s'] == pytest.approx(30, abs=.1)


def test_sparse_plan_does_not_expand_forecast_horizon(monkeypatch):
    from backend import app as backend
    monkeypatch.setattr(backend, 'schedule', pd.DataFrame([
        dict(tr_id=99, ts=0), dict(tr_id=99, ts=2000),
    ]))
    assert backend.target_for(99, 0) is None
    invalid = {**point(16), 'horizon_fallback': True}
    with pytest.raises(ValueError):
        build_one(invalid, [])


def test_forecast_preserves_model_output_at_large_current_deviation(monkeypatch):
    import asyncio
    import httpx
    from backend import app as backend
    forecast_point = {**point(), 'cur_dev_s': 1800}
    target = epoch(forecast_point['target_time_begin'])
    monkeypatch.setattr(backend, 'schedule', pd.DataFrame([
        dict(tr_id=1, tt_action_item_id=7, ts=target, time_begin=forecast_point['target_time_begin'],
             lon=37.6, lat=55.7, building_address='Target'),
    ]))
    monkeypatch.setattr(backend, 'vehicles', {})
    model_result = dict(prediction_s=200, lower_s=-50, upper_s=450, late_probability=.6, model='v5')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'predictions': [model_result]})
        )) as client:
            monkeypatch.setattr(backend, 'client', client)
            return await backend.forecast(forecast_point, [row(0)], 'live')
    result = asyncio.run(run())
    for key, value in model_result.items():
        assert result[key] == value
    assert result['current_deviation_s'] == 1800
