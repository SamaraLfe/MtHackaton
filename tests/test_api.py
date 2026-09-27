import asyncio
import json
import os
import sqlite3
from pathlib import Path
import httpx
import pytest
import time
import pandas as pd
from fastapi.testclient import TestClient
from ml.features import FEATURES
from ml.service import app as ml_app
from backend import app as backend


def cleanup_action_case(attempt_id):
    """Remove only the case created by a test, preserving any prior user case."""
    with sqlite3.connect(backend.DB_PATH) as store:
        rows=store.execute('SELECT id,payload FROM dispatcher_action_cases').fetchall()
        for case_id,payload in rows:
            if json.loads(payload).get('attempt_id')==attempt_id:
                store.execute('DELETE FROM dispatcher_action_cases WHERE id=?',(case_id,))
        store.commit()


def test_risk_and_incident_helpers():
    items=[
        {'tr_id':10,'level':'high','late_probability':.8,'prediction_s':180,'reason':'slow'},
        {'tr_id':11,'level':'low','late_probability':.1},
        {'tr_id':12,'level':'unknown','late_probability':None},
    ]
    routes=backend.route_risk(items)
    assert routes[0]['tr_id']==10 and routes[0]['level']=='high'
    assert [x['vehicle_id'] for x in backend.incidents(items)]==[10]


def test_business_kpis_are_explainable_and_risk_weighted():
    result=backend.business_kpis([
        {'tr_id':10,'level':'high','late_probability':.8,'prediction_s':180},
        {'tr_id':11,'level':'low','late_probability':.1,'prediction_s':-30},
        {'tr_id':12,'level':'unknown','late_probability':None,'prediction_s':None},
    ])
    assert result['active_vehicles']==3
    assert result['forecasted_vehicles']==2
    assert result['expected_delay_minutes']==2.4
    assert result['top_priority_tr_id']==10


def test_driver_decision_has_guardrails_and_follow_up():
    vehicle={
        'tr_id':10,'level':'high','prediction_s':180,'current_deviation_s':150,
        'late_probability':.8,'horizon_s':720,'connection_state':'live','stale':False,
        'trip_status':'active',
    }
    ready=backend.driver_decision(vehicle,'accelerate_safely')
    assert ready['allowed'] is True
    assert ready['evidence']['expected_delay_s']==144
    assert ready['next_check_in_s']<=720

    blocked=backend.driver_decision({**vehicle,'prediction_s':60},'accelerate_safely')
    assert blocked['allowed'] is False
    assert '120' in ' '.join(blocked['blockers'])
    assert ready['utility']['model']=='benefit-v1-rule-based'
    assert ready['utility']['expected_saved_delay_s']>=0


def test_action_center_orders_bad_results_and_reads_only_owner_cases(monkeypatch):
    now=pd.Timestamp('2026-01-06T12:00:00+03:00')
    requested=[]
    monkeypatch.setattr(backend,'get_dispatcher',lambda dispatcher_id: {'id':dispatcher_id} if dispatcher_id=='dispatcher-01' else None)
    monkeypatch.setattr(backend,'stored_action_cases',lambda dispatcher_id,limit=200: requested.append(dispatcher_id) or [
        {'id':'case-yellow','dispatcher_id':'dispatcher-01','tr_id':10,'status':'completed_no_result','tone':'no_result','updated_at':'2026-01-06T11:59:00+03:00'},
        {'id':'case-red','dispatcher_id':'dispatcher-01','tr_id':11,'status':'worsened','tone':'worsened','updated_at':'2026-01-06T11:58:00+03:00'},
    ])
    monkeypatch.setattr(backend,'reconcile_action_case',lambda case,now=None:case)

    items=backend.action_center_items('dispatcher-01',now)

    assert requested==['dispatcher-01']
    assert [item['id'] for item in items]==['case-red','case-yellow']
    assert all(item['dispatcher_id']=='dispatcher-01' for item in items)


