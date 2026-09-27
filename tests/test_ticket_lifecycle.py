"""In-memory tickets, fifteen-second terminal display and idempotent receipts."""
import asyncio
import copy
import sqlite3
import httpx

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as backend
from emulator import service as emulator


@pytest.fixture
def tickets(tmp_path,monkeypatch):
    monkeypatch.setattr(backend,'DB_PATH',tmp_path/'dispatcher.db')
    monkeypatch.setattr(backend,'db',None)
    monkeypatch.setattr(backend,'lock',asyncio.Lock())
    monkeypatch.setattr(backend,'seed_dispatcher_02_all_routes',False)
    clock=[pd.Timestamp('2026-09-27T12:00:00+03:00')]
    original=backend._moscow_time
    monkeypatch.setattr(backend,'_moscow_time',lambda value=None:clock[0] if value is None else original(value))
    backend.init_store()
    store=backend.db
    vehicle=dict(tr_id=10,source='live',connection_state='live',trip_status='active',on_route=True,
                 level='medium',late_probability=.5,prediction_s=180.,current_deviation_s=150.,
                 horizon_s=720,stale=False,degraded=False,T=clock[0].isoformat())
    monkeypatch.setattr(backend,'action_vehicle',lambda tr:{**vehicle,'tr_id':tr})
    monkeypatch.setattr(backend,'state',{'mode':'live'})
    def queue(dispatcher='dispatcher-01',action='contact',tr_id=10):
        body=backend.DriverCommand(role='dispatcher',dispatcher_id=dispatcher,tr_id=tr_id,
                                   action=action,message='Подтвердите приём и текущую обстановку.')
        return asyncio.run(backend.queue_driver_command(body))
    def observe(level='low',probability=.2,advance=3,**overrides):
        clock[0]+=pd.Timedelta(seconds=advance)
        vehicle.update({'T':clock[0].isoformat(),'level':level,'late_probability':probability,**overrides})
        backend.reconcile_driver_tickets(10)
    yield dict(queue=queue,observe=observe,vehicle=vehicle,clock=clock,store=store)
    backend.db.close()


def current(dispatcher='dispatcher-01',tr_id=10):
    return backend.stored_action_case(dispatcher,tr_id)


def test_sending_receipt_execution_then_live_green_permanently_deletes_ticket(tickets):
    command=tickets['queue']()
    assert current()['status']=='pending'
    assert current()['status_label']=='Отправляем водителю'
    accepted=backend.acknowledge_driver_command(command['id'],demo=True)
    old=copy.deepcopy(current())
    assert accepted['status']==current()['status']=='executing'
    assert current()['delivery_confirmed']
    assert accepted['simulated_response']['projection']['prediction_before_s']==180
    assert accepted['simulated_response']['projection']['prediction_after_s']==180
    assert tickets['vehicle']['prediction_s']==180
    # Better seconds but still yellow: no terminal success and no deletion.
    tickets['observe']('medium',.4,prediction_s=80,current_deviation_s=30)
    assert current()['status']=='executing'
    tickets['observe']()
    assert current()['status']=='completed_success'
    assert current()['status_label']=='Закрыт' and current()['tone']=='success'
    deadline=pd.Timestamp(current()['visible_until'])
    assert deadline-tickets['clock'][0]==pd.Timedelta(seconds=15)
    terminal=backend.stored_driver_command(command['id'])
    assert terminal['status']=='completed'
    assert terminal['completion_evidence']['level']=='low'
    # Neither late telemetry, an old callback, a stale upsert nor restart revives it.
    tickets['observe']('high',.9,prediction_s=400)
    assert not backend.save_action_case(old)
    assert current()['tone']=='success'
    assert pd.Timestamp(current()['visible_until'])==deadline
    tickets['clock'][0]=deadline-pd.Timedelta(milliseconds=1)
    assert backend.purge_expired_action_cases()==0
    assert current()
    tickets['clock'][0]=deadline
    assert backend.purge_expired_action_cases()==1
    assert current() is None
    assert tickets['store'].execute('SELECT COUNT(*) FROM dispatcher_action_cases').fetchone()[0]==0
    assert backend.acknowledge_driver_command(command['id'],demo=True)['status']=='completed'
    assert backend.action_center_items('dispatcher-01')==[]
    tickets['store'].close()
    backend.init_store()
    assert current() is None
    assert backend.stored_driver_command(command['id'])['status']=='completed'


def test_low_without_driver_receipt_does_not_close_ticket(tickets):
    tickets['queue']()
    tickets['observe']()
    assert current()['status']=='pending'
    assert not current()['delivery_confirmed']


