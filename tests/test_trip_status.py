"""Trip status must not be inferred from the availability of an ML target."""
import pandas as pd
import pytest
from backend import app as backend


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(backend,'schedule',pd.DataFrame([
        dict(tr_id=1,ts=1000,lon=37.,lat=55.,building_address='Start'),
        dict(tr_id=1,ts=1600,lon=37.01,lat=55.01,building_address='End'),
    ]))
    monkeypatch.setattr(backend,'history',{})
    monkeypatch.setattr(backend,'vehicles',{})
    monkeypatch.setattr(backend,'deviations',{})
    monkeypatch.setattr(backend,'position_offsets',{})
    monkeypatch.setattr(backend,'state',{'mode':'live'})
    monkeypatch.setattr(backend,'live_track',lambda tr: [])
    return backend


def test_never_seen_vehicle_is_not_a_connection_failure(fleet):
    result=fleet.operational_vehicle(1,2000)
    assert result['trip_status']=='not_started'
    assert result['on_route'] is False
    assert result['attention_level']=='normal'
    assert 'не подтверждён' in result['reason']


@pytest.mark.parametrize('age',[20,3600])
def test_lost_vehicle_stays_critical_even_after_archive_fallback_timeout(fleet,age):
    fleet.vehicles[1]=dict(source='live',position_time=2000,lon=37.,lat=55.,prediction_s=120)
    result=fleet.operational_vehicle(1,2000+age)
    assert result['on_route'] is True
    assert result['attention_level']=='critical'
    assert result['level']=='high'
    assert result['prediction_s'] is None


def test_missing_prediction_target_does_not_end_trip(fleet):
    fleet.vehicles[1]=dict(source='live',position_time=2000,lon=37.005,lat=55.005,prediction_s=None)
    result=fleet.operational_vehicle(1,2001)
    assert result['trip_status']=='active'
    assert result['status_label']=='Нет контрольной точки'


def test_final_position_ends_trip_even_after_connection_stops(fleet):
    fleet.vehicles[1]=dict(source='live',position_time=2000,lon=37.01,lat=55.01)
    fleet.deviations[1]={'position_match':dict(segment_index=0,fraction=1,deviation_s=40)}
    result=fleet.operational_vehicle(1,9000)
    assert result['trip_status']=='completed'
    assert result['on_route'] is False
    assert result['next_stop'] is None
    assert result['attention_level']=='normal'
    assert result['route_start_stop']=='Start'
    assert result['route_end_stop']=='End'
    assert pd.Timestamp(result['previous_stop_time']).timestamp()==1040


def test_intentional_pause_is_not_connection_failure(fleet):
    fleet.state['ingest_paused']=True
    fleet.vehicles[1]=dict(source='live',position_time=2000)
    result=fleet.operational_vehicle(1,9000)
    assert result['connection_state']=='paused'
    assert result['attention_level']=='normal'


def test_all_streams_stopped_switch_to_historical_fallback(fleet, monkeypatch):
    monkeypatch.setattr(backend, 'archive_vehicles', {
        1: dict(tr_id=1, source='historical_v5', prediction_s=90, level='low',
                lon=37.0, lat=55.0, on_route=True),
    })
    monkeypatch.setattr(backend, 'points', pd.DataFrame())
    fleet.state['ingest_paused'] = True

    assert backend.telemetry_fallback_active(now=2000) is True
    result = backend.historical_fallback_state()

    assert result['state']['mode'] == 'replay'
    assert result['state']['fallback_mode'] == 'historical'
    assert result['vehicles'][0]['source'] == 'historical_fallback'
    assert result['vehicles'][0]['prediction_s'] == 90
    assert 'недоступен' in result['vehicles'][0]['reason']


def test_fallback_waits_for_the_configured_connection_grace_period(fleet, monkeypatch):
    monkeypatch.setattr(backend, 'last_telemetry_received_at', 100.0)
    monkeypatch.setattr(backend, 'LIVE_FALLBACK_S', 60)
    fleet.state['ingest_paused'] = False

    assert backend.telemetry_fallback_active(now=159.9) is False
    assert backend.telemetry_fallback_active(now=160.0) is True