def test_new_action_for_same_dispatcher_and_vehicle_updates_stable_case(monkeypatch):
    cases={}
    monkeypatch.setattr(backend,'stored_action_case',lambda dispatcher_id,tr_id:cases.get((dispatcher_id,tr_id)))
    monkeypatch.setattr(backend,'save_action_case',lambda case:cases.__setitem__((case['dispatcher_id'],case['tr_id']),dict(case)))
    monkeypatch.setattr(backend,'action_vehicle',lambda tr_id:{'prediction_s':180})
    first={'id':'cmd-1','tr_id':10,'dispatcher':{'id':'dispatcher-01'},'action':'contact','action_title':'Связаться','created_at':'2026-01-06T11:00:00+03:00'}
    second={**first,'id':'cmd-2','action':'accelerate_safely','action_title':'Сократить отставание','created_at':'2026-01-06T11:05:00+03:00'}

    case_one=backend.register_action_case(first,'driver_command')
    case_two=backend.register_action_case(second,'driver_command')

    assert case_two['id']==case_one['id']=='case-dispatcher-01-10'
    assert case_two['revision']==2
    assert case_two['attempt_id']=='cmd-2'
    assert case_two['status']=='pending'


def test_action_outcome_uses_absolute_timetable_deviation():
    assert backend.classify_action_outcome(180,90)=='completed_success'
    assert backend.classify_action_outcome(-120,-30)=='completed_success'
    assert backend.classify_action_outcome(180,175)=='completed_no_result'
    assert backend.classify_action_outcome(90,180)=='worsened'
    assert backend.classify_action_outcome(90,30,delivered=False)=='improved_independently'


def test_success_case_is_visible_for_three_seconds_only(monkeypatch):
    now=pd.Timestamp('2026-01-06T12:00:00+03:00')
    saved=[]
    case={
        'id':'case-dispatcher-01-10','dispatcher_id':'dispatcher-01','tr_id':10,
        'status':'completed_no_result','tone':'no_result','updated_at':now.isoformat(),
    }
    monkeypatch.setattr(backend,'save_action_case',lambda value:saved.append(dict(value)))
    resolved=backend.apply_action_case_outcome(case,'completed_success','Улучшилось',now,after=30)
    assert pd.Timestamp(resolved['visible_until'])-now==pd.Timedelta(seconds=3)
    monkeypatch.setattr(backend,'get_dispatcher',lambda dispatcher_id:{'id':dispatcher_id})
    monkeypatch.setattr(backend,'stored_action_cases',lambda dispatcher_id,limit=200:[resolved])
    monkeypatch.setattr(backend,'reconcile_action_case',lambda value,now=None:value)
    assert backend.action_center_items('dispatcher-01',now+pd.Timedelta(seconds=2))
    assert backend.action_center_items('dispatcher-01',now+pd.Timedelta(seconds=4))==[]


def test_bad_case_persists_until_newer_vehicle_observation_improves_it(monkeypatch):
    saved=[]
    case={
        'id':'case-dispatcher-01-10','dispatcher_id':'dispatcher-01','tr_id':10,
        'status':'worsened','tone':'worsened','delivery_confirmed':True,
        'baseline_prediction_s':120,'latest_prediction_s':180,
        'action_started_at':'2026-01-06T11:55:00+03:00',
        'last_observation_at':'2026-01-06T12:00:00+03:00',
        'updated_at':'2026-01-06T12:00:00+03:00','visible_until':None,
    }
    monkeypatch.setattr(backend,'save_action_case',lambda value:saved.append(dict(value)))
    monkeypatch.setattr(backend,'action_vehicle',lambda tr_id:{'prediction_s':30,'T':'2026-01-06T12:01:00+03:00','stale':False})
    resolved=backend.reconcile_action_case(case,pd.Timestamp('2026-01-06T12:01:00+03:00'))
    assert resolved['status']=='completed_success'
    assert resolved['tone']=='success'
    assert resolved['outcome_source']=='vehicle_telemetry'


def test_stale_driver_command_becomes_terminal_integration_timeout(monkeypatch):
    saved=[]
    old={'id':'cmd-old','tr_id':10,'created_at':'2026-01-06T11:00:00+03:00','status':'queued_for_integration'}
    monkeypatch.setattr(backend,'stored_driver_commands',lambda limit=2000:[dict(old)])
    monkeypatch.setattr(backend,'update_driver_command',lambda command:saved.append(command))
    expired=backend.expire_stale_driver_commands(pd.Timestamp('2026-01-06T12:00:00+03:00'),ttl_s=900)
    assert expired==1
    assert saved[0]['status']=='integration_timeout'
    assert saved[0]['simulation_available'] is False