def test_receipt_is_idempotent_and_does_not_fake_a_success(tickets):
    command=tickets['queue'](action='accelerate_safely')
    first=backend.acknowledge_driver_command(command['id'],demo=True)
    tickets['clock'][0]+=pd.Timedelta(seconds=1)
    second=backend.acknowledge_driver_command(command['id'],demo=True)
    assert first['acknowledged_at']==second['acknowledged_at']
    assert current()['status']=='executing'
    assert first['simulated_response']['projection']['expected_saved_delay_s']==0


@pytest.mark.parametrize('status',['completed_no_result','worsened'])
def test_legacy_accepted_ticket_migrates_and_closes_only_on_live_green(tickets,status):
    command=tickets['queue']()
    accepted=backend.acknowledge_driver_command(command['id'],demo=True)
    accepted['status']='simulated_completed'
    backend.update_driver_command(accepted)
    case=current()
    case.update(status=status,risk_observed=False)
    case.pop('acknowledged_at')
    backend.save_action_case(case)
    tickets['observe']('medium',.45)
    assert current()['status']=='executing'
    assert current()['acknowledged_at']==accepted['simulated_response']['simulated_at']
    assert backend.stored_driver_command(command['id'])['status']=='executing'
    tickets['observe']()
    assert current()['status']=='completed_success'
    tickets['clock'][0]+=pd.Timedelta(seconds=15)
    backend.purge_expired_action_cases()
    assert current() is None
    assert backend.stored_driver_command(command['id'])['status']=='completed'


@pytest.mark.parametrize('override',[
    {'stale':True},{'degraded':True},{'source':'historical_fallback'},
    {'connection_state':'offline'},{'connection_state':'waiting'},{'connection_state':'paused'},
    {'trip_status':'completed'},{'on_route':False},{'prediction_s':None},
    {'late_probability':None},{'level':'unknown'},{'current_deviation_s':None},
])
def test_unreliable_low_state_never_deletes_ticket(tickets,override):
    command=tickets['queue']()
    backend.acknowledge_driver_command(command['id'])
    tickets['observe'](**override)
    assert current()['status']=='executing'


@pytest.mark.parametrize('seconds',[-1,0])
def test_observation_before_or_at_receipt_is_not_completion(tickets,seconds):
    command=tickets['queue']()
    backend.acknowledge_driver_command(command['id'])
    tickets['vehicle'].update(level='low',late_probability=.2,
        T=(tickets['clock'][0]+pd.Timedelta(seconds=seconds)).isoformat())
    backend.reconcile_driver_tickets(10)
    assert current()['status']=='executing'


def test_future_or_old_forecast_never_closes_ticket(tickets):
    command=tickets['queue']();backend.acknowledge_driver_command(command['id'])
    tickets['vehicle'].update(level='low',late_probability=.2,
        T=(tickets['clock'][0]+pd.Timedelta(seconds=60)).isoformat())
    backend.reconcile_driver_tickets(10)
    assert current()
    tickets['clock'][0]+=pd.Timedelta(seconds=90)
    backend.reconcile_driver_tickets(10)
    assert current()


def test_superseded_receipt_and_old_snapshot_cannot_change_new_attempt(tickets):
    first=tickets['queue']();old=copy.deepcopy(current())
    second=tickets['queue']()
    with pytest.raises(HTTPException) as error:backend.acknowledge_driver_command(first['id'],demo=True)
    assert error.value.status_code==409
    assert not backend.delete_action_case(old)
    assert not backend.save_action_case(old)
    assert not backend.close_driver_case(old,tickets['clock'][0],tickets['vehicle'])
    assert current()['attempt_id']==second['id']
    assert current()['status']=='pending'


def test_green_before_any_risk_is_not_positive_transition(tickets):
    tickets['vehicle'].update(level='low',late_probability=.1,prediction_s=0)
    command=tickets['queue']();backend.acknowledge_driver_command(command['id'])
    tickets['observe']()
    assert current()['status']=='executing'
    tickets['observe']('medium',.5)
    tickets['observe']()
    assert current()['status']=='completed_success'


def test_ticket_owners_are_isolated_and_vehicle_updates_close_all_accepted_tickets(tickets):
    first=tickets['queue']();second=tickets['queue']('dispatcher-02')
    backend.acknowledge_driver_command(first['id'])
    assert {item['attempt_id'] for item in backend.action_center_items('dispatcher-01')}=={first['id']}
    assert {item['attempt_id'] for item in backend.action_center_items('dispatcher-02')}=={second['id']}
    tickets['observe']()
    assert current()['status']=='completed_success'
    tickets['clock'][0]+=pd.Timedelta(seconds=15)
    backend.purge_expired_action_cases()
    assert current() is None
    assert current('dispatcher-02')['status']=='pending'


