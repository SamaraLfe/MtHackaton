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


def test_submission_complete_and_finite():
    from pathlib import Path
    p=Path('artifacts/submission.csv')
    if not p.exists():pytest.skip('Run training first')
    result=pd.read_csv(p,sep=';')
    assert list(result)==['sample_id','prediction']
    assert result.sample_id.is_unique and np.isfinite(result.prediction).all()