def test_stale_reserve_action_becomes_terminal_integration_timeout(monkeypatch):
    saved=[]
    old={'id':'reserve-old','tr_id':10,'created_at':'2026-01-06T11:00:00+03:00','status':'queued_for_integration','external_execution':False}
    monkeypatch.setattr(backend,'stored_reserve_actions',lambda limit=2000:[dict(old)])
    monkeypatch.setattr(backend,'update_reserve_action',lambda action:saved.append(action))
    expired=backend.expire_stale_reserve_actions(pd.Timestamp('2026-01-06T12:00:00+03:00'),ttl_s=900)
    assert expired==1
    assert saved[0]['status']=='integration_timeout'
    assert saved[0]['external_execution'] is False
    assert 'резерва' in saved[0]['terminal_reason']


def test_action_center_endpoint_returns_operational_summary():
    with TestClient(backend.app) as client:
        response=client.get('/api/action-center',params={'dispatcher_id':'dispatcher-01'})
        assert response.status_code==200
        data=response.json()
        assert {'items','summary','as_of','dispatcher_id'}<=set(data)
        assert {'open','due','blocked','high_priority','reserve_timed_out'}<=set(data['summary'])
        assert data['dispatcher_id']=='dispatcher-01'


def test_action_plan_ranks_actions_with_transparent_benefit_model():
    with TestClient(backend.app) as client:
        response=client.get('/api/action-plan',params={'tr_id':131672,'dispatcher_id':'dispatcher-02'})
        assert response.status_code==200,response.text
        data=response.json()
        assert data['model']=='benefit-v1-rule-based'
        assert len(data['options'])==4
        assert all('utility' in option and 'decision' in option for option in data['options'])
        assert data['recommended_action'] in {option['action'] for option in data['options']}


def test_driver_simulator_closes_allowed_command_from_open_queue():
    command_id=None
    old_mode=backend.state.get('mode')
    try:
        with TestClient(backend.app) as client:
            client.post('/api/mode',json={'mode':'replay'})
            created=client.post('/api/driver-commands',json={
                'role':'dispatcher','dispatcher_id':'dispatcher-02','tr_id':131672,'action':'maintain',
                'message':'Подтвердите движение по графику для локальной симуляции.'
            })
            assert created.status_code==201,created.text
            command=created.json();command_id=command['id']
            assert command['status']=='queued_for_integration'
            simulated=client.post(f"/api/driver-commands/{command_id}/simulate")
            assert simulated.status_code==200,simulated.text
            result=simulated.json()
            assert result['status']=='simulated_completed'
            assert result['simulated_response']['acknowledged'] is True
            assert result['simulated_response']['mode']=='local_driver_simulator'
            assert result['simulated_response']['next_step']['action']=='recheck_vehicle'
            assert result['simulated_response']['next_step']['check_at']
            center=client.get('/api/action-center',params={'dispatcher_id':'dispatcher-02'}).json()
            case=next(item for item in center['items'] if item['attempt_id']==command_id)
            assert case['status']=='completed_no_result'
            assert case['tone']=='no_result'
    finally:
        if command_id:
            with sqlite3.connect(backend.DB_PATH) as store:
                store.execute('DELETE FROM driver_commands WHERE id=?',(command_id,))
                store.commit()
            cleanup_action_case(command_id)
        backend.state['mode']=old_mode


def test_driver_simulator_rejects_expired_command():
    command_id=None
    try:
        with TestClient(backend.app) as client:
            created=client.post('/api/driver-commands',json={
                'role':'dispatcher','dispatcher_id':'dispatcher-02','tr_id':131672,'action':'maintain',
                'message':'Проверка недоступности просроченного канала.'
            })
            assert created.status_code==201,created.text
            command=created.json();command_id=command['id']
            command['status']='integration_timeout'
            command['simulation_available']=False
            backend.update_driver_command(command)
            response=client.post(f"/api/driver-commands/{command_id}/simulate")
            assert response.status_code==409
            assert 'недоступна' in response.json()['detail']['message']
    finally:
        if command_id:
            with sqlite3.connect(backend.DB_PATH) as store:
                store.execute('DELETE FROM driver_commands WHERE id=?',(command_id,))
                store.commit()
            cleanup_action_case(command_id)