def test_closure_is_atomic_when_audit_write_fails(tickets):
    command=tickets['queue']();backend.acknowledge_driver_command(command['id'])
    tickets['store'].executescript("""CREATE TRIGGER fail_audit_update BEFORE UPDATE ON driver_commands
        BEGIN SELECT RAISE(ABORT, 'test failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError):
        backend.close_driver_case(current(),tickets['clock'][0],tickets['vehicle'])
    assert current()
    assert current()['status']=='executing'
    assert backend.stored_driver_command(command['id'])['status']=='executing'


def test_expired_success_ticket_is_physically_purged_without_deleting_journal(tickets):
    command=tickets['queue']();case=current()
    case.update(status='completed_success',tone='success',visible_until=tickets['clock'][0].isoformat())
    backend.save_action_case(case)
    tickets['store'].close();backend.init_store()
    assert current() is None
    assert backend.stored_driver_command(command['id']) is not None


def test_acknowledgement_api_and_repeated_demo_are_receipts_only(tickets):
    command=tickets['queue']()
    client=TestClient(backend.app)  # no lifespan: use this fixture's isolated DB
    response=client.post(f"/api/driver-commands/{command['id']}/acknowledge")
    assert response.status_code==200 and response.json()['status']=='executing'
    assert client.post(f"/api/driver-commands/{command['id']}/simulate").json()['status']=='executing'
    summary=client.get('/api/action-center?dispatcher_id=dispatcher-01').json()['summary']
    assert summary['executing']==1 and summary['success']==0


def test_restart_clears_all_tickets_but_keeps_accounts_assignments_and_audit(tickets):
    command=tickets['queue']()
    store=tickets['store']
    store.execute('INSERT INTO assignments(dispatcher_id,tr_id) VALUES(?,?)',('dispatcher-01',10));store.commit()
    assert store.execute("SELECT 1 FROM sqlite_temp_master WHERE name='dispatcher_action_cases'").fetchone()
    assert store.execute('PRAGMA temp_store').fetchone()[0]==2
    assert not store.execute("SELECT 1 FROM main.sqlite_master WHERE name='dispatcher_action_cases'").fetchone()
    with sqlite3.connect(backend.DB_PATH) as disk:
        assert not disk.execute("SELECT 1 FROM sqlite_master WHERE name='dispatcher_action_cases'").fetchone()
    store.close();backend.init_store()
    assert current() is None
    assert backend.get_dispatcher('dispatcher-01')['assigned_tr_ids']==[10]
    assert backend.stored_driver_command(command['id'])
    with pytest.raises(HTTPException) as error:backend.acknowledge_driver_command(command['id'])
    assert error.value.status_code==409


def test_startup_removes_legacy_disk_ticket_table_without_touching_audit(tickets):
    command=tickets['queue']()
    tickets['store'].execute('CREATE TABLE main.dispatcher_action_cases(id TEXT,payload TEXT)')
    tickets['store'].execute('INSERT INTO main.dispatcher_action_cases VALUES(?,?)',('legacy','{}'))
    tickets['store'].commit();tickets['store'].close()
    backend.init_store()
    assert current() is None
    assert not backend.db.execute("SELECT 1 FROM main.sqlite_master WHERE name='dispatcher_action_cases'").fetchone()
    assert backend.stored_driver_command(command['id'])


@pytest.mark.parametrize(('before','expected'),[(0,30),(30,45),(60,90),(100,130),(130,130)])
def test_demo_really_accelerates_only_selected_custom_ts_once(tickets,monkeypatch,before,expected):
    tr_id=backend.CUSTOM_TR_ID_OFFSET+10;other=tr_id+1
    monkeypatch.setattr(emulator,'runtime',{**emulator.runtime,'paused':False,'connected':True})
    monkeypatch.setattr(emulator,'custom_vehicle_ids',{tr_id,other})
    monkeypatch.setattr(emulator,'debug_speed_overrides',{tr_id:before,other:20.})
    monkeypatch.setattr(backend,'history',{})
    tickets['vehicle']['features']={'speed_last':before}
    command=tickets['queue'](tr_id=tr_id)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=emulator.app),base_url='http://custom') as client:
            monkeypatch.setattr(backend,'client',client)
            first=await backend.simulate_driver_command(command['id'])
            second=await backend.simulate_driver_command(command['id'])
            assert first==second
            assert first['status']=='executing'
            assert first['simulated_response']['debug_speed']['speed_kmh']==expected
            assert current(tr_id=tr_id)['tone']=='executing'
    asyncio.run(run())
    assert emulator.debug_speed_overrides=={tr_id:expected,other:20.}
    assert tickets['vehicle']['prediction_s']==180
    route={'tr_id':tr_id,'base_tr_id':10,'path':[(37.,55.),(37.01,55.)],'timings':[0.,600.]}
    motion=emulator.initial_vehicle_state(route,chaos_enabled=True)
    motion.update(chaos_phase='stop',chaos_pace=0.,chaos_remaining_s=100.)
    lon,lat,speed=emulator.advance_vehicle(route,motion,elapsed_s=3)
    assert speed==expected
    assert emulator.haversine_m(37.,55.,lon,lat)==pytest.approx(expected/3.6*3,abs=1e-5)


@pytest.mark.parametrize(('failure','expected_status'),[('offline',503),('paused',409),('speed_write',503)])
def test_demo_failure_does_not_acknowledge_or_close_ticket(tickets,monkeypatch,failure,expected_status):
    tr_id=backend.CUSTOM_TR_ID_OFFSET+10
    command=tickets['queue'](tr_id=tr_id)
    async def fake_call(path,method='GET',payload=None):
        if failure=='offline' or method=='POST':return {'status':'unavailable'}
        return {'paused':failure=='paused','connected':True}
    monkeypatch.setattr(backend,'custom_emulator_call',fake_call)
    with pytest.raises(HTTPException) as error:asyncio.run(backend.simulate_driver_command(command['id']))
    assert error.value.status_code==expected_status
    assert current(tr_id=tr_id)['status']=='pending'
    assert backend.stored_driver_command(command['id'])['status']=='queued_for_integration'


def test_original_demo_does_not_touch_custom_debug_speed(tickets,monkeypatch):
    command=tickets['queue']()
    async def forbidden(*args,**kwargs):raise AssertionError('Original TS must not change custom emulator')
    monkeypatch.setattr(backend,'custom_emulator_call',forbidden)
    accepted=asyncio.run(backend.simulate_driver_command(command['id']))
    assert accepted['status']=='executing'
    assert 'debug_speed' not in accepted['simulated_response']
    assert 'оригинального' in accepted['simulated_response']['note']


def test_closed_ticket_is_swept_without_browser_or_new_telemetry(tickets):
    command=tickets['queue']();backend.acknowledge_driver_command(command['id'])
    tickets['observe']()
    tickets['clock'][0]+=pd.Timedelta(seconds=15)
    async def run():
        maintenance=asyncio.create_task(backend.maintain_action_cases())
        try:
            await asyncio.sleep(0)
            assert current() is None
        finally:
            maintenance.cancel()
            with pytest.raises(asyncio.CancelledError):await maintenance
    asyncio.run(run())


def test_two_parallel_demo_requests_apply_speed_only_once(tickets,monkeypatch):
    tr_id=backend.CUSTOM_TR_ID_OFFSET+10
    command=tickets['queue'](tr_id=tr_id)
    calls=[]
    async def source(path,method='GET',payload=None):return {'paused':False,'connected':True}
    async def speed(body):
        calls.append(body.speed_kmh)
        await asyncio.sleep(0)
        return {'tr_id':body.tr_id,'speed_kmh':body.speed_kmh,'debug':True}
    monkeypatch.setattr(backend,'custom_emulator_call',source)
    monkeypatch.setattr(backend,'apply_debug_speed',speed)
    async def run():
        results=await asyncio.gather(backend.simulate_driver_command(command['id']),backend.simulate_driver_command(command['id']))
        assert results[0]==results[1]
    asyncio.run(run())
    assert calls==[30.]
    assert current(tr_id=tr_id)['status']=='executing'


def test_new_attempt_replaces_green_ticket_without_old_expiry_deleting_it(tickets):
    command=tickets['queue']();backend.acknowledge_driver_command(command['id'])
    tickets['observe']()
    old=copy.deepcopy(current())
    replacement=tickets['queue']()
    tickets['clock'][0]+=pd.Timedelta(seconds=15)
    backend.purge_expired_action_cases()
    assert not backend.delete_action_case(old)
    assert current()['attempt_id']==replacement['id']
    assert current()['status']=='pending'


def test_old_reserve_outbox_does_not_block_new_session_but_active_ticket_does(tickets,monkeypatch):
    monkeypatch.setattr(backend,'reserve_placement',lambda vehicle,policy: {'before_prediction_s':180})
    monkeypatch.setattr(backend,'reserve_release_decision',lambda vehicle,placement: {'allowed':True})
    body=backend.ReserveDispatch(dispatcher_id='dispatcher-01',tr_id=10)
    first=asyncio.run(backend.release_reserve(body))
    with pytest.raises(HTTPException) as error:asyncio.run(backend.release_reserve(body))
    assert error.value.status_code==409
    tickets['store'].close();backend.init_store()
    assert current() is None
    second=asyncio.run(backend.release_reserve(body))
    assert current()['attempt_id']==second['id'] and second['id']!=first['id']