def test_reserve_placement_handles_vehicle_without_control_stop():
    saved=backend.schedule
    backend.schedule=pd.DataFrame([
        dict(tt_action_item_id=7,tr_id=1,ts=100,time_begin='2026-01-06 10:00:00',lon=37.6,lat=55.7,building_address='Stop A'),
        dict(tt_action_item_id=8,tr_id=1,ts=200,time_begin='2026-01-06 10:10:00',lon=37.61,lat=55.71,building_address='Stop B'),
    ])
    try:
        placement=backend.reserve_placement({
            'tr_id':1,'target_stop_id':None,'T':None,'target_time_begin':None,
            'current_deviation_s':30,'prediction_s':180,'horizon_s':600,
            'stale':False,'connection_state':'live',
            'position_match':{'segment_index':0,'fraction':0.2,'projected_lon':37.602,'projected_lat':55.702,'confidence':0.8},
        })
        assert placement is not None
        assert placement['target_stop_address']=='Stop B'
    finally:
        backend.schedule=saved


def test_metrics_expose_readable_v5_summary():
    with TestClient(backend.app) as client:
        response=client.get('/api/metrics')
        assert response.status_code==200
        data=response.json()
        assert data['v5']['model']=='v5'
        assert data['v5']['features']==60
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
        assert data['impact']['saved_expected_delay_minutes']==.4
        backend.vehicles.clear()


def test_reserve_release_is_persisted_only_after_evidence_gate():
    action_id=None
    try:
        with TestClient(backend.app) as client:
            client.post('/api/mode',json={'mode':'replay'})
            created=client.post('/api/reserve-dispatches',json={'dispatcher_id':'dispatcher-02','tr_id':131672})
            assert created.status_code==201,created.text
            action=created.json();action_id=action['id']
            assert action['status']=='queued_for_integration'
            assert action['decision']['allowed'] is True
            assert action['placement']['reserve_eta_s']<=action['placement']['horizon_s']
            history=client.get('/api/reserve-dispatches',params={'tr_id':131672}).json()['items']
            assert any(item['id']==action_id for item in history)
            simulated=client.post(f'/api/reserve-dispatches/{action_id}/simulate')
            assert simulated.status_code==200,simulated.text
            assert simulated.json()['status']=='simulated_completed'
            center=client.get('/api/action-center',params={'dispatcher_id':'dispatcher-02'}).json()
            case=next(item for item in center['items'] if item['attempt_id']==action_id)
            assert case['dispatcher_id']=='dispatcher-02'
            assert case['status'] in {'completed_success','completed_no_result'}
    finally:
        if action_id:
            with sqlite3.connect(backend.DB_PATH) as store:
                store.execute('DELETE FROM reserve_actions WHERE id=?',(action_id,))
                store.commit()
            cleanup_action_case(action_id)
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
        assert matched['match_kind']=='planned_trajectory_segment'
        assert matched['segment_start_stop_id']==7 and matched['next_stop_id']==8
        assert matched['distance_to_next_stop_m']>0 and matched['road_graph_matched'] is False
        assert client.post('/api/admin/simulation',json={'role':'dispatcher','tr_id':1}).status_code==403
        backend.schedule=saved


def test_dispatcher_command_is_persisted_in_the_local_outbox():
    command_id=None
    try:
        with TestClient(backend.app) as client:
            response=client.post('/api/driver-commands',json={
                'role':'dispatcher','dispatcher_id':'dispatcher-01','tr_id':131672,'action':'accelerate_safely',
                'message':'При возможности сократить отставание без нарушения безопасности.'
            })
            assert response.status_code==201,response.text
            command=response.json();command_id=command['id']
            assert command['status']==('queued_for_integration' if command['decision']['allowed'] else 'blocked_by_guardrail')
            assert command['decision']['action']=='accelerate_safely'
            assert command['channel']=='local_dispatch_outbox'
            assert command['dispatcher']['id']=='dispatcher-01'
        with sqlite3.connect(backend.DB_PATH) as store:
            payload=store.execute('SELECT payload FROM driver_commands WHERE id=?',(command_id,)).fetchone()[0]
            assert json.loads(payload)['id']==command_id
        with TestClient(backend.app) as client:
            commands=client.get('/api/driver-commands',params={'tr_id':131672})
            assert commands.status_code==200
            assert commands.json()['items'][0]['id']==command_id
    finally:
        if command_id:
            with sqlite3.connect(backend.DB_PATH) as store:
                store.execute('DELETE FROM driver_commands WHERE id=?',(command_id,))
                store.commit()
            cleanup_action_case(command_id)


def test_control_room_loads_leaflet_stylesheet():
    with TestClient(backend.app) as client:
        response=client.get('/')
        assert response.status_code==200
        assert '/static/vendor/leaflet/leaflet.css' in response.text


def test_map_module_tolerates_dispatcher_page_without_legacy_controls():
    source=(Path('dashboard')/'map.js').read_text(encoding='utf-8')
    assert "getElementById('map-all-routes')?.checked" in source
    assert "getElementById('map-empty')?.classList" in source
    assert 'setView(marker.getLatLng(),targetZoom' in source


def test_dispatcher_uses_a_keyless_basemap_and_vehicle_terms():
    config=(Path('dashboard')/'map-config.js').read_text(encoding='utf-8')
    page=(Path('dashboard')/'dispatcher.html').read_text(encoding='utf-8')
    assert 'tile.openstreetmap.org' in config
    assert 'cartocdn.com' not in config
    assert '<span>ТС на маршруте</span>' in page
    assert 'КАРТА ТС' in page
    assert 'ВСЕ ТС' in page


def test_admin_count_explains_that_administrators_are_excluded():
    page=(Path('dashboard')/'admin-control.html').read_text(encoding='utf-8')
    assert 'ДИСПЕТЧЕРОВ (БЕЗ АДМИНИСТРАТОРА)' in page


def test_running_verifier_checks_readiness_and_runtime_metrics():
    from scripts import verify_running
    payloads={
        '/health/ready':{'status':'ready','checks':{'ml_api':True}},
        '/api/observability':{'status':'ok','request_latency_ms':{'samples':3},'queues':{}},
    }
    report=verify_running.runtime_contract(lambda path:payloads[path])
    assert report['readiness_ready'] is True
    assert report['observability_available'] is True
    assert report['observability_samples']==3


def test_vehicle_detail_identifies_the_position_source():
    source=(Path('dashboard')/'dispatcher.js').read_text(encoding='utf-8')
    assert 'ИСТОЧНИК ПОЗИЦИИ' in source
    assert 'Архивная телеметрия' in source
    assert 'Оригинальный эмулятор' in source


def test_dispatcher_explains_model_prediction_data_flow_and_reference_links():
    page=(Path('dashboard')/'dispatcher.html').read_text(encoding='utf-8')
    source=(Path('dashboard')/'dispatcher.js').read_text(encoding='utf-8')
    assert 'Контроль рейсов' in page
    assert 'Как читать прогноз' not in page
    assert 'Прогноз: архивный V5' in source
    assert '/docs' in page
    assert 'github.com/SamaraLfe/MtHackaton' in page
    assert 'late_probability' in source
    assert 'reason_is_hypothesis' in source
    assert '/static/dispatcher.js?v=' in page
    assert '/code' in page


def test_dispatcher_keeps_operational_reserve_what_if_workflow_visible():
    page=(Path('dashboard')/'dispatcher.html').read_text(encoding='utf-8')
    source=(Path('dashboard')/'dispatcher.js').read_text(encoding='utf-8')
    map_source=(Path('dashboard')/'map.js').read_text(encoding='utf-8')
    assert 'Выпустить резервное ТС' in page
    assert 'reserve-route' in page and 'reserve-mode' in page
    assert 'action-center-list' in page
    assert "'/api/what-if'" in source
    assert '/api/action-center' in source
    assert 'scenarioVehicle' in source and 'scenario-marker' in map_source


def test_attention_queue_does_not_label_on_time_vehicles_as_interventions():
    source=(Path('dashboard')/'dispatcher.js').read_text(encoding='utf-8')
    attention=source[source.index('function renderAttention()'):source.index('function renderActionCenter()')]
    assert '.concat(' not in attention
    assert 'Нет рейсов, требующих вмешательства' in attention


def test_backend_builds_and_exposes_code_documentation_route():
    source=(Path('backend')/'app.py').read_text(encoding='utf-8')
    runner=(Path('scripts')/'start_backend.py').read_text(encoding='utf-8')
    compose=(Path('compose.yaml')).read_text(encoding='utf-8')
    assert "@app.get('/code/'" in source
    assert 'sphinx' in runner
    assert 'python scripts/start_backend.py' in compose


def test_replay_mode_populates_a_multi_vehicle_historical_snapshot():
    with TestClient(backend.app) as client:
        response=client.post('/api/mode',json={'mode':'replay'})
        assert response.status_code==200
        snapshot=client.get('/api/state').json()
        assert len(snapshot['vehicles'])==11
        assert {v['source'] for v in snapshot['vehicles']}=={'historical_v5'}
        assert all(v['prediction_s'] is not None for v in snapshot['vehicles'])


def test_live_forecast_uses_schedule_on_telemetry_calendar_day():
    """A live alert must never point at a January stop for September telemetry."""
    with TestClient(backend.app) as client:
        client.post('/api/mode',json={'mode':'live'})
        now=time.time()-2
        event=dict(tr_id=131672,event_time=pd.Timestamp(now,unit='s',tz='UTC').isoformat(),lon=37.6,lat=55.7,speed=12)
        response=client.post('/api/telemetry',json=[event])
        assert response.status_code==200
        vehicle=next(v for v in client.get('/api/state').json()['vehicles'] if v['tr_id']==131672)
        event_day=pd.Timestamp(event['event_time']).tz_convert('Europe/Moscow').date()
        if vehicle['source']=='live':
            assert pd.Timestamp(vehicle['T']).date()==event_day
            assert vehicle['prediction_s'] is None or pd.Timestamp(vehicle['target_time_begin']).date()==event_day
        else:
            assert vehicle['live_position'] is True
            assert pd.Timestamp(vehicle['live_position_time']).date()==event_day


def test_dispatcher_profiles_are_available_for_local_workspaces():
    with TestClient(backend.app) as client:
        response=client.get('/api/dispatchers')
        assert response.status_code==200
        profiles=response.json()['profiles']
        assert len(profiles)>=2
        assert {'id','name','role'}<=set(profiles[0])


def test_senior_dispatcher_defaults_to_the_complete_fleet():
    with sqlite3.connect(backend.DB_PATH) as store:
        store.execute("DELETE FROM app_meta WHERE key='dispatcher_02_all_routes_v1'")
        store.execute("DELETE FROM assignments WHERE dispatcher_id='dispatcher-02'")
    with TestClient(backend.app) as client:
        profile=client.get('/api/dispatchers/dispatcher-02').json()
        expected=sorted(int(value) for value in backend.schedule.tr_id.unique())
        assert profile['name']=='Диспетчер №2 (все ТС)'
        assert profile['role']=='Старший диспетчер'
        assert profile['assigned_tr_ids']==expected


def test_admin_can_create_dispatcher_and_assign_routes():
    dispatcher_id=None
    try:
        with TestClient(backend.app) as client:
            created=client.post('/api/admin/dispatchers',json={
                'name':'Диспетчер тестового маршрута','login':f'route-test-{int(time.time()*1000)}','role':'Маршрутный диспетчер'
            })
            assert created.status_code==201,created.text
            dispatcher=created.json();dispatcher_id=dispatcher['id']
            assignment=client.put(f"/api/admin/dispatchers/{dispatcher_id}/assignments",json={'tr_ids':[131672,134040]})
            assert assignment.status_code==200,assignment.text
            profile=client.get(f"/api/dispatchers/{dispatcher_id}")
            assert profile.status_code==200
            assert profile.json()['assigned_tr_ids']==[131672,134040]
            scoped=client.get('/api/state',params={'dispatcher_id':dispatcher_id})
            assert {item['tr_id'] for item in scoped.json()['vehicles']}=={131672,134040}
            command=client.post('/api/driver-commands',json={
                'role':'dispatcher','dispatcher_id':dispatcher_id,'tr_id':131672,'action':'contact','message':'Подтвердите обстановку на следующем участке.'
            })
            assert command.status_code==201,command.text
    finally:
        if dispatcher_id:
            with sqlite3.connect(backend.DB_PATH) as db:
                db.execute('DELETE FROM assignments WHERE dispatcher_id=?',(dispatcher_id,))
                db.execute('DELETE FROM dispatchers WHERE id=?',(dispatcher_id,))


def test_simulation_reports_lifecycle_events_and_effect():
    run_id=None
    run=None
    try:
        with TestClient(backend.app) as client:
            client.post('/api/mode',json={'mode':'live'})
            created=client.post('/api/admin/simulations',json={
                'dispatcher_id':'admin-01','tr_id':131672,'scenario':'slow','count':2,'interval_s':5
            })
            assert created.status_code==202,created.text
            run_id=created.json()['id']
            deadline=time.time()+3
            while time.time()<deadline:
                run=client.get(f'/api/admin/simulations/{run_id}').json()
                if run['status'] in {'completed','failed'}:break
                time.sleep(.05)
            assert run is not None
            assert run['status']=='completed'
            assert len(run['events'])==2
            assert 'effect' in run and 'after' in run['effect']
    finally:
        if run_id:
            backend.simulation_runs.pop(run_id,None)
            with sqlite3.connect(backend.DB_PATH) as db:
                db.execute('DELETE FROM simulations WHERE id=?',(run_id,))


def test_simulation_can_be_cancelled_and_history_is_filterable():
    run_id=None
    run=None
    try:
        with TestClient(backend.app) as client:
            client.post('/api/mode',json={'mode':'live'})
            created=client.post('/api/admin/simulations',json={
                'dispatcher_id':'admin-01','tr_id':131672,'scenario':'slow','count':20,'interval_s':5
            })
            assert created.status_code==202,created.text
            run_id=created.json()['id']
            cancelled=client.post(f'/api/admin/simulations/{run_id}/cancel',json={'dispatcher_id':'admin-01'})
            assert cancelled.status_code==200,cancelled.text
            deadline=time.time()+3
            while time.time()<deadline:
                run=client.get(f'/api/admin/simulations/{run_id}').json()
                if run['status'] in {'cancelled','completed','failed'}:break
                time.sleep(.05)
            assert run is not None
            assert run['status']=='cancelled'
            history=client.get('/api/admin/simulations',params={'status':'cancelled','tr_id':131672,'limit':10})
            assert history.status_code==200
            assert any(item['id']==run_id for item in history.json()['items'])
    finally:
        if run_id:
            backend.simulation_runs.pop(run_id,None)
            with sqlite3.connect(backend.DB_PATH) as db:
                db.execute('DELETE FROM simulations WHERE id=?',(run_id,))


def test_state_exposes_live_track_and_runtime_endpoints():
    with TestClient(backend.app) as client:
        client.post('/api/mode',json={'mode':'live'})
        now=time.time()-2
        event={'tr_id':131672,'event_time':pd.Timestamp(now,unit='s',tz='UTC').isoformat(),'lon':37.6,'lat':55.7,'speed':10}
        assert client.post('/api/telemetry',json=[event]).status_code==200
        vehicle=next(v for v in client.get('/api/state').json()['vehicles'] if v['tr_id']==131672)
        assert vehicle['live_track'][-1]['lon']==37.6
        metrics=client.get('/api/observability')
        assert metrics.status_code==200
        assert metrics.json()['queues']['telemetry_points_in_memory']>=1
        ready=client.get('/health/ready')
        assert ready.status_code in {200,503}


def test_state_exposes_http_telemetry_provenance():
    with TestClient(backend.app) as client:
        client.post('/api/mode',json={'mode':'live'})
        event={'tr_id':131672,'event_time':pd.Timestamp.now(tz='UTC').isoformat(),'lon':37.6,'lat':55.7,'speed':10}
        assert client.post('/api/telemetry',json=[event]).status_code==200
        vehicle=next(v for v in client.get('/api/state').json()['vehicles'] if v['tr_id']==131672)
        assert vehicle['telemetry_source']=='http_json'


def test_live_track_excludes_positions_older_than_the_track_ttl():
    tr_id=987654
    backend.history[tr_id].clear()
    now=time.time()
    try:
        backend.history[tr_id].append({'event_time':pd.Timestamp(now-7200,unit='s',tz='UTC').isoformat(),'ts':now-7200,'lon':37.6,'lat':55.7,'location_valid':True})
        backend.history[tr_id].append({'event_time':pd.Timestamp(now-10,unit='s',tz='UTC').isoformat(),'ts':now-10,'lon':37.61,'lat':55.71,'location_valid':True})
        track=backend.live_track(tr_id)
        assert len(track)==1
        assert track[0]['lon']==37.61
    finally:
        backend.history.pop(tr_id,None)


def test_replay_telemetry_overlays_the_historical_snapshot_without_switching_modes():
    with TestClient(backend.app) as client:
        client.post('/api/mode',json={'mode':'replay'})
        assert backend.state['mode']=='replay'
        response=client.post('/api/telemetry',json=[{
            'tr_id':131672,'event_time':pd.Timestamp.now(tz='UTC').isoformat(),
            'lon':37.81,'lat':55.75,'speed':28,'location_valid':True,
        }])
        assert response.status_code==200,response.text

        state=client.get('/api/state',params={'dispatcher_id':'dispatcher-01'}).json()
        vehicle=next(item for item in state['vehicles'] if item['tr_id']==131672)
        assert state['state']['mode']=='replay'
        assert vehicle['source']=='historical_v5'
        assert vehicle['live_position'] is True
        assert vehicle['lon']==37.81 and vehicle['lat']==55.75


def test_stale_live_telemetry_does_not_overlay_the_historical_snapshot():
    with TestClient(backend.app) as client:
        backend.vehicles.clear()
        backend.history.clear()
        response=client.post('/api/telemetry',json=[{
            'tr_id':131672,'event_time':(pd.Timestamp.now(tz='UTC')-pd.Timedelta(hours=2)).isoformat(),
            'lon':37.81,'lat':55.75,'speed':28,'location_valid':True,
        }])
        assert response.status_code==200,response.text

        state=client.get('/api/state',params={'dispatcher_id':'dispatcher-01'}).json()
        vehicle=next(item for item in state['vehicles'] if item['tr_id']==131672)
        assert 'live_position' not in vehicle


def test_ml_rejects_nonfinite_and_missing_features():
    with TestClient(ml_app) as client:
        assert client.get('/health').status_code==200
        assert client.post('/predict',json={'rows':[{}]}).status_code==422
        legacy={feature:0 for feature in FEATURES}
        assert client.post('/predict',json={'rows':[legacy]}).status_code==410

@pytest.mark.skipif(not Path(os.getenv('DATA_DIR','dataset')).exists(),reason='Dataset required')
def test_replay_live_and_degradation():
    with TestClient(backend.app) as client:
        original=backend.client
        backend.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=ml_app),base_url='http://ml')
        try:
            assert client.get('/openapi.json').status_code==200
            assert client.get('/').status_code==200
            client.post('/api/mode',json={'mode':'replay'})
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
            saved_live_schedule_day=backend.live_schedule_day
            now=int(time.time())-2
            event=dict(tr_id=1,event_time=pd.Timestamp(now,unit='s',tz='UTC').isoformat(),lon=37.6,lat=55.7,speed=0)
            backend.schedule=pd.DataFrame([
                dict(tt_action_item_id=1,tr_id=1,ts=now-45,time_begin=pd.Timestamp(now-45,unit='s',tz='UTC').isoformat(),lon=37.6,lat=55.7,building_address='Past stop'),
                dict(tt_action_item_id=2,tr_id=1,ts=now+720,time_begin=pd.Timestamp(now+720,unit='s',tz='UTC').isoformat(),lon=37.61,lat=55.71,building_address='Future stop')])
            backend.live_schedule_day=backend.timestamp(event['event_time']).date()
            assert client.post('/api/telemetry',json=[event]).status_code==200
            live=client.get('/api/state').json()['vehicles'][0]
            assert live['source']=='live' and live['features']['cur_dev_s']==45
            assert live['horizon_s']==720 and not live['degraded']
            backend.schedule=saved_schedule
            backend.live_schedule_day=saved_live_schedule_day
            client.post('/api/mode',json={'mode':'replay'})
            async def failed(request):raise httpx.ConnectError('offline')
            backend.client=httpx.AsyncClient(transport=httpx.MockTransport(failed))
            result=client.post('/api/replay/step').json()['prediction']
            assert result['degraded'] and result['late_probability'] is None and result['level']=='unknown'
        finally:backend.client=original
