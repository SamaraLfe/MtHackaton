"""Dispatcher backend: NDTP/JSON -> causal features -> independent ML service."""
import asyncio, json, logging, math, os, sqlite3, time, uuid, urllib.parse
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from ml.features import build_one, epoch, timestamp, load_traffic, load_schedule, haversine
from backend.ndtp import handle
from backend.api_docs import CONTACT, DESCRIPTION, LICENSE, OPENAPI_EXAMPLES, SERVERS, TAGS, operation

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.getenv('DATA_DIR','dataset'));ARTIFACT=Path(os.getenv('ARTIFACT_DIR','artifacts'))
ML_URL=os.getenv('ML_URL','http://127.0.0.1:8001')
CUSTOM_EMULATOR_URL=os.getenv('CUSTOM_EMULATOR_URL','http://127.0.0.1:18081').rstrip('/')
OFFICIAL_EMULATOR_URL=os.getenv('OFFICIAL_EMULATOR_URL','http://127.0.0.1:18080').rstrip('/')
LIVE_TRACK_TTL_S=int(os.getenv('LIVE_TRACK_TTL_S','3600'))
CUSTOM_TR_ID_OFFSET = int(
    os.getenv(
        'CUSTOM_TR_ID_OFFSET',
        '1000000'
    )
)

CUSTOM_UNIT_ID_OFFSET = int(
    os.getenv(
        'CUSTOM_UNIT_ID_OFFSET',
        '100000000'
    )
)
LIVE_STALE_S = int(
    os.getenv(
        'LIVE_STALE_S',
        '15'
    )
)

LIVE_FALLBACK_S = int(
    os.getenv(
        'LIVE_FALLBACK_S',
        '60'
    )
)
history=defaultdict(lambda:deque(maxlen=1000));vehicles={};archive_vehicles={};counters=defaultdict(int)
last_telemetry_received_at=0.0
deviations={};position_offsets={};position_states={};last_forecast={};state={'mode':'replay','clock':None,'index':0,'snapshot':False,'ingest_paused':False}
schedule=None;schedule_template=None;live_schedule_day=None;traffic=None;points=None;mapping={};client=None;lock=asyncio.Lock();model_meta={}
DB_PATH=Path(os.getenv('STATE_DIR','state'))/'dispatcher.db';db=None
seed_dispatcher_02_all_routes=False
OFFICIAL_CONFIG_PATH=Path(os.getenv('STATE_DIR','state'))/'official_emulator_config.json'
simulation_runs={};simulation_tasks={}
official_config_cache=None
runtime_stats={'started_at':time.time(),'requests_total':0,'requests_errors':0,'recent_request_ms':deque(maxlen=500)}
logger=logging.getLogger('takt.backend')

class Telemetry(BaseModel):
    """One HTTP telemetry event for a vehicle registered in the plan."""
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    tr_id:int=Field(gt=0,description='Идентификатор ТС в расписании, не NDTP unit_id.',examples=[131672])
    event_time:str=Field(description='Время события; ISO 8601 с поясом предпочтителен, время без пояса считается UTC.',examples=['2026-01-06T03:34:50Z'])
    lon:float=Field(ge=-180,le=180,description='Долгота в градусах.',examples=[37.60])
    lat:float=Field(ge=-90,le=90,description='Широта в градусах.',examples=[55.75])
    speed:float=Field(ge=0,le=130,description='Скорость в км/ч.',examples=[25])
    location_valid:bool=Field(default=True,description='Можно ли использовать GPS-наблюдение для положения и признаков.')

class Point(BaseModel):
    """Manual forecast point tied to one scheduled stop in the 10–15 minute window."""
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    tr_id:int=Field(gt=0,description='ТС, чья принятая история используется для расчёта.',examples=[131672])
    T:str=Field(description='Момент расчёта; телеметрия позднее T не используется.',examples=['2026-01-06T03:35:00Z'])
    target_stop_id:int=Field(description='Первая плановая остановка в окне (T+10, T+15] минут.',examples=[53700172828])
    target_time_begin:str=Field(description='Плановое время целевой остановки; совпадает с активным расписанием.',examples=['2026-01-06T03:50:00Z'])
    cur_dev_s:float=Field(description='Текущее отклонение в секундах: плюс — опоздание.',examples=[274])

class Mode(BaseModel):
    """Shared source mode for the whole backend process."""
    mode:str=Field(pattern='^(live|replay)$',description='live принимает поток; replay восстанавливает архивный снимок и исторический прогон.',examples=['replay'])

class WhatIf(BaseModel):
    """Non-persistent risk scenario inputs."""
    tr_id:int|None=Field(default=None,gt=0,description='ТС/линия, для которой проверяется выпуск резерва.',examples=[131672])
    extra_vehicles:int=Field(default=0,ge=0,le=20,description='Дополнительные ТС только для эвристического расчёта.',examples=[1])
    headway_reduction_pct:float=Field(default=0,ge=0,le=50,description='Сокращение интервала в процентах.',examples=[0])

class MapMatch(BaseModel):
    """Position that should be matched to a planned stop."""
    tr_id:int=Field(gt=0,description='ТС, среди плановых остановок которого выполняется поиск.',examples=[131672])
    lon:float=Field(ge=-180,le=180,description='Долгота в градусах.',examples=[37.60])
    lat:float=Field(ge=-90,le=90,description='Широта в градусах.',examples=[55.75])

class AdminSimulation(BaseModel):
    """Legacy synchronous simulation body kept for compatibility."""
    role:str=Field(description='Должно быть literal admin; это не проверка учётной записи.',examples=['admin'])
    tr_id:int=Field(gt=0,description='ТС с координатами в состоянии или расписании.',examples=[131672])
    scenario:str=Field(default='slow',pattern='^(normal|slow|stop)$',description='normal=35, slow=8, stop=0 км/ч.')
    count:int=Field(default=3,ge=1,le=20,description='Число синтетических событий.')
    interval_s:int=Field(default=30,ge=5,le=300,description='Интервал между событиями, секунды.')

class DriverCommand(BaseModel):
    """Auditable driver instruction evaluated against current vehicle evidence."""
    role:str=Field(pattern='^(dispatcher|admin)$',description='Заявленная роль автора записи.',examples=['dispatcher'])
    dispatcher_id:str=Field(pattern='^dispatcher-[a-z0-9]{2,40}$',description='Существующий локальный профиль диспетчера.',examples=['dispatcher-01'])
    tr_id:int=Field(gt=0,description='Получатель указания.',examples=[131672])
    action:str=Field(pattern='^(contact|maintain|accelerate_safely|slow_down_safely)$',description='contact, maintain, accelerate_safely или slow_down_safely.',examples=['contact'])
    message:str=Field(min_length=5,max_length=300,description='Текст локального указания.',examples=['Уточните причину задержки и текущую обстановку.'])

class ReserveDispatch(BaseModel):
    """Request to register a reserve release after data-driven validation."""
    dispatcher_id:str=Field(pattern='^(admin|dispatcher)-[a-z0-9-]{2,40}$',description='Профиль оператора, подтверждающий выпуск резерва.',examples=['dispatcher-02'])
    tr_id:int=Field(gt=0,description='Основное ТС/линия, для которой выпускается резерв.',examples=[131672])

class DispatcherCreate(BaseModel):
    """New local dispatcher profile persisted in SQLite."""
    name:str=Field(min_length=3,max_length=120,description='Отображаемое имя профиля.',examples=['Диспетчер №03'])
    login:str=Field(pattern='^[a-z0-9][a-z0-9_-]{2,40}$',description='Уникальный локальный логин без пароля.',examples=['dispatcher03'])
    role:str=Field(default='Маршрутный диспетчер',min_length=3,max_length=80,description='Название роли для интерфейса.',examples=['Маршрутный диспетчер'])

class AssignmentUpdate(BaseModel):
    """Complete replacement for one profile's assigned vehicles."""
    tr_ids:list[int]=Field(min_length=0,max_length=100,description='Полный список назначенных tr_id; пустой список снимает назначения.',examples=[[131672]])

class SimulationCreate(BaseModel):
    """Asynchronous simulation run owned by a local dispatcher profile."""
    dispatcher_id:str=Field(pattern='^(admin|dispatcher)-\\d{2}$',description='Профиль оператора; запуск разрешён только роли «Администратор».',examples=['admin-01'])
    tr_id:int=Field(gt=0,description='ТС для синтетического потока.',examples=[131672])
    scenario:str=Field(default='slow',pattern='^(normal|slow|stop)$',description='normal=35, slow=8, stop=0 км/ч.')
    count:int=Field(default=5,ge=1,le=20,description='Число событий в запуске.')
    interval_s:int=Field(default=30,ge=5,le=300,description='Интервал событий, секунды.')

class SimulationCancel(BaseModel):
    """Cancellation request for one running local simulation."""
    dispatcher_id:str=Field(pattern='^(admin|dispatcher)-\\d{2}$',description='Профиль оператора; отмена доступна только администратору.',examples=['admin-01'])

class EmulatorControl(BaseModel):
    """Local prototype operator allowed to pause a telemetry source."""
    dispatcher_id:str=Field(pattern='^(admin|dispatcher)-[a-z0-9-]{2,40}$',description='Существующий рабочий профиль, управляющий источниками эмуляции.',examples=['dispatcher-01'])

class DebugSpeedOverride(EmulatorControl):
    """Ephemeral debug-only speed overlay for a custom-emulator vehicle."""
    tr_id:int=Field(gt=0,description='ТС собственного эмулятора.')
    speed_kmh:float=Field(ge=0,le=130,description='Искусственная скорость, км/ч.')

class AdminAction(BaseModel):
    """Administrator profile authorizing a destructive local account action."""
    operator_id:str=Field(pattern='^admin-[a-z0-9-]{2,40}$',description='Существующий профиль администратора.',examples=['admin-01'])

def _account(row):
    return {'id':row['id'],'name':row['name'],'login':row['login'],'role':row['role'],'status':row['status']}

def init_store():
    global db, seed_dispatcher_02_all_routes
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DB_PATH,check_same_thread=False);db.row_factory=sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS dispatchers (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, login TEXT NOT NULL UNIQUE,
          role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS assignments (
          dispatcher_id TEXT NOT NULL, tr_id INTEGER NOT NULL,
          PRIMARY KEY(dispatcher_id,tr_id), FOREIGN KEY(dispatcher_id) REFERENCES dispatchers(id));
        CREATE TABLE IF NOT EXISTS simulations (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS driver_commands (
          id TEXT PRIMARY KEY, tr_id INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reserve_actions (
          id TEXT PRIMARY KEY, tr_id INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dispatcher_action_cases (
          id TEXT PRIMARY KEY, dispatcher_id TEXT NOT NULL, tr_id INTEGER NOT NULL,
          payload TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(dispatcher_id,tr_id));
        CREATE TABLE IF NOT EXISTS app_meta (
          key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    seeds=[('admin-01','Администратор','admin','Администратор'),('dispatcher-01','Диспетчер №01','dispatcher01','Маршрутный диспетчер'),('dispatcher-02','Диспетчер №2 (все ТС)','dispatcher02','Старший диспетчер')]
    for account in seeds:
        db.execute('INSERT OR IGNORE INTO dispatchers(id,name,login,role,status,created_at) VALUES(?,?,?,?,?,?)',(*account,'active',pd.Timestamp.now(tz='Europe/Moscow').isoformat()))
    # Rename the built-in profile in existing state volumes without touching
    # names of user-created accounts.
    db.execute('UPDATE dispatchers SET name=?, role=? WHERE id=?',
               ('Диспетчер №2 (все ТС)','Старший диспетчер','dispatcher-02'))
    seed_dispatcher_02_all_routes=db.execute(
        'SELECT 1 FROM app_meta WHERE key=?',('dispatcher_02_all_routes_v1',)
    ).fetchone() is None
    db.commit()

def get_dispatcher(dispatcher_id):
    row=db.execute('SELECT * FROM dispatchers WHERE id=?',(dispatcher_id,)).fetchone()
    if row is None:return None
    result=_account(row)
    result['assigned_tr_ids']=[item['tr_id'] for item in db.execute('SELECT tr_id FROM assignments WHERE dispatcher_id=? ORDER BY tr_id',(dispatcher_id,))]
    return result

def list_dispatchers():
    return [get_dispatcher(row['id']) for row in db.execute('SELECT id FROM dispatchers WHERE status="active" ORDER BY role DESC,name')]

def save_simulation(run):
    db.execute('INSERT OR REPLACE INTO simulations(id,payload,created_at) VALUES(?,?,?)',(run['id'],json.dumps(clean(run),ensure_ascii=False),run['created_at']))
    db.commit()

def stored_simulations(status=None,tr_id=None,limit=50):
    """Read scenario history, preferring fresher in-process lifecycle state."""
    rows=db.execute('SELECT payload FROM simulations ORDER BY created_at DESC').fetchall()
    by_id={item['id']:item for item in (json.loads(row['payload']) for row in rows)}
    # A background run can reach a terminal state immediately before its latest
    # SQLite snapshot becomes visible to a concurrent list request.  The run
    # registry is authoritative while this backend process owns the task.
    for run_id,run in simulation_runs.items():
        by_id[run_id]=clean(run)
    items=sorted(by_id.values(),key=lambda item:item.get('created_at',''),reverse=True)
    if status: items=[item for item in items if item.get('status')==status]
    if tr_id is not None: items=[item for item in items if item.get('tr_id')==tr_id]
    return items[:limit]

def save_driver_command(command):
    """Persist an auditable local outbox command before returning it to the UI."""
    db.execute('INSERT INTO driver_commands(id,tr_id,payload,created_at) VALUES(?,?,?,?)',
               (command['id'],command['tr_id'],json.dumps(clean(command),ensure_ascii=False),command['created_at']))
    db.commit()

def update_driver_command(command):
    """Update one local command while preserving its immutable command id."""
    db.execute('UPDATE driver_commands SET payload=? WHERE id=?',
               (json.dumps(clean(command),ensure_ascii=False),command['id']))
    db.commit()

def stored_driver_commands(tr_id=None,limit=200):
    """Return durable outbox records newest first; no delivery is implied."""
    query='SELECT payload FROM driver_commands'
    params=[]
    if tr_id is not None:
        query+=' WHERE tr_id=?';params.append(tr_id)
    query+=' ORDER BY created_at DESC LIMIT ?';params.append(limit)
    return [json.loads(row['payload']) for row in db.execute(query,params)]

def stored_driver_command(command_id):
    """Read one command by id for idempotent simulator callbacks."""
    row=db.execute('SELECT payload FROM driver_commands WHERE id=?',(command_id,)).fetchone()
    return json.loads(row['payload']) if row else None

def expire_stale_driver_commands(now=None,ttl_s=900):
    """Close local integration records that could not be delivered in time."""
    now=pd.Timestamp.now(tz='Europe/Moscow') if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now=now.tz_localize('Europe/Moscow')
    expired=0
    for command in stored_driver_commands(limit=2000):
        if command.get('status')!='queued_for_integration':
            continue
        try:
            created=pd.Timestamp(command.get('created_at'))
            if created.tzinfo is None:
                created=created.tz_localize('Europe/Moscow')
        except (TypeError,ValueError):
            continue
        if (now-created).total_seconds()<=ttl_s:
            continue
        command.update(
            status='integration_timeout',simulation_available=False,
            terminal_reason='Внешний канал не подтвердил доставку в течение 15 минут.',
            resolved_at=now.isoformat(),
        )
        update_driver_command(command)
        update_action_case_from_delivery(command,'driver_command',now=now)
        expired+=1
    return expired

def save_reserve_action(action):
    """Persist a validated reserve release in the local integration outbox."""
    db.execute('INSERT INTO reserve_actions(id,tr_id,payload,created_at) VALUES(?,?,?,?)',
               (action['id'],action['tr_id'],json.dumps(clean(action),ensure_ascii=False),action['created_at']))
    db.commit()

def update_reserve_action(action):
    """Update a reserve request without changing its audit identity."""
    db.execute('UPDATE reserve_actions SET payload=? WHERE id=?',
               (json.dumps(clean(action),ensure_ascii=False),action['id']))
    db.commit()

def stored_reserve_actions(tr_id=None,limit=100):
    """Return newest reserve decisions with their evidence and lifecycle state."""
    query='SELECT payload FROM reserve_actions'
    params=[]
    if tr_id is not None:
        query+=' WHERE tr_id=?';params.append(tr_id)
    query+=' ORDER BY created_at DESC LIMIT ?';params.append(limit)
    return [json.loads(row['payload']) for row in db.execute(query,params)]

def expire_stale_reserve_actions(now=None,ttl_s=900):
    """Close reserve requests that the external fleet did not acknowledge."""
    now=pd.Timestamp.now(tz='Europe/Moscow') if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now=now.tz_localize('Europe/Moscow')
    expired=0
    for action in stored_reserve_actions(limit=2000):
        if action.get('status')!='queued_for_integration':
            continue
        try:
            created=pd.Timestamp(action.get('created_at'))
            if created.tzinfo is None:
                created=created.tz_localize('Europe/Moscow')
        except (TypeError,ValueError):
            continue
        if (now-created).total_seconds()<=ttl_s:
            continue
        action.update(
            status='integration_timeout',external_execution=False,
            terminal_reason='Внешний флот не подтвердил выпуск резерва в течение 15 минут.',
            resolved_at=now.isoformat(),
        )
        update_reserve_action(action)
        update_action_case_from_delivery(action,'reserve_release',now=now)
        expired+=1
    return expired

ACTION_CASE_STATUSES={
    'pending':('Ожидает выполнения','pending'),
    'completed_success':('Выполнено успешно','success'),
    'completed_no_result':('Выполнено без результата','no_result'),
    'not_delivered':('Не дошло до водителя','not_delivered'),
    'worsened':('Ситуация ухудшилась','worsened'),
    'improved_independently':('Ситуация улучшилась','success'),
}
ACTION_RESULT_THRESHOLD_S=15.0
ACTION_SUCCESS_VISIBLE_S=3

def _moscow_time(value=None):
    stamp=pd.Timestamp.now(tz='Europe/Moscow') if value is None else pd.Timestamp(value)
    return stamp.tz_localize('Europe/Moscow') if stamp.tzinfo is None else stamp.tz_convert('Europe/Moscow')

def _case_dispatcher(record):
    dispatcher=record.get('dispatcher') or {}
    return dispatcher.get('id') or record.get('dispatcher_id')

def stored_action_case(dispatcher_id,tr_id):
    row=db.execute(
        'SELECT payload FROM dispatcher_action_cases WHERE dispatcher_id=? AND tr_id=?',
        (dispatcher_id,int(tr_id)),
    ).fetchone()
    return json.loads(row['payload']) if row else None

def stored_action_cases(dispatcher_id,limit=200):
    rows=db.execute(
        'SELECT payload FROM dispatcher_action_cases WHERE dispatcher_id=? ORDER BY updated_at DESC LIMIT ?',
        (dispatcher_id,int(limit)),
    ).fetchall()
    return [json.loads(row['payload']) for row in rows]

def save_action_case(case):
    """Upsert one stable dispatcher+vehicle case without changing its identity."""
    payload=json.dumps(clean(case),ensure_ascii=False)
    db.execute('''
        INSERT INTO dispatcher_action_cases(id,dispatcher_id,tr_id,payload,updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(dispatcher_id,tr_id) DO UPDATE SET
          payload=excluded.payload, updated_at=excluded.updated_at
    ''',(case['id'],case['dispatcher_id'],int(case['tr_id']),payload,case['updated_at']))
    db.commit()

def register_action_case(record,kind,now=None):
    """Start or replace the current attempt in the same dispatcher+vehicle case."""
    dispatcher_id=_case_dispatcher(record)
    if not dispatcher_id:
        return None
    now=_moscow_time(now or record.get('created_at'))
    previous=stored_action_case(dispatcher_id,record['tr_id'])
    decision=record.get('decision') or {}
    baseline=(record.get('placement') or {}).get('before_prediction_s')
    if baseline is None:
        baseline=(decision.get('evidence') or {}).get('prediction_s')
    if baseline is None:
        vehicle=action_vehicle(record['tr_id'])
        baseline=vehicle.get('prediction_s') if vehicle else None
    status_label,tone=ACTION_CASE_STATUSES['pending']
    case={
        'id':previous['id'] if previous else f"case-{dispatcher_id}-{int(record['tr_id'])}",
        'dispatcher_id':dispatcher_id,'dispatcher':record.get('dispatcher') or {'id':dispatcher_id},
        'tr_id':int(record['tr_id']),'revision':int((previous or {}).get('revision') or 0)+1,
        'attempt_id':record['id'],'kind':kind,'action':record.get('action'),
        'action_title':record.get('action_title') or ('Выпуск резервного ТС' if kind=='reserve_release' else 'Оперативное действие'),
        'status':'pending','status_label':status_label,'tone':tone,
        'result_detail':'Действие зарегистрировано. Ожидаем подтверждение исполнения и новый результат по ТС.',
        'created_at':(previous or {}).get('created_at') or now.isoformat(),
        'action_started_at':now.isoformat(),'updated_at':now.isoformat(),'visible_until':None,
        'delivery_confirmed':False,'outcome_source':'outbox',
        'baseline_prediction_s':baseline,'latest_prediction_s':baseline,
        'last_observation_at':now.isoformat(),
    }
    save_action_case(case)
    return case

def classify_action_outcome(before,after,delivered=True):
    """Classify useful change by distance from the timetable, not delay sign."""
    if before is None or after is None:
        return 'completed_no_result' if delivered else 'not_delivered'
    improvement=abs(float(before))-abs(float(after))
    if improvement>=ACTION_RESULT_THRESHOLD_S:
        return 'completed_success' if delivered else 'improved_independently'
    if improvement<=-ACTION_RESULT_THRESHOLD_S and delivered:
        return 'worsened'
    return 'completed_no_result' if delivered else 'not_delivered'

def apply_action_case_outcome(case,status,detail,now=None,after=None,source='telemetry'):
    now=_moscow_time(now)
    label,tone=ACTION_CASE_STATUSES[status]
    changed=case.get('status')!=status
    case.update(
        status=status,status_label=label,tone=tone,result_detail=detail,
        updated_at=now.isoformat(),outcome_source=source,
        latest_prediction_s=after if after is not None else case.get('latest_prediction_s'),
    )
    if tone=='success':
        # Start the three-second acknowledgement only on a real transition;
        # repeated polling must not keep a green card alive indefinitely.
        if changed or not case.get('visible_until'):
            case['visible_until']=(now+pd.to_timedelta(ACTION_SUCCESS_VISIBLE_S,unit='s')).isoformat()
    else:
        case['visible_until']=None
    save_action_case(case)
    return case

def update_action_case_from_delivery(record,kind,now=None):
    """Apply a delivery/simulator lifecycle result to the owner's stable case."""
    dispatcher_id=_case_dispatcher(record)
    if not dispatcher_id:
        return None
    case=stored_action_case(dispatcher_id,record['tr_id'])
    if not case or case.get('attempt_id')!=record.get('id'):
        return case
    now=_moscow_time(now or record.get('resolved_at'))
    if record.get('status')=='integration_timeout':
        target='Не получено подтверждение внешнего канала. Действие не считается доставленным или выполненным.'
        return apply_action_case_outcome(case,'not_delivered',target,now,source='integration_timeout')
    if record.get('status')!='simulated_completed':
        return case
    response=record.get('simulated_response') or {}
    projection=response.get('projection') or {}
    before=projection.get('prediction_before_s',case.get('baseline_prediction_s'))
    after=projection.get('prediction_after_s')
    status=classify_action_outcome(before,after,delivered=True)
    details={
        'completed_success':'Действие подтверждено: ожидаемое отклонение от графика уменьшилось.',
        'completed_no_result':'Действие выполнено, но заметного улучшения отклонения пока нет.',
        'worsened':'После выполнения отклонение от графика увеличилось. Нужна новая мера.',
    }
    case['delivery_confirmed']=True
    case['last_observation_at']=now.isoformat()
    return apply_action_case_outcome(case,status,details[status],now,after,source=response.get('mode') or 'local_simulator')

def reconcile_action_case(case,now=None):
    """Let a newer vehicle forecast improve or worsen a persistent result."""
    now=_moscow_time(now)
    vehicle=action_vehicle(case['tr_id'])
    if not vehicle or vehicle.get('prediction_s') is None or vehicle.get('stale'):
        return case
    observed_at=vehicle.get('T') or vehicle.get('position_time')
    if not observed_at:
        return case
    try:
        observed=_moscow_time(observed_at)
        previous=_moscow_time(case.get('last_observation_at') or case.get('action_started_at'))
    except (TypeError,ValueError):
        return case
    if observed<=previous:
        return case
    before=case.get('baseline_prediction_s')
    after=vehicle.get('prediction_s')
    status=classify_action_outcome(before,after,bool(case.get('delivery_confirmed')))
    # A queued action remains pending until delivery is confirmed or expires;
    # merely seeing a new forecast does not prove that it was executed.
    if case.get('status')=='pending' and not case.get('delivery_confirmed'):
        case['last_observation_at']=observed.isoformat()
        case['latest_prediction_s']=after
        save_action_case(case)
        return case
    case['last_observation_at']=observed.isoformat()
    details={
        'completed_success':'Новая телеметрия подтверждает улучшение ситуации по ТС.',
        'improved_independently':'Ситуация по ТС улучшилась, хотя доставка действия не была подтверждена.',
        'completed_no_result':'По новой телеметрии заметного улучшения пока нет.',
        'not_delivered':'Доставка не подтверждена; заметного улучшения ситуации по ТС пока нет.',
        'worsened':'Новая телеметрия показывает увеличение отклонения от графика. Нужна новая мера.',
    }
    return apply_action_case_outcome(case,status,details[status],observed,after,source='vehicle_telemetry')

def action_center_items(dispatcher_id,now=None):
    """Return only the current dispatcher's stable per-vehicle action cases."""
    now=pd.Timestamp.now(tz='Europe/Moscow') if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now=now.tz_localize('Europe/Moscow')
    profile=get_dispatcher(dispatcher_id)
    if profile is None:
        raise HTTPException(404,'Dispatcher not found')
    items=[]
    for original in stored_action_cases(dispatcher_id):
        case=reconcile_action_case(dict(original),now)
        if case.get('tone')=='success' and case.get('visible_until'):
            try:
                if _moscow_time(case['visible_until'])<=now:
                    continue
            except (TypeError,ValueError):
                continue
        items.append(clean(case))
    tone_rank={'worsened':0,'no_result':1,'not_delivered':2,'pending':3,'success':4}
    items.sort(key=lambda item:(tone_rank.get(item.get('tone'),9),item.get('updated_at','')))
    return items

def live_track(tr_id,limit=100):
    """Return recent valid GPS points, preserving simulated-point provenance."""
    rows=[row for row in history[tr_id] if row.get('location_valid') and np.isfinite(row.get('lon',np.nan)) and np.isfinite(row.get('lat',np.nan))]
    if rows:
        cutoff=max(float(row['ts']) for row in rows)-LIVE_TRACK_TTL_S
        rows=[row for row in rows if float(row['ts'])>=cutoff]
    track=[]
    for row in rows[-limit:]:
        item={'lon':row['lon'],'lat':row['lat'],'event_time':timestamp(row['event_time']).isoformat(),
              'simulated':bool(row.get('simulated',False))}
        # The original image's raw Nav00 coordinate is retained for audit,
        # while the public map coordinate remains the route projection.
        if row.get('raw_lon') is not None and row.get('raw_lat') is not None:
            item['raw_lon']=row['raw_lon']; item['raw_lat']=row['raw_lat']
        track.append(clean(item))
    return track

def align_schedule_to_event_day(frame, event_time):
    """Move a historical day-plan to the local calendar day of live telemetry."""
    if frame.empty:return frame.copy()
    result=frame.copy()
    event_local=timestamp(event_time)
    schedule_start=pd.to_datetime(result['ts'].min(), unit='s', utc=True).tz_convert('Europe/Moscow').normalize()
    shift=event_local.normalize().timestamp()-schedule_start.timestamp()
    result['ts']=result['ts'].astype(float)+shift
    # Preserve the wall-clock plan time from the dataset while changing only
    # its calendar day. `ts` remains the authoritative epoch for matching.
    original_times=pd.to_datetime(frame['time_begin'],format='mixed')
    delta_days=(event_local.date()-pd.Timestamp(original_times.min()).date()).days
    result['time_begin']=(original_times+pd.to_timedelta(delta_days,unit='D')).map(lambda value:value.isoformat(sep=' '))
    return result

def clean(value):
    if isinstance(value,dict): return {k:clean(v) for k,v in value.items()}
    if isinstance(value,list): return [clean(v) for v in value]
    if isinstance(value,(np.integer,)):return int(value)
    if isinstance(value,(np.floating,float)):return float(value) if np.isfinite(value) else None
    return value

def display_text(value,fallback='Не указано'):
    """Convert nullable CSV display fields without leaking the string ``nan``."""
    return fallback if value is None or pd.isna(value) or not str(value).strip() else str(value)

def target_for(tr,t,stop_id=None,time_offset=0.0,same_day=False):
    """Find the first stop in the 10–15 minute window.

    ``time_offset`` is used only for a live source whose synthetic run starts
    at a different point of the historical day plan. It shifts the effective
    clock without changing the stored route geometry.
    """
    offset=float(time_offset or 0)
    route=schedule[schedule.tr_id==tr].sort_values('ts')
    if route.empty:return None
    # Full line plans are shifted to the telemetry day. Tiny two-stop
    # fixtures/scenarios may intentionally cross midnight and should retain
    # their explicit horizon.
    if same_day and len(route) >= 3:
        # A live forecast belongs to the telemetry calendar day.  Without
        # this guard a request close to midnight could select the first stop
        # after midnight from the shifted plan.
        event_day=pd.Timestamp(t,unit='s',tz='UTC').tz_convert('Europe/Moscow').date()
        local_days=pd.to_datetime(route['ts']+offset,unit='s',utc=True).dt.tz_convert('Europe/Moscow').dt.date
        route=route.loc[local_days==event_day]
        if route.empty:return None
    candidates=route[(route.ts+offset>t+600)&(route.ts+offset<=t+900)]
    effective_offset=offset
    if candidates.empty:return None
    first_time=(candidates.ts+effective_offset).min()
    candidates=candidates[(candidates.ts+effective_offset)==first_time]
    if stop_id is not None:candidates=candidates[candidates.tt_action_item_id==stop_id]
    if candidates.empty:return None
    result=candidates.sort_values('tt_action_item_id').iloc[0].to_dict()
    if effective_offset:
        result['raw_ts']=float(result['ts'])
        result['ts']=float(result['ts'])+effective_offset
        result['time_begin']=(pd.Timestamp(result['time_begin'])+pd.to_timedelta(effective_offset,unit='s')).isoformat(sep=' ')
        result['schedule_offset']=effective_offset
    return result

def planned_position_at(tr, ts):
    """Project an original-emulator packet onto its scheduled route.

    The supplied Java image generates random GPS coordinates when ``cells``
    is empty. Those coordinates are valid NDTP packets but are not a valid
    observation of the selected line. For the prototype we keep the packet's
    identity/source and replace only its map position with a deterministic
    interpolation along the same scheduled geometry shown in the dashboard.
    """
    route=schedule[schedule.tr_id==tr].dropna(subset=['lon','lat']).sort_values('ts').reset_index(drop=True)
    if route.empty:return None
    if len(route)==1:
        return {'lon':float(route.iloc[0].lon),'lat':float(route.iloc[0].lat),'speed':0.0}
    first=float(route.iloc[0].ts); last=float(route.iloc[-1].ts)
    period=max(3600.0,last-first)
    target=first+((float(ts)-first)%period)
    right=int(np.searchsorted(route.ts.to_numpy(dtype=float),target,side='right'))
    left=max(0,min(len(route)-1,right-1))
    if right>=len(route):
        start=route.iloc[-1]; end=route.iloc[0]; end_ts=float(end.ts)+period; start_ts=float(start.ts)
        segment_index=len(route)-1
    else:
        start=route.iloc[left]; end=route.iloc[right]; start_ts=float(start.ts); end_ts=float(end.ts)
        segment_index=left
    fraction=0.0 if end_ts<=start_ts else min(1.0,max(0.0,(target-start_ts)/(end_ts-start_ts)))
    lon=float(start.lon)+(float(end.lon)-float(start.lon))*fraction
    lat=float(start.lat)+(float(end.lat)-float(start.lat))*fraction
    distance=haversine(float(start.lon),float(start.lat),float(end.lon),float(end.lat))
    speed=min(130.0,max(1.0,distance/max(1.0,end_ts-start_ts)*3.6))
    return {'lon':lon,'lat':lat,'speed':speed,'segment_index':segment_index,'fraction':fraction}

def stop_neighbors(tr,stop):
    """Return readable neighbouring planned stops for a selected target."""
    route=schedule[schedule.tr_id==tr].sort_values('ts').reset_index(drop=True)
    raw_ts=float(stop.get('raw_ts',stop['ts']-float(stop.get('schedule_offset',0) or 0)))
    matches=np.flatnonzero((route.ts.to_numpy()==raw_ts)&(route.tt_action_item_id.to_numpy()==stop['tt_action_item_id']))
    if not len(matches):return 'Не указана','Не указана'
    position=int(matches[0])
    previous=route.iloc[position-1].building_address if position else None
    following=route.iloc[position+1].building_address if position+1<len(route) else None
    return display_text(previous,'Начало маршрута'),display_text(following,'Конец маршрута')

def route_risk(items):
    grouped={}
    for item in items:
        tr=int(item['tr_id']);bucket=grouped.setdefault(tr,[])
        bucket.append(item)
    result=[]
    for tr,bucket in grouped.items():
        probabilities=[v['late_probability'] for v in bucket if v.get('late_probability') is not None]
        level='high' if any(v.get('level')=='high' for v in bucket) else 'medium' if any(v.get('level')=='medium' for v in bucket) else 'low' if probabilities else 'unknown'
        result.append({'tr_id':tr,'level':level,'vehicle_count':len(bucket),'high_count':sum(v.get('level')=='high' for v in bucket),'max_late_probability':max(probabilities,default=None),'vehicles':[int(v['tr_id']) for v in bucket]})
    return sorted(result,key=lambda r:({'high':0,'medium':1,'low':2,'unknown':3}[r['level']],r['tr_id']))

def incidents(items):
    return [dict(vehicle_id=int(v['tr_id']),route_id=int(v['tr_id']),level=v.get('level','unknown'),prediction_s=v.get('prediction_s'),late_probability=v.get('late_probability'),reason=v.get('reason'),stop_address=v.get('stop_address'),source=v.get('source'),recommendation=v.get('recommendation')) for v in sorted(items,key=lambda x:({'high':0,'medium':1,'low':2,'unknown':3}[x.get('level','unknown')],-(x.get('late_probability') or -1))) if v.get('level') in {'high','medium'}]

def business_kpis(items):
    """Return transparent operational impact metrics for the current fleet."""
    operational=[]
    forecasted=[]
    for item in items:
        if item.get('trip_status') in {'completed','not_started'} or item.get('on_route') is False:
            continue
        operational.append(item)
        prediction=item.get('prediction_s')
        probability=item.get('late_probability')
        if item.get('stale') or prediction is None or probability is None:
            continue
        try:
            prediction=float(prediction); probability=float(probability)
        except (TypeError,ValueError):
            continue
        if not np.isfinite(prediction) or not np.isfinite(probability):
            continue
        forecasted.append((item,max(0.0,prediction),min(1.0,max(0.0,probability))))

    exposure=sum(delay for _,delay,_ in forecasted)
    expected=sum(delay*probability for _,delay,probability in forecasted)
    on_time=sum(1 for item,_,_ in forecasted if item.get('level')=='low')
    attention=sum(1 for item in operational if item.get('level') in {'high','medium'} or item.get('attention_level')=='critical')
    high=sum(1 for item in operational if item.get('level')=='high' or item.get('attention_level')=='critical')
    top=max(forecasted,key=lambda value:value[1]*value[2],default=None)
    return clean({
        'active_vehicles':len(operational),
        'forecasted_vehicles':len(forecasted),
        'attention_vehicles':attention,
        'high_risk_vehicles':high,
        'coverage_pct':round(100*len(forecasted)/len(operational),1) if operational else None,
        'on_time_forecast_pct':round(100*on_time/len(forecasted),1) if forecasted else None,
        'delay_exposure_minutes':round(exposure/60,1),
        'expected_delay_minutes':round(expected/60,1),
        'top_priority_tr_id':int(top[0]['tr_id']) if top else None,
        'definition':'Σ max(0, прогноз задержки) × P(задержка > 120 с) / 60; прозрачный proxy для приоритизации',
    })


def reserve_placement(vehicle):
    """Choose a concrete on-route position for the non-persistent reserve.

    The reserve starts at the selected vehicle's current projected point. That
    is the only position supported by the available evidence: the backend has
    no depot, driver or turn-around data from which to invent another start.
    The returned track follows the remaining planned stop sequence so the UI
    can draw and animate the scenario on the same line.
    """
    tr=int(vehicle['tr_id'])
    route=schedule[schedule.tr_id==tr].dropna(subset=['lon','lat']).sort_values('ts').reset_index(drop=True)
    if route.empty:
        return None
    match=vehicle.get('position_match')
    if match is None and vehicle.get('lon') is not None and vehicle.get('lat') is not None:
        match=match_stop(tr,float(vehicle['lon']),float(vehicle['lat']))
    if match is None:
        match={'segment_index':0,'projected_lon':float(route.iloc[0].lon),'projected_lat':float(route.iloc[0].lat),
               'segment_start_stop_address':display_text(route.iloc[0].building_address),'next_stop_address':display_text(route.iloc[1].building_address if len(route)>1 else route.iloc[0].building_address),
               'distance_m':None,'confidence':0}
    nearest_index=max(0,min(len(route)-2,int(match.get('segment_index',0)))) if len(route)>1 else 0
    raw_target_id=vehicle.get('target_stop_id')
    try:
        target_id=int(raw_target_id) if raw_target_id is not None and not pd.isna(raw_target_id) else None
    except (TypeError,ValueError):
        target_id=None
    target_rows=route[route.tt_action_item_id==target_id] if target_id is not None else route.iloc[0:0]
    target_index=int(target_rows.index[0]) if not target_rows.empty else min(len(route)-1,nearest_index+1)
    schedule_shift=0.0
    target_epoch=None
    if not target_rows.empty and vehicle.get('target_time_begin') is not None:
        try:
            target_epoch=epoch(vehicle['target_time_begin'])
            target_row=target_rows.iloc[int(np.argmin(np.abs(target_rows.ts.to_numpy(dtype=float)-target_epoch)))]
            # Historical snapshots and live passes can use a different
            # calendar anchor. Align the selected planned stop to the actual
            # forecast target before choosing the current segment by time.
            schedule_shift=float(target_epoch)-float(target_row.ts)
            target_index=int(target_row.name)
        except (TypeError,ValueError):
            target_epoch=None
    index=nearest_index
    if target_epoch is not None and vehicle.get('T') is not None and len(route)>1:
        try:
            current_raw_ts=epoch(vehicle['T'])-schedule_shift
            temporal_index=int(np.searchsorted(route.ts.to_numpy(dtype=float),current_raw_ts,side='right')-1)
            temporal_index=max(0,min(len(route)-2,temporal_index))
            if temporal_index<target_index:
                index=temporal_index
                segment_start=route.iloc[index]
                segment_end=route.iloc[index+1]
                temporal_fraction=min(1.0,max(0.0,(current_raw_ts-float(segment_start.ts))/max(1.0,float(segment_end.ts)-float(segment_start.ts))))
                projected_lon=float(segment_start.lon)+(float(segment_end.lon)-float(segment_start.lon))*temporal_fraction
                projected_lat=float(segment_start.lat)+(float(segment_end.lat)-float(segment_start.lat))*temporal_fraction
                # Nearest-geometry matching can jump to another pass of a
                # loop. For a forecasted target, time is the safer signal.
                match={**match,'segment_index':index,'fraction':0.0,
                       'projected_lon':projected_lon,'projected_lat':projected_lat,
                       'segment_start_stop_address':display_text(route.iloc[index].building_address),
                       'next_stop_address':display_text(route.iloc[index+1].building_address),
                       'confidence':max(float(match.get('confidence') or 0.0),0.35)}
                match['fraction']=temporal_fraction
        except (TypeError,ValueError):
            pass
    if target_index <= index:
        target_index=min(len(route)-1,index+1)
    target=route.iloc[target_index]
    track=[{'lon':float(match.get('projected_lon',route.iloc[index].lon)),'lat':float(match.get('projected_lat',route.iloc[index].lat)),'simulated':True}]
    for row in route.iloc[index+1:min(len(route),index+13)].itertuples():
        track.append({'lon':float(row.lon),'lat':float(row.lat),'simulated':True})
    current=float(vehicle.get('current_deviation_s') or 0)
    raw_forecast=vehicle.get('prediction_s')
    forecast=float(raw_forecast) if raw_forecast is not None else current
    positive_delay=max(0.0,forecast,current)
    distance_to_target=0.0
    if len(route)>1:
        first_fraction=1.0-float(match.get('fraction',0.0)) if match.get('segment_index')==index else 1.0
        distance_to_target+=haversine(float(match.get('projected_lon',route.iloc[index].lon)),float(match.get('projected_lat',route.iloc[index].lat)),route.iloc[index+1].lon,route.iloc[index+1].lat)*first_fraction
        for left,right in zip(route.iloc[index+1:target_index].itertuples(),route.iloc[index+2:target_index+1].itertuples()):
            distance_to_target+=haversine(left.lon,left.lat,right.lon,right.lat)

    # Validate the scenario against the actual target horizon and planned
    # geometry.  The reserve starts at the current projected point because no
    # depot/driver data is available; its effect is therefore bounded by what
    # can physically be reached before the target stop.
    fraction=float(match.get('fraction',0.0) or 0.0)
    if len(route)>1 and index<len(route)-1:
        current_plan_ts=float(route.iloc[index].ts)+fraction*max(0.0,float(route.iloc[index+1].ts)-float(route.iloc[index].ts))
    else:
        current_plan_ts=float(route.iloc[index].ts)
    planned_time_s=max(1.0,float(target.ts)-current_plan_ts)
    planned_speed_kmh=distance_to_target/planned_time_s*3.6 if distance_to_target else 0.0
    reserve_speed_kmh=round(min(60.0,max(15.0,planned_speed_kmh*1.15 if planned_speed_kmh else 25.0)),1)
    reserve_eta_s=distance_to_target/(reserve_speed_kmh/3.6) if distance_to_target else 0.0
    horizon=vehicle.get('horizon_s')
    if horizon is None and vehicle.get('T') is not None and vehicle.get('target_time_begin') is not None:
        try:horizon=epoch(vehicle['target_time_begin'])-epoch(vehicle['T'])
        except (TypeError,ValueError):horizon=None
    horizon=float(horizon) if horizon is not None else None
    slack_s=(horizon-reserve_eta_s) if horizon is not None else None
    confidence=float(match.get('confidence') or 0.0)
    blockers=[]
    if horizon is None or horizon<=0:
        blockers.append('Нет положительного горизонта до контрольной точки')
    if confidence<0.25:
        blockers.append('Низкая уверенность сопоставления с плановой траекторией')
    if slack_s is not None and slack_s<0:
        blockers.append('Резерв не успевает к контрольной точке по расчётному ETA')
    if vehicle.get('stale') or vehicle.get('connection_state')=='offline':
        blockers.append('Телеметрия устарела: сначала нужно восстановить связь')
    feasible=not blockers
    horizon_factor=min(1.0,max(0.0,(horizon or 0.0)/900.0))
    slack_factor=min(1.0,max(0.0,(slack_s or 0.0)/max(horizon or 1.0,1.0)))
    effect_ratio=round(min(0.35,0.05+0.20*confidence*horizon_factor*slack_factor),3) if feasible else 0.0
    relief=round(positive_delay*effect_ratio,1) if positive_delay else 0.0
    after_prediction=round(forecast-relief if forecast>0 else forecast,1)
    after_current=round(current-min(max(0.0,current),relief),1)
    return {
        'tr_id':tr,'segment_index':index,'lon':track[0]['lon'],'lat':track[0]['lat'],
        'start_stop_address':display_text(match.get('segment_start_stop_address',route.iloc[index].building_address)),
        'next_stop_address':display_text(match.get('next_stop_address',route.iloc[index+1].building_address if len(route)>1 else route.iloc[index].building_address)),
        'target_stop_address':display_text(target.building_address),'target_stop_id':int(target.tt_action_item_id),
        'distance_to_target_m':round(distance_to_target,1),'confidence':confidence,
        'horizon_s':round(horizon,1) if horizon is not None else None,
        'planned_time_to_target_s':round(planned_time_s,1),
        'planned_speed_kmh':round(planned_speed_kmh,1),
        'reserve_speed_kmh':reserve_speed_kmh,'reserve_eta_s':round(reserve_eta_s,1),
        'slack_s':round(slack_s,1) if slack_s is not None else None,
        'feasible':feasible,'blockers':blockers,'effect_ratio':effect_ratio,
        'reason':'Позиция выбрана по текущей проекции на плановый сегмент; резерв следует к целевой остановке.',
        'relief_s':relief,'before_current_deviation_s':round(current,1),'after_current_deviation_s':after_current,
        'before_prediction_s':round(forecast,1),'after_prediction_s':after_prediction,'track':track,
    }

def calibrated_uncertainty(prediction):
    """Apply the same frozen residual calibration as the ML service."""
    residuals=np.asarray(model_meta.get('calibration_residuals',[]),dtype=float)
    radius=float(model_meta.get('interval_radius_s',0.0))
    threshold=float(model_meta.get('late_threshold_s',120.0))
    probability=float((1+np.sum(residuals>threshold-prediction))/(len(residuals)+2)) if len(residuals) else None
    level='unknown' if probability is None else 'high' if probability>=.7 else 'medium' if probability>=.35 else 'low'
    return {'lower_s':prediction-radius,'upper_s':prediction+radius,'late_probability':probability,'level':level}


def load_historical_snapshot():
    """Build a fallback view from frozen V5 validate predictions."""
    vehicles.clear();archive_vehicles.clear();history.clear();deviations.clear();position_offsets.clear();position_states.clear();last_forecast.clear()
    predictions={}
    prediction_path=ARTIFACT/'submission.csv'
    if prediction_path.exists():
        prediction_frame=pd.read_csv(prediction_path,sep=';')
        predictions=dict(zip(prediction_frame.sample_id.astype(str),prediction_frame.prediction.astype(float)))
    snapshot=points.sort_values('T').groupby('tr_id',as_index=False).tail(1).sort_values('tr_id')
    for _,point in snapshot.iterrows():
        tr=int(point['tr_id']);event_time=point['T'];t=epoch(event_time)
        stop_rows=schedule[(schedule.tr_id==tr)&(schedule.tt_action_item_id==int(point['target_stop_id']))]
        if stop_rows.empty:continue
        stop=stop_rows.sort_values('ts').iloc[0]
        route=schedule[schedule.tr_id==tr].sort_values('ts').reset_index(drop=True)
        stop_index=route.index[route.tt_action_item_id==int(point['target_stop_id'])][0]
        previous=route.iloc[max(0,stop_index-1)].building_address
        following=route.iloc[min(len(route)-1,stop_index+1)].building_address
        records=traffic[(traffic.tr_id==tr)&(traffic.ts<=t)&(traffic.ts>=t-600)]
        valid=records[records.location_valid].dropna(subset=['lon','lat'])
        last=valid.iloc[-1] if not valid.empty else None
        prediction=predictions.get(str(point.get('sample_id','')))
        uncertainty=calibrated_uncertainty(prediction) if prediction is not None else {'lower_s':None,'upper_s':None,'late_probability':None,'level':'unknown'}
        address='Контрольная точка не указана' if pd.isna(stop.building_address) else str(stop.building_address)
        snapshot_vehicle=dict(
            tr_id=tr,T=timestamp(event_time).isoformat(),target_time_begin=timestamp(point.target_time_begin).isoformat(),
            target_stop_id=int(point.target_stop_id),stop_address=address,prediction_s=prediction,
            previous_stop='Контрольная точка не указана' if pd.isna(previous) else str(previous),next_stop='Контрольная точка не указана' if pd.isna(following) else str(following),
            lower_s=uncertainty['lower_s'],upper_s=uncertainty['upper_s'],late_probability=uncertainty['late_probability'],level=uncertainty['level'],
            reason='Офлайн-прогноз V5 по архивной телеметрии',recommendation='Связаться с водителем и уточнить обстановку' if uncertainty['level'] in {'high','medium'} else 'Продолжить наблюдение',
            source='historical_v5',model='v5_offline',reason_is_hypothesis=True,degraded=prediction is None,stale=False,position_time=float(last.ts) if last is not None else None,
            lon=float(last.lon) if last is not None else None,lat=float(last.lat) if last is not None else None,
        )
        archive_vehicles[tr]=snapshot_vehicle
        vehicles[tr]=dict(snapshot_vehicle)
    state.update(mode='replay',clock=timestamp(snapshot['T'].max()).isoformat(),index=len(points),snapshot=True)

def _segment_projection(lon,lat,start_lon,start_lat,end_lon,end_lat):
    """Project WGS84 coordinates onto a short planned segment in local metres."""
    scale_x=111_320*math.cos(math.radians((lat+start_lat+end_lat)/3))
    scale_y=110_540
    ax=(end_lon-start_lon)*scale_x;ay=(end_lat-start_lat)*scale_y
    px=(lon-start_lon)*scale_x;py=(lat-start_lat)*scale_y
    length_sq=ax*ax+ay*ay
    fraction=0 if length_sq==0 else min(1,max(0,(px*ax+py*ay)/length_sq))
    projected_lon=start_lon+(end_lon-start_lon)*fraction
    projected_lat=start_lat+(end_lat-start_lat)*fraction
    distance=math.hypot(px-ax*fraction,py-ay*fraction)
    return fraction,projected_lon,projected_lat,distance

def match_stop(tr_id, lon, lat, segment_hint=None):
    candidates=schedule[schedule.tr_id==tr_id].dropna(subset=['lon','lat']).sort_values('ts')
    if candidates.empty:return None
    if len(candidates)==1:
        row=candidates.iloc[0];distance=haversine(lon,lat,row.lon,row.lat)
        return {'tr_id':int(tr_id),'match_kind':'planned_stop','road_graph_matched':False,'stop_id':int(row.tt_action_item_id),'stop_address':display_text(row.building_address),'distance_m':round(distance,2),'segment_index':0,'next_stop_id':int(row.tt_action_item_id),'next_stop_address':display_text(row.building_address),'confidence':round(max(0.,1-distance/250),3)}
    best=None
    # A route can contain several runs through the same street. Once a live
    # source has been matched, keep the next match close to its previous
    # segment instead of jumping to an identical geometry from another run.
    hint = int(segment_hint) if segment_hint is not None else None
    allowed = None
    if hint is not None:
        allowed = set(range(max(0, hint - 2), min(len(candidates) - 1, hint + 8) + 1))
    rows=list(candidates.itertuples())
    for position,(start,end) in enumerate(zip(rows,rows[1:])):
        if allowed is not None and position not in allowed:
            continue
        fraction,projected_lon,projected_lat,distance=_segment_projection(lon,lat,start.lon,start.lat,end.lon,end.lat)
        same_geometry = best is not None and abs(distance-best['distance']) <= 1.0
        closer_to_hint = same_geometry and hint is not None and abs(position-hint) < abs(best['position']-hint)
        if best is None or distance<best['distance'] or closer_to_hint:
            heading=(math.degrees(math.atan2((end.lon-start.lon)*math.cos(math.radians((start.lat+end.lat)/2)),end.lat-start.lat))+360)%360
            best={'position':position,'start':start,'end':end,'fraction':fraction,'lon':projected_lon,'lat':projected_lat,'distance':distance,'heading':heading}
    if best is None:
        # A synthetic source may wrap to the first point of its route. In that
        # case recover with a global geometry match and let the estimator
        # reset its calibrated pass.
        for position,(start,end) in enumerate(zip(rows,rows[1:])):
            fraction,projected_lon,projected_lat,distance=_segment_projection(lon,lat,start.lon,start.lat,end.lon,end.lat)
            same_geometry = best is not None and abs(distance-best['distance']) <= 1.0
            closer_to_hint = same_geometry and hint is not None and abs(position-hint) < abs(best['position']-hint)
            if best is None or distance<best['distance'] or closer_to_hint:
                heading=(math.degrees(math.atan2((end.lon-start.lon)*math.cos(math.radians((start.lat+end.lat)/2)),end.lat-start.lat))+360)%360
                best={'position':position,'start':start,'end':end,'fraction':fraction,'lon':projected_lon,'lat':projected_lat,'distance':distance,'heading':heading}
    start,end=best['start'],best['end']
    nearest=start if best['fraction']<.5 else end
    distance_to_next=haversine(best['lon'],best['lat'],end.lon,end.lat)
    return {'tr_id':int(tr_id),'match_kind':'planned_trajectory_segment','road_graph_matched':False,
            'stop_id':int(nearest.tt_action_item_id),'stop_address':display_text(nearest.building_address),
            'distance_m':round(best['distance'],2),'segment_index':best['position'],
            'fraction':round(best['fraction'],6),
            'segment_start_stop_id':int(start.tt_action_item_id),'next_stop_id':int(end.tt_action_item_id),
            'segment_start_stop_address':display_text(start.building_address),
            'next_stop_address':display_text(end.building_address),
            'projected_lon':round(best['lon'],7),'projected_lat':round(best['lat'],7),
            'distance_to_next_stop_m':round(distance_to_next,2),'direction_degrees':round(best['heading'],1),
            'direction':'along_planned_trajectory','confidence':round(max(0.,1-best['distance']/250),3)}

def estimate_position(tr_id, lon, lat, ts, speed=None, segment_hint=None, fraction_hint=None, allow_slow_stop=True):
    """Estimate live schedule offset from the vehicle's position on the plan.

    The emulator moves between planned stop coordinates, so a stop-arrival-only
    detector leaves most packets at zero.  We instead project every valid GPS
    point onto the planned segment and interpolate the segment's scheduled time.
    The first valid point calibrates the historical plan to the live source's
    wall clock; later points therefore measure movement against the same
    planned sequence without pretending that an arrival was observed.
    """
    previous_state = position_states.get(tr_id, {})
    match = match_stop(
        tr_id,
        lon,
        lat,
        previous_state.get('segment_index') if segment_hint is None else segment_hint,
    )
    # A nearest point many kilometres away is not an operational match.  The
    # source may be live, but its coordinate is not evidence for this route.
    if not match or float(match.get('distance_m', float('inf'))) > 250:
        return None
    route = schedule[schedule.tr_id == tr_id].dropna(subset=['lon', 'lat']).sort_values('ts').reset_index(drop=True)
    if route.empty:
        return None
    position = int(match.get('segment_index', 0))
    if previous_state.get('segment_index') is not None and position < int(previous_state['segment_index']) - 2:
        # The planned sequence wrapped; start a new calibrated pass rather
        # than comparing this point with a stale segment from yesterday.
        position_offsets.pop(tr_id, None)
    if len(route) == 1 or position >= len(route) - 1:
        raw_expected = float(route.iloc[-1].ts)
        segment_start = route.iloc[-1]
        segment_end = route.iloc[-1]
    else:
        start = route.iloc[position]
        end = route.iloc[position + 1]
        segment_start = start
        segment_end = end
        fraction = 0.0
        if match.get('segment_index') == position:
            # Use the same projection as the geometry matcher rather than a
            # nearest-stop snap so the expected time moves continuously.
            fraction = (
                min(1.0, max(0.0, float(fraction_hint)))
                if fraction_hint is not None
                else _segment_projection(lon, lat, start.lon, start.lat, end.lon, end.lat)[0]
            )
        raw_expected = float(start.ts) + fraction * max(0.0, float(end.ts) - float(start.ts))
    # A near-zero speed packet at a planned stop is the one case where the
    # feed gives us an observed arrival signal rather than only a geometric
    # estimate.  Keep this explicit so the UI/API can distinguish it.
    observed = route[route.tt_action_item_id == int(match['stop_id'])]
    nearest_stop_distance = min((haversine(lon, lat, row.lon, row.lat) for row in observed.itertuples()), default=float('inf'))
    if allow_slow_stop and speed is not None and float(speed) < 5 and nearest_stop_distance <= 60:
        planned = float(observed.iloc[(observed.ts - ts).abs().argmin()].ts)
        # The schedule has already been moved to the live calendar day, but
        # the vehicle may start a run at a different point of that plan.  Use
        # the same first-packet calibration as the continuous projection
        # branch; comparing ``ts`` with the raw stop timestamp here used to
        # manufacture a multi-hour deviation which was then clipped to
        # ``-1800``/``1800`` seconds.
        period = max(3600.0, float(route.iloc[-1].ts) - float(route.iloc[0].ts))
        offset = position_offsets.get(tr_id)
        if offset is None:
            raw_gap = float(ts) - planned
            # A normally aligned live plan must retain a real delay (for
            # example, 45 seconds after the scheduled stop).  A synthetic
            # source, however, starts at the first stop when its process is
            # launched.  On a long circular route the wall-clock gap to that
            # first stop can be smaller than one full route period, so using
            # ``period`` as the only threshold mistakes a fresh source pass
            # for several hours of operational delay.  Treat a gap larger
            # than half a route as a new source pass while preserving normal
            # sub-hour operational deviations.
            calibration_threshold=max(3600.0, period * 0.5)
            offset = raw_gap if abs(raw_gap) > calibration_threshold else 0.0
            position_offsets[tr_id] = offset
        offset = float(offset)
        expected = planned + offset
        previous_expected = deviations.get(tr_id, {}).get('expected_ts')
        if previous_expected is not None:
            while expected - previous_expected > period / 2:
                expected -= period
            while previous_expected - expected > period / 2:
                expected += period
        deviation = float(ts) - expected
        position_states[tr_id] = {'segment_index': position, 'ts': float(ts)}
        return {
            **match,
            'match_kind': 'observed_slow_stop',
            'deviation_s': round(deviation, 1),
            'expected_ts': expected,
            'expected_position_time': pd.Timestamp(expected, unit='s', tz='UTC').tz_convert('Europe/Moscow').isoformat(),
            'previous_stop_time': pd.Timestamp(float(segment_start.ts) + offset, unit='s', tz='UTC').tz_convert('Europe/Moscow').isoformat(),
            'next_stop_time': pd.Timestamp(float(segment_end.ts) + offset, unit='s', tz='UTC').tz_convert('Europe/Moscow').isoformat(),
            'stop_times_estimated': True,
            'estimated': True,
        }

    first_ts = float(route.iloc[0].ts)
    last_ts = float(route.iloc[-1].ts)
    period = max(3600.0, last_ts - first_ts)
    # Calibrate the plan to the first valid point from this source.  The
    # emulator intentionally starts at the first geometry point at wall-clock
    # time, so comparing it to the historical 06:00 plan would otherwise
    # produce a permanent multi-hour error.  Subsequent packets then change
    # the value as the vehicle progresses through scheduled segments.
    if tr_id not in position_offsets:
        position_offsets[tr_id] = float(ts) - raw_expected
    expected = raw_expected + position_offsets[tr_id]
    # If a route wraps, keep the equivalent planned timestamp closest to the
    # previous live estimate rather than jumping back to the source day.
    previous_expected = deviations.get(tr_id, {}).get('expected_ts')
    if previous_expected is not None:
        while expected - previous_expected > period / 2:
            expected -= period
        while previous_expected - expected > period / 2:
            expected += period
    deviation = float(ts) - expected
    position_states[tr_id] = {'segment_index': position, 'ts': float(ts)}
    return {
        **match,
        'deviation_s': round(deviation, 1),
        'expected_ts': expected,
        'expected_position_time': pd.Timestamp(expected, unit='s', tz='UTC').tz_convert('Europe/Moscow').isoformat(),
        'previous_stop_time': pd.Timestamp(float(segment_start.ts) + position_offsets.get(tr_id, 0.0), unit='s', tz='UTC').tz_convert('Europe/Moscow').isoformat(),
        'next_stop_time': pd.Timestamp(float(segment_end.ts) + position_offsets.get(tr_id, 0.0), unit='s', tz='UTC').tz_convert('Europe/Moscow').isoformat(),
        'stop_times_estimated': True,
        'estimated': True,
    }

async def forecast(point,records,source):
    t=epoch(point['T']);tr=int(point['tr_id'])
    schedule_offset=float(point.get('schedule_offset',0) or 0)
    stop=target_for(tr,t,int(point['target_stop_id']),schedule_offset,same_day=source=='live')
    if stop is None or int(stop['tt_action_item_id'])!=int(point['target_stop_id']) or abs(stop['ts']-epoch(point['target_time_begin']))>1:
        raise ValueError('Точка не соответствует первой остановке в окне (T+10, T+15]')
    features=build_one(point,records,stop)
    start=time.perf_counter();degraded=False
    try:
        schedule_window=schedule[(schedule.tr_id==tr)&(schedule.ts+schedule_offset>=t-1800)&(schedule.ts+schedule_offset<=t+1800)].copy()
        if schedule_offset:
            schedule_window['ts']=schedule_window['ts'].astype(float)+schedule_offset
            schedule_window['time_begin']=(pd.to_datetime(schedule_window['time_begin'],format='mixed')+pd.to_timedelta(schedule_offset,unit='s')).map(lambda value:value.isoformat(sep=' '))
        response=await client.post(ML_URL+'/predict_v5',json={'points':[clean(point)],'histories':[[clean(x) for x in records]],'schedules':[[clean(x) for x in schedule_window.to_dict('records')]]});response.raise_for_status()
        result=response.json()['predictions'][0]
    except (httpx.HTTPError,KeyError,ValueError):
        counters['ml_failures']+=1;degraded=True
        result=dict(prediction_s=point['cur_dev_s'],lower_s=None,upper_s=None,late_probability=None,model='persistence_fallback')
    counters['last_inference_ms']=round((time.perf_counter()-start)*1000,2)
    stale=features['age_s']>120 or bool(features['missing_gps'])
    risk=result['late_probability']
    level='unknown' if degraded or stale else ('unknown' if risk is None else 'high' if risk>=.7 else 'medium' if risk>=.35 else 'low')
    reason='Устойчивое отклонение от графика'
    reason_explanation='Положение на плановом сегменте устойчиво отличается от ожидаемого времени; это сигнал для проверки, а не установленная причина.'
    if features['idle_s']>=90:
        reason='Длительная остановка: возможный простой'
        reason_explanation='Телеметрия показывает не менее 90 секунд почти без движения; причина простоя в прототипе не подтверждается.'
    elif features['speed_trend']<-2:
        reason='Снижение скорости за последние 10 минут'
        reason_explanation='Средняя скорость за последние 10 минут снижается; это может объяснять отклонение, но не доказывает пробку или неисправность.'
    if stale:
        reason='Нет свежей достоверной телеметрии'
        reason_explanation='Свежесть или качество GPS недостаточны, поэтому риск нельзя трактовать как подтверждённый.'
    valid=[r for r in records if r.get('location_valid') and r['ts']<=t and np.isfinite(r.get('lon',np.nan)) and np.isfinite(r.get('lat',np.nan))]
    last=max(valid,key=lambda r:r['ts']) if valid else None
    position_match=point.get('position_match')
    previous_stop,next_stop=stop_neighbors(tr,stop)
    # The forecast target is 10–15 minutes ahead.  Keep its neighbouring
    # stops separately, while the operational card uses the segment currently
    # occupied by the vehicle (and its estimated stop times).
    target_previous_stop, target_next_stop = previous_stop, next_stop
    if position_match:
        previous_stop = position_match.get('segment_start_stop_address', previous_stop)
        next_stop = position_match.get('next_stop_address', next_stop)
    result.update(tr_id=tr,T=timestamp(point['T']).isoformat(),target_time_begin=timestamp(point['target_time_begin']).isoformat(),target_stop_id=int(stop['tt_action_item_id']),
      stop_address=display_text(stop['building_address'],'Контрольная точка не указана'),level=level,reason=reason,reason_explanation=reason_explanation,reason_is_hypothesis=True,
      previous_stop=previous_stop,next_stop=next_stop,
      target_previous_stop=target_previous_stop,target_next_stop=target_next_stop,
      previous_stop_time=position_match.get('previous_stop_time') if position_match else None,
      next_stop_time=position_match.get('next_stop_time') if position_match else None,
      stop_times_estimated=bool(position_match and position_match.get('stop_times_estimated', True)),
      recommendation='Проверить ситуацию с водителем и доступность резерва' if level=='high' else 'Наблюдать за движением',
      source=source,degraded=degraded,stale=stale,features=features,lon=last['lon'] if last else None,lat=last['lat'] if last else None,
      position_time=last['ts'] if last else None,horizon_s=epoch(point['target_time_begin'])-t,
      current_deviation_s=point.get('cur_dev_s'),
      deviation_estimated=bool(point.get('deviation_estimated', False)),
      schedule_offset_s=round(schedule_offset,1),
      horizon_fallback=bool(point.get('horizon_fallback', False)),
      position_match=point.get('position_match'))
    vehicles[tr]=clean(result);counters['predictions']+=1
    return clean(result)

async def ingest(event):
    global schedule,live_schedule_day,last_telemetry_received_at
    if state.get('ingest_paused'):
        counters['paused_packets']+=1
        return
    tr=event['tr_id'];ts=epoch(event['event_time'])
    if ts>time.time()+60: raise ValueError('Телеметрия из будущего')
    event_day=timestamp(event['event_time']).date()
    if state['mode']=='live' and live_schedule_day!=event_day:
        schedule=align_schedule_to_event_day(schedule, event['event_time'])
        live_schedule_day=event_day
    records=history[tr]
    if records and ts<=records[-1]['ts']:
        counters['late_or_duplicate_packets']+=1;return
    last_telemetry_received_at=time.time()
    row=dict(event,ts=ts);records.append(row);counters['telemetry_rows']+=1
    if state['mode']!='live':
        # The historical forecast stays visible by default. Only a fresh NDTP
        # coordinate can overlay it, so delayed source traffic never looks live.
        # A live custom/original packet for a vehicle absent from the frozen
        # archive must not create a half-empty "archive" row in replay.
        if tr not in archive_vehicles:
            return
        if event['location_valid'] and ts>=time.time()-LIVE_TRACK_TTL_S:
            historical=dict(archive_vehicles.get(tr, {}))
            vehicles[tr]={**historical,
                'tr_id':tr,'reason':'Live NDTP: позиция получена; прогноз остаётся историческим','reason_is_hypothesis':True,
                'source':'historical_v5','telemetry_source':event.get('telemetry_source','unknown'),
                'live_position':True,'live_position_time':timestamp(event['event_time']).isoformat(),
                'lon':event['lon'],'lat':event['lat'],'position_time':ts,
            }
        return
    state['clock']=timestamp(event['event_time']).isoformat()
    projection_segment_hint = None
    if event.get('telemetry_source')=='ndtp_nav00' and event.get('location_valid'):
        # The external image's empty-cell auto generator emits random GPS.
        # Keep the original NDTP source for provenance, but make its map
        # position follow the same planned geometry as the dashboard route.
        raw_lon,raw_lat=float(event['lon']),float(event['lat'])
        projected=planned_position_at(tr,ts)
        if projected is not None:
            projection_segment_hint=projected.get('segment_index')
            event['raw_lon']=raw_lon; event['raw_lat']=raw_lat
            event['lon']=projected['lon']; event['lat']=projected['lat']; event['speed']=projected['speed']
            event['position_adjusted']=True
            # ``row`` was copied into history before the projection so the
            # raw packet remains available for provenance.  Forecast builds
            # its current position from history, therefore keep the observed
            # row in sync with the route-projected coordinates as well.  If
            # this is omitted, every ML refresh briefly puts the marker back
            # at the random GPS coordinate emitted by the stock emulator.
            if records and records[-1].get('ts') == ts:
                records[-1].update({
                    'lon': event['lon'],
                    'lat': event['lat'],
                    'speed': event.get('speed'),
                    'raw_lon': event.get('raw_lon'),
                    'raw_lat': event.get('raw_lat'),
                    'position_adjusted': True,
                })
    position_match = None
    if event['location_valid']:
        position_match = estimate_position(
            tr,
            event['lon'],
            event['lat'],
            ts,
            event.get('speed'),
            segment_hint=projection_segment_hint,
            fraction_hint=projected.get('fraction') if projection_segment_hint is not None else None,
            allow_slow_stop=event.get('telemetry_source') not in {'ndtp_nav00', 'custom_ndtp_nav00'},
        )
        if position_match is not None:
            deviations[tr] = {
                'stop': int(position_match['stop_id']),
                'value': float(position_match['deviation_s']),
                'expected_ts': float(position_match['expected_ts']),
                'position_match': position_match,
            }

    # Position is refreshed for every valid NDTP packet.
    # ML inference stays throttled separately below.
    current = vehicles.get(tr)

    if current is not None and event['location_valid']:
        current['lon'] = event['lon']
        current['lat'] = event['lat']
        current['position_time'] = ts
        current['telemetry_source'] = event.get(
            'telemetry_source',
            'unknown',
        )
        current['position_adjusted'] = bool(event.get('position_adjusted', current.get('position_adjusted', False)))
        if event.get('raw_lon') is not None and event.get('raw_lat') is not None:
            current['raw_lon'] = event['raw_lon']
            current['raw_lat'] = event['raw_lat']
        current['live_position_time'] = timestamp(
            event['event_time']
        ).isoformat()
        if position_match is not None:
            current['current_deviation_s'] = position_match['deviation_s']
            current['deviation_estimated'] = bool(position_match.get('estimated', False))
            current['position_match'] = position_match
            current['previous_stop'] = position_match.get('segment_start_stop_address', current.get('previous_stop'))
            current['next_stop'] = position_match.get('next_stop_address', current.get('next_stop'))
            current['previous_stop_time'] = position_match.get('previous_stop_time')
            current['next_stop_time'] = position_match.get('next_stop_time')
            current['stop_times_estimated'] = bool(position_match.get('stop_times_estimated', True))
        else:
            current['current_deviation_s'] = None
            current['deviation_estimated'] = False
            current['position_match'] = None

    if ts - last_forecast.get(tr, 0) < 30:
        return
    last_forecast[tr]=ts
    schedule_offset=float(position_offsets.get(tr,0) or 0) if position_match is not None else 0.0
    stop=target_for(tr,ts,time_offset=schedule_offset,same_day=True)
    if stop is None:
        vehicles[tr]=dict(tr_id=tr,T=timestamp(event['event_time']).isoformat(),level='unknown',reason='Нет плановой остановки через 10–15 минут',reason_is_hypothesis=True,lon=event['lon'] if event['location_valid'] else None,lat=event['lat'] if event['location_valid'] else None,source='live',telemetry_source=event.get('telemetry_source','unknown'),position_adjusted=bool(event.get('position_adjusted',False)),position_origin='planned_route_projection' if event.get('position_adjusted') else None,prediction_s=None,late_probability=None,position_time=ts)
        return
    schedule_offset=float(stop.get('schedule_offset',schedule_offset) or 0)
    current_deviation = position_match['deviation_s'] if position_match is not None else None
    point=dict(tr_id=tr,T=event['event_time'],target_stop_id=int(stop['tt_action_item_id']),target_time_begin=stop['time_begin'],
               cur_dev_s=float(current_deviation if current_deviation is not None else 0),
               deviation_estimated=bool(position_match and position_match.get('estimated', False)),
               position_match=position_match,schedule_offset=schedule_offset,
               horizon_fallback=bool(stop.get('horizon_fallback',False)))
    result=await forecast(point,list(records),'live')
    result['telemetry_source']=event.get('telemetry_source','unknown')
    result['position_adjusted']=bool(event.get('position_adjusted',False))
    if event.get('position_adjusted'):
        result['position_origin']='planned_route_projection'
        result['raw_lon']=event.get('raw_lon')
        result['raw_lat']=event.get('raw_lat')
    vehicles[tr]=clean(result)
    if current_deviation is None:
        result.update(current_deviation_s=None,deviation_estimated=False,
                      reason=f"{result['reason']}; текущее отклонение не удалось оценить по положению")
        vehicles[tr]=clean(result)

async def on_ndtp(event):
    unit = event.pop('unit_id')

    tr = mapping.get(unit)

    if tr is None:
        counters['unknown_units'] += 1
        return

    is_custom = (
        unit
        >= CUSTOM_UNIT_ID_OFFSET
    )

    event['tr_id'] = tr

    try:
        validated = Telemetry(
            **event
        )

        payload = (
            validated.model_dump()
        )

        payload[
            'telemetry_source'
        ] = (
            'custom_ndtp_nav00'
            if is_custom
            else 'ndtp_nav00'
        )

        payload[
            'simulated'
        ] = is_custom

        async with lock:
            await ingest(
                payload
            )

    except ValueError:
        counters[
            'invalid_events'
        ] += 1

@asynccontextmanager
async def lifespan(app):
    global schedule,schedule_template,traffic,points,mapping,client,model_meta,last_telemetry_received_at
    init_store()
    schedule = load_schedule(
        DATA / 'validate/schedule_plan.csv'
    )

    base_schedule = schedule.copy()

    custom_schedule = (
        base_schedule.copy()
    )

    custom_schedule['tr_id'] = (
        custom_schedule['tr_id']
        .astype(int)
        + CUSTOM_TR_ID_OFFSET
    )

    schedule = pd.concat(
        [
            base_schedule,
            custom_schedule,
        ],
        ignore_index=True,
    )

    schedule_template = schedule.copy()
    all_ids=sorted({int(item) for item in schedule.tr_id.unique()})
    if db.execute('SELECT COUNT(*) AS count FROM assignments WHERE dispatcher_id=?',('dispatcher-01',)).fetchone()['count']==0:
        db.executemany('INSERT OR IGNORE INTO assignments(dispatcher_id,tr_id) VALUES(?,?)',
                       [('dispatcher-01',tr_id) for index,tr_id in enumerate(all_ids) if index%2==0])
    if seed_dispatcher_02_all_routes:
        db.executemany('INSERT OR IGNORE INTO assignments(dispatcher_id,tr_id) VALUES(?,?)',
                       [('dispatcher-02',tr_id) for tr_id in all_ids])
        db.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)',
                   ('dispatcher_02_all_routes_v1',pd.Timestamp.now(tz='Europe/Moscow').isoformat()))
    db.commit()
    # Если базовое ТС назначено диспетчеру,
    # автоматически назначаем ему и его custom-копию.
    existing_assignments = db.execute(
        '''
        SELECT dispatcher_id, tr_id
        FROM assignments
        '''
    ).fetchall()

    custom_assignments = []

    for assignment in existing_assignments:
        tr_id = int(
            assignment['tr_id']
        )

        # Не создаём custom-копию
        # от уже виртуального ТС.
        if tr_id >= CUSTOM_TR_ID_OFFSET:
            continue

        custom_tr_id = (
            tr_id
            + CUSTOM_TR_ID_OFFSET
        )

        custom_assignments.append(
            (
                assignment[
                    'dispatcher_id'
                ],
                custom_tr_id,
            )
        )

    db.executemany(
        '''
        INSERT OR IGNORE INTO assignments(
            dispatcher_id,
            tr_id
        )
        VALUES (?, ?)
        ''',
        custom_assignments,
    )

    db.commit()
    traffic=load_traffic(DATA/'validate/traffic.csv')
    points=pd.read_csv(DATA/'validate/points.csv').sort_values('T').reset_index(drop=True)
    ids=pd.read_csv(DATA/'validate/traffic.csv',usecols=['unit_id','tr_id']).drop_duplicates()
    mapping={int(r.unit_id):int(r.tr_id) for r in ids.itertuples()}
    for row in ids.itertuples():
        custom_unit_id = (
            int(row.unit_id)
            + CUSTOM_UNIT_ID_OFFSET
        )

        custom_tr_id = (
            int(row.tr_id)
            + CUSTOM_TR_ID_OFFSET
        )

        mapping[
            custom_unit_id
        ] = custom_tr_id
    mapping.update({int(k):int(v) for k,v in json.loads(os.getenv('UNIT_MAP','{}')).items()})
    client=httpx.AsyncClient(timeout=2)
    model_meta=json.loads((ARTIFACT/'model.json').read_text(encoding='utf-8'))
    load_historical_snapshot()
    if os.getenv('START_MODE','live')=='live':
        state.update(mode='live',index=0,clock=None,snapshot=False);vehicles.clear();last_telemetry_received_at=0.0
    server=await asyncio.start_server(lambda r,w:handle(r,w,on_ndtp,counters),'0.0.0.0',int(os.getenv('NDTP_PORT','9201')))
    official_warmup=asyncio.create_task(warm_official_source())
    yield
    official_warmup.cancel()
    try:
        await official_warmup
    except asyncio.CancelledError:
        pass
    server.close();await server.wait_closed();await client.aclose()
    if db is not None:db.close()

app=FastAPI(title='Такт — Backend API',version='1.1.0',lifespan=lifespan,
    description=DESCRIPTION,openapi_tags=TAGS,servers=SERVERS,
    contact=CONTACT,license_info=LICENSE,docs_url='/docs/swagger',redoc_url='/redoc',
    swagger_ui_parameters={
        # Keep the operation list readable while leaving request/response
        # schemas available in the Models section.
        'docExpansion':'list',
        'defaultModelsExpandDepth':0,
        'defaultModelExpandDepth':1,
        'displayOperationId':True,
        'displayRequestDuration':True,
        'filter':True,
        'deepLinking':True,
        'persistAuthorization':True,
        'tryItOutEnabled':True,
        'requestSnippetsEnabled':True,
        'showExtensions':False,
        'showCommonExtensions':False,
        'syntaxHighlight':{'theme':'arta'},
    })


def custom_openapi():
    """Build the published contract with examples and integration metadata."""
    if app.openapi_schema:
        return app.openapi_schema
    schema=get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=TAGS,
        servers=SERVERS,
        contact=CONTACT,
        license_info=LICENSE,
    )
    schema['externalDocs']={
        'description':'Руководство API и примеры сценариев',
        'url':'/docs',
    }
    schema['info']['x-contract-status']='prototype-local'
    schema['info']['x-generated-by']='scripts/export_openapi.py'
    schema['x-tagGroups']=[
        {'name':'Рабочий контур','tags':['Состояние','Источники и прогноз','Карта','Риски и what-if']},
        {'name':'Операционная работа','tags':['Диспетчеры','Указания водителю','Источники данных']},
        {'name':'Диагностика и совместимость','tags':['Качество модели','Совместимость API']},
    ]
    for path, methods in OPENAPI_EXAMPLES.items():
        path_item=schema.get('paths',{}).get(path)
        if not path_item:
            continue
        for method, example in methods.items():
            operation_schema=path_item.get(method)
            if not operation_schema:
                continue
            if 'request' in example:
                request_body=operation_schema.setdefault('requestBody',{})
                media=request_body.setdefault('content',{}).setdefault('application/json',{})
                media.setdefault('examples',{})['basic']={
                    'summary':'Минимальный рабочий пример',
                    'value':example['request'],
                }
            if 'response' in example:
                response=operation_schema.setdefault('responses',{}).setdefault('200',{})
                media=response.setdefault('content',{}).setdefault('application/json',{})
                media.setdefault('examples',{})['basic']={
                    'summary':'Форма успешного ответа',
                    'value':example['response'],
                }
    app.openapi_schema=schema
    return schema


app.openapi=custom_openapi

@app.middleware('http')
async def observe_request(request:Request,call_next):
    """Keep bounded in-process request timings for the operations endpoint."""
    started=time.perf_counter()
    status=500
    try:
        response=await call_next(request)
        status=response.status_code
        if status>=500:runtime_stats['requests_errors']+=1
        return response
    except Exception:
        runtime_stats['requests_errors']+=1
        raise
    finally:
        runtime_stats['requests_total']+=1
        elapsed=round((time.perf_counter()-started)*1000,2)
        runtime_stats['recent_request_ms'].append(elapsed)
        logger.info(json.dumps({'event':'http_request','method':request.method,'path':request.url.path,'status':status,'latency_ms':elapsed},ensure_ascii=False))

@app.get('/health',**operation('health'))
async def health():
    """Report backend availability and the current shared source mode."""
    return {'status':'ok','mode':state['mode']}

@app.get('/health/ready',**operation('readiness'))
async def readiness():
    """Check dependencies required to serve forecasts, including the ML API."""
    checks={'schedule':schedule is not None and not schedule.empty,'traffic':traffic is not None and not traffic.empty,
            'points':points is not None and not points.empty,'sqlite':db is not None,
            'artifacts':(ARTIFACT/'metrics.json').exists() and (ARTIFACT/'model.json').exists()}
    try:
        response=await client.get(ML_URL+'/health')
        checks['ml_api']=response.is_success
    except httpx.HTTPError:
        checks['ml_api']=False
    ready=all(checks.values())
    if not ready:raise HTTPException(503,{'status':'not_ready','checks':checks})
    return {'status':'ready','checks':checks}

@app.get('/api/observability',**operation('observability'))
async def observability():
    """Expose bounded local runtime measurements for a collector or dashboard."""
    latencies=list(runtime_stats['recent_request_ms'])
    return {'status':'ok','uptime_s':round(time.time()-runtime_stats['started_at'],2),
            'requests_total':runtime_stats['requests_total'],'requests_errors':runtime_stats['requests_errors'],
            'request_latency_ms':{'samples':len(latencies),'last':latencies[-1] if latencies else None,
                                  'avg':round(sum(latencies)/len(latencies),2) if latencies else None,
                                  'max':max(latencies,default=None)},
            'queues':{'telemetry_points_in_memory':sum(len(items) for items in history.values()),
                      'driver_commands_persisted':db.execute('SELECT COUNT(*) FROM driver_commands').fetchone()[0],
                      'active_simulations':sum(run.get('status') in {'queued','running'} for run in simulation_runs.values())},
            'counters':dict(counters)}

@app.get('/api/dispatchers',**operation('dispatchers'))
async def dispatchers():return {'profiles':list_dispatchers(),'authentication':'managed_local_accounts'}

@app.get('/api/dispatchers/{dispatcher_id}',**operation('dispatcher_profile'))
async def dispatcher_profile(dispatcher_id:str):
    profile=get_dispatcher(dispatcher_id)
    if profile is None:raise HTTPException(404,'Dispatcher not found')
    return profile

@app.post('/api/admin/dispatchers',status_code=201,**operation('create_dispatcher'))
async def create_dispatcher(body:DispatcherCreate):
    dispatcher_id=f"dispatcher-{uuid.uuid4().hex[:8]}"
    try:
        db.execute('INSERT INTO dispatchers(id,name,login,role,status,created_at) VALUES(?,?,?,?,?,?)',(dispatcher_id,body.name,body.login,body.role,'active',pd.Timestamp.now(tz='Europe/Moscow').isoformat()));db.commit()
    except sqlite3.IntegrityError:raise HTTPException(409,'Login already exists')
    return get_dispatcher(dispatcher_id)

@app.put('/api/admin/dispatchers/{dispatcher_id}/assignments',**operation('assignments'))
async def set_dispatcher_assignments(dispatcher_id:str,body:AssignmentUpdate):
    if get_dispatcher(dispatcher_id) is None:raise HTTPException(404,'Dispatcher not found')
    known={int(item) for item in schedule.tr_id.unique()}
    if any(item not in known for item in body.tr_ids):raise HTTPException(422,'Unknown vehicle/route assignment')
    db.execute('DELETE FROM assignments WHERE dispatcher_id=?',(dispatcher_id,))
    db.executemany('INSERT INTO assignments(dispatcher_id,tr_id) VALUES(?,?)',[(dispatcher_id,item) for item in sorted(set(body.tr_ids))]);db.commit()
    return get_dispatcher(dispatcher_id)

@app.delete('/api/admin/dispatchers/{dispatcher_id}',**operation('delete_dispatcher'))
async def delete_dispatcher(dispatcher_id:str,body:AdminAction):
    operator=get_dispatcher(body.operator_id)
    target=get_dispatcher(dispatcher_id)
    if operator is None or operator['role']!='Администратор':
        raise HTTPException(403,'Admin account required')
    if target is None:
        raise HTTPException(404,'Dispatcher not found')
    if target['role']=='Администратор':
        raise HTTPException(422,'Admin account cannot be deleted')
    db.execute('DELETE FROM assignments WHERE dispatcher_id=?',(dispatcher_id,))
    db.execute('DELETE FROM dispatchers WHERE id=?',(dispatcher_id,))
    db.commit()
    return {'deleted':True,'dispatcher_id':dispatcher_id}

@app.post('/api/telemetry',**operation('telemetry'))
async def telemetry(events:list[Telemetry]):
    if not 1<=len(events)<=1000:raise HTTPException(422,'Batch size must be 1..1000')
    try:
        # Validate all timestamps before mutating the buffer.
        for e in events:
            if epoch(e.event_time)>time.time()+60:raise ValueError('Future timestamp')
        async with lock:
            for e in events:
                payload=e.model_dump();payload['telemetry_source']='http_json'
                await ingest(payload)
    except ValueError as e:raise HTTPException(422,str(e))
    return {'accepted':len(events)}

@app.post('/api/predict',**operation('predict'))
async def predict(point:Point):
    try:
        if epoch(point.T)>time.time()+60:raise ValueError('Future forecast time')
        async with lock:return await forecast(point.model_dump(),list(history[point.tr_id]),'api')
    except ValueError as e:raise HTTPException(422,str(e))

@app.post('/api/mode',**operation('mode'))
async def mode(body:Mode):
    global schedule,live_schedule_day,last_telemetry_received_at
    async with lock:
        schedule=schedule_template.copy();live_schedule_day=None
        if body.mode=='replay':load_historical_snapshot()
        else:
            state.update(mode='live',index=0,clock=None,snapshot=False);vehicles.clear();history.clear();deviations.clear();position_offsets.clear();position_states.clear();last_forecast.clear();last_telemetry_received_at=0.0
    return state

@app.post('/api/replay/step',**operation('replay'))
async def replay():
    async with lock:
        if state['mode']!='replay':raise HTTPException(409,'Switch to replay mode first')
        if state.get('snapshot'):
            vehicles.clear();history.clear();deviations.clear();position_offsets.clear();position_states.clear();last_forecast.clear()
            state.update(index=0,clock=None,snapshot=False)
        i=state['index']
        if i>=len(points):return {'done':True,**state}
        p=points.iloc[i].to_dict();t=epoch(p['T'])
        rows=traffic[(traffic.tr_id==p['tr_id'])&(traffic.ts<=t)&(traffic.ts>=t-600)].to_dict('records')
        try:result=await forecast(p,rows,'historical_replay')
        except ValueError as e:raise HTTPException(422,str(e))
        state['clock']=timestamp(p['T']).isoformat();state['index']+=1
        return {'done':False,'prediction':result,**state}

def operational_vehicle(tr, now):
    """Describe trip progress independently of forecast availability.

    An empty prediction window is not evidence that a trip ended. Completion
    requires the last observed position at the end of the final plan segment.
    A never-seen vehicle has an unconfirmed departure, not a lost connection.
    """
    records=list(history.get(tr, ()))
    live=vehicles.get(tr, {})
    seen=bool(records) or live.get('source') in {'live', 'last_known_live'}
    item=dict(live if seen else {})
    item.update(tr_id=tr)
    route=schedule[schedule.tr_id==tr].dropna(subset=['lon','lat']).sort_values('ts').reset_index(drop=True)
    if route.empty:return item
    first,last=route.iloc[0],route.iloc[-1]
    item.update(route_start_stop=display_text(first.building_address),route_end_stop=display_text(last.building_address))
    match=deviations.get(tr,{}).get('position_match') or item.get('position_match') or {}
    if records:
        valid=next((r for r in reversed(records) if r.get('location_valid')),None)
        if valid:item.update(lon=valid['lon'],lat=valid['lat'],position_time=valid['ts'])
    packet_ts=records[-1]['ts'] if records else item.get('position_time')
    age=max(0,now-float(packet_ts)) if packet_ts is not None else None
    item['telemetry_age_s']=round(age,1) if age is not None else None
    item['telemetry_source']=item.get('telemetry_source') or (records[-1].get('telemetry_source') if records else None)
    ended=(seen and len(route)>1 and match.get('segment_index')==len(route)-2
           and float(match.get('fraction',0))>=.995 and item.get('lon') is not None
           and haversine(item['lon'],item['lat'],last.lon,last.lat)<=30)
    if match:
        index=min(len(route)-1,max(0,int(match.get('segment_index',0))))
        offset=float(position_offsets.get(tr,0))
        deviation=float(match.get('deviation_s',0))
        item.update(previous_stop=display_text(route.iloc[index].building_address),
                    next_stop=None if ended else display_text(route.iloc[min(index+1,len(route)-1)].building_address),
                    previous_stop_time=pd.Timestamp(float(route.iloc[index].ts)+offset+deviation,unit='s',tz='UTC').isoformat(),
                    next_stop_time=None if ended else pd.Timestamp(float(route.iloc[min(index+1,len(route)-1)].ts)+offset+deviation,unit='s',tz='UTC').isoformat(),
                    stop_times_estimated=True,position_match=match,
                    current_deviation_s=match.get('deviation_s'),deviation_estimated=True)
    item.update(trip_status='active' if seen else 'not_started',on_route=seen,attention_level='normal')
    if ended:
        item.update(trip_status='completed',on_route=False,status_label='Рейс завершён',level='unknown',
                    reason='Достигнута конечная остановка; рейс снят с активной карты',connection_state='completed')
    elif not seen:
        item.update(source='waiting_for_live',connection_state='waiting',status_label='Выход не подтверждён',level='unknown',
                    reason='ТС ещё не передавало телеметрию: выход на маршрут не подтверждён',lon=float(first.lon),lat=float(first.lat),position_time=None)
    elif state.get('ingest_paused'):
        item.update(status_label='Поток приостановлен',level='unknown',reason='Приём телеметрии приостановлен оператором',connection_state='paused')
    elif age is None or age>LIVE_STALE_S:
        item.update(source='last_known_live',connection_state='offline',attention_level='critical',level='high',
                    status_label='Нет связи · критично',reason='Потеря телеметрии на активном рейсе',
                    recommendation='Срочно связаться с водителем и проверить связь')
    else:
        item['connection_state']='live'
        if item.get('prediction_s') is None:
            item.update(status_label='Нет контрольной точки',reason='Нет плановой остановки в окне 10–15 минут; рейс продолжается')
        elif item.get('degraded'):
            item.update(status_label='Модель недоступна',reason='Прогноз модели временно недоступен')
        else:
            item['status_label']={'high':'Риск опоздания','medium':'Нужно проверить','low':'В графике'}.get(item.get('level'),'Нет оценки')
    if ended or not seen or item['connection_state'] in {'offline','paused'}:
        item.update(prediction_s=None,late_probability=None,lower_s=None,upper_s=None,stale=True)
    track=live_track(tr)
    if track:item['live_track']=track
    item['dispatcher_recommendation']=dispatcher_recommendation(item)
    return clean(item)


def action_vehicle(tr_id):
    """Read the same evidence that is shown to the dispatcher before acting."""
    tr=int(tr_id)
    if state.get('mode')=='live':
        return operational_vehicle(tr,time.time())
    return dict(vehicles.get(tr) or archive_vehicles.get(tr) or {})


def dispatcher_recommendation(vehicle):
    """Select a next action from freshness, risk and the current forecast."""
    level=vehicle.get('level','unknown')
    prediction=vehicle.get('prediction_s')
    probability=vehicle.get('late_probability')
    if vehicle.get('trip_status')=='completed':
        action='maintain';title='Действие не требуется';reason='Рейс уже завершён.';priority='none'
    elif vehicle.get('connection_state') in {'offline','waiting'} or vehicle.get('attention_level')=='critical':
        action='contact';title='Восстановить связь';reason='Сначала нужно подтвердить состояние ТС по каналу связи.';priority='critical'
    elif level=='high' and prediction is not None and float(prediction)>0:
        action='contact';title='Связаться и проверить обстановку';reason='Высокий риск положительной задержки требует подтверждения причины до выпуска управляемого действия.';priority='high'
    elif level=='medium' and prediction is not None and float(prediction)>0:
        action='contact';title='Уточнить возможность выдержать график';reason='Средний риск положительной задержки: сначала собираем подтверждение от водителя.';priority='medium'
    elif prediction is not None and float(prediction)<-60:
        action='slow_down_safely';title='Согласовать выдерживание интервала';reason='Прогноз показывает опережение плана; важно не создавать неравномерный интервал.';priority='low'
    else:
        action='maintain';title='Продолжать по графику';reason='Нет сигнала, требующего активного вмешательства.';priority='low'
    return {'action':action,'title':title,'reason':reason,'priority':priority,'late_probability':probability,'prediction_s':prediction}

DRIVER_ACTION_TITLES={
    'contact':'Связаться с водителем',
    'maintain':'Продолжать по графику',
    'accelerate_safely':'Безопасно сократить отставание',
    'slow_down_safely':'Согласовать выдерживание интервала',
}

def action_benefit_model(vehicle,action,decision=None):
    """Estimate action value from the same evidence used by the guardrails.

    This is intentionally a transparent decision-support model, not a claim of
    learned causal impact: the source data has no labelled post-action outcomes.
    Its versioned output can later be replaced by a calibrated outcome model.
    """
    decision=decision or {}
    prediction=float(vehicle['prediction_s']) if vehicle.get('prediction_s') is not None else None
    probability=float(vehicle['late_probability']) if vehicle.get('late_probability') is not None else None
    probability=None if probability is None else min(1.0,max(0.0,probability))
    expected_before=None if prediction is None or probability is None else round(max(0.0,prediction)*probability,1)
    stale=bool(vehicle.get('stale')) or vehicle.get('connection_state') in {'offline','waiting','historical'}
    evidence_count=sum(value is not None for value in (prediction,probability,vehicle.get('horizon_s')))
    confidence=min(1.0,0.35+0.2*evidence_count)
    if stale:confidence*=0.35
    effect=decision.get('expected_effect') or {}
    saved_expected=0.0
    information_value=0.0
    rationale=''
    if action=='accelerate_safely':
        recoverable=max(0.0,float(effect.get('recoverable_delay_s') or 0.0))
        saved_expected=recoverable*(probability or 0.0)
        rationale='Потенциал сокращения задержки взвешен вероятностью события и снижением уверенности при устаревшей телеметрии.'
    elif action=='contact':
        # Contact has no claimed physical delay saving; its value is the risk
        # exposure that becomes observable before the next intervention.
        information_value=(expected_before or 0.0)*0.10
        rationale='Информационное действие: не обещает снижение задержки, но уменьшает неопределённость перед следующим шагом.'
    elif action=='slow_down_safely':
        information_value=max(0.0,abs(min(0.0,prediction or 0.0)))*0.20
        rationale='Ценность — в снижении риска неравномерного интервала; это не пассажиро-минуты и не факт результата.'
    else:
        rationale='Наблюдение сохраняет текущую картину без заявленного активного эффекта.'
    total_value=saved_expected+information_value
    denominator=max(1.0,expected_before or 0.0)
    score=min(100.0,max(0.0,100.0*total_value/denominator*confidence)) if total_value else 0.0
    if action=='contact' and expected_before and not decision.get('allowed',True):
        score=0.0
    blockers=decision.get('blockers') or []
    if blockers:
        score=0.0
        rationale=f"Недоступно: {blockers[0]}"
    return clean({
        'model':'benefit-v1-rule-based','score':round(score,1),'confidence_pct':round(confidence*100,1),
        'expected_exposure_s':expected_before,'expected_saved_delay_s':round(saved_expected,1),
        'information_value_s':round(information_value,1),'rationale':rationale,
        'allowed':bool(decision.get('allowed',True)),'action':action,
    })

def simulate_driver_response(vehicle,command):
    """Produce an explicit, deterministic local driver response for demos/tests."""
    action=command['action']
    decision=command.get('decision') or driver_decision(vehicle,action)
    prediction=float(vehicle['prediction_s']) if vehicle.get('prediction_s') is not None else None
    probability=float(vehicle['late_probability']) if vehicle.get('late_probability') is not None else None
    probability=None if probability is None else min(1.0,max(0.0,probability))
    before=prediction
    after=prediction
    result='acknowledged'
    response='Водитель получил указание и подтвердил текущую обстановку.'
    if action=='accelerate_safely':
        recoverable=max(0.0,float((decision.get('expected_effect') or {}).get('recoverable_delay_s') or 0.0))
        after=max(0.0,(prediction or 0.0)-recoverable)
        result='applied'
        response='Принял. Сокращаю отставание только в безопасном режиме, без нарушения ПДД.'
    elif action=='slow_down_safely':
        correction=min(abs(min(0.0,prediction or 0.0)),30.0)
        after=(prediction or 0.0)+correction
        result='applied'
        response='Принял. Выравниваю интервал без резкого торможения.'
    elif action=='maintain':
        result='applied'
        response='Принял. Продолжаю движение по графику и сообщу при изменении обстановки.'
    else:
        result='acknowledged'
        response='Принял. Текущее состояние подтверждаю, продолжаю наблюдение.'
    expected_saved=None if before is None or after is None or probability is None else round(max(0.0,before-after)*probability,1)
    next_step={
        'action':'recheck_vehicle',
        'title':'Повторно проверить рейс',
        'check_in_s':decision.get('next_check_in_s'),
        'check_at':decision.get('next_check_at'),
        'reason':'Проверить новый прогноз и подтверждённую обстановку после реакции водителя.',
    }
    return clean({
        'mode':'local_driver_simulator','simulated_at':pd.Timestamp.now(tz='Europe/Moscow').isoformat(),
        'acknowledged':True,'result':result,'response':response,
        'projection':{'prediction_before_s':before,'prediction_after_s':round(after,1) if after is not None else None,'expected_saved_delay_s':expected_saved},
        'next_step':next_step,
        'note':'Симуляция реакции для прототипа; live-телеметрия и прогноз в state не изменяются.',
    })

def simulate_reserve_response(action):
    """Confirm a reserve release with the already calculated placement effect."""
    placement=action.get('placement') or {}
    before=placement.get('before_prediction_s')
    after=placement.get('after_prediction_s')
    now=pd.Timestamp.now(tz='Europe/Moscow').isoformat()
    return clean({
        'mode':'local_fleet_simulator','simulated_at':now,'acknowledged':True,'result':'applied',
        'response':'Флот подтвердил выпуск резервного ТС на рассчитанный участок.',
        'projection':{
            'prediction_before_s':before,'prediction_after_s':after,
            'expected_saved_delay_s':None if before is None or after is None else round(max(0.0,float(before)-float(after)),1),
        },
        'note':'Локальная эмуляция подтверждения флота; реальный внешний парк не подключён.',
    })


def driver_decision(vehicle,action):
    """Evaluate a driver instruction and return evidence for the audit trail."""
    recommendation=dispatcher_recommendation(vehicle)
    prediction=vehicle.get('prediction_s')
    current=vehicle.get('current_deviation_s')
    probability=vehicle.get('late_probability')
    horizon=vehicle.get('horizon_s')
    stale=bool(vehicle.get('stale')) or vehicle.get('connection_state') in {'offline','waiting'}
    prediction=float(prediction) if prediction is not None else None
    current=float(current) if current is not None else None
    probability=float(probability) if probability is not None else None
    horizon=float(horizon) if horizon is not None else None
    expected_delay_s=None if prediction is None or probability is None else round(max(0.0,prediction)*min(1.0,max(0.0,probability)),1)
    blockers=[]
    if vehicle.get('trip_status')=='completed':
        blockers.append('Рейс завершён')
    if action=='contact':
        if vehicle.get('connection_state')=='waiting':
            blockers.append('Выход на линию ещё не подтверждён')
        allowed=not blockers and vehicle.get('trip_status')!='completed'
        goal='Подтвердить причину отклонения, состояние ТС и возможность выполнить следующий безопасный шаг.'
        effect={'type':'information','expected':'уточнение фактической причины и доступности следующего действия'}
    elif action=='accelerate_safely':
        if stale:blockers.append('Нет свежей телеметрии')
        if prediction is None or prediction<120:blockers.append('Прогноз не показывает задержку не менее 120 секунд')
        if probability is None or probability<.35:blockers.append('Риск ниже порога активного вмешательства')
        if horizon is not None and horizon<600:blockers.append('До контрольной точки осталось меньше 10 минут')
        allowed=not blockers
        recoverable=round(min(max(0.0,prediction or 0.0),max(30.0,(current or prediction or 0.0)*.25)),1) if allowed else 0.0
        goal='Сократить отставание только в рамках ПДД и подтверждённой дорожной обстановки.'
        effect={'type':'heuristic','recoverable_delay_s':recoverable,'note':'Оценка потенциала, не обещание фактического сокращения.'}
    elif action=='slow_down_safely':
        if stale:blockers.append('Нет свежей телеметрии')
        if prediction is None or prediction>-60:blockers.append('Нет устойчивого сигнала опережения более 60 секунд')
        allowed=not blockers
        goal='Вернуть интервал к плану без создания нового скопления пассажиров.'
        effect={'type':'headway_control','expected':'снижение риска опережения и неравномерного интервала'}
    else:
        allowed=not vehicle.get('trip_status')=='completed'
        goal='Продолжать движение по графику и повторно проверить состояние в заданный момент.'
        effect={'type':'monitoring','expected':'сохранение текущего интервала без активного воздействия'}
    base_check=90 if recommendation.get('priority') in {'critical','high'} else 180 if recommendation.get('priority')=='medium' else 300
    if horizon is not None and horizon>0:
        base_check=min(base_check,max(30.0,horizon-60))
    next_check=(pd.Timestamp.now(tz='Europe/Moscow')+pd.to_timedelta(base_check,unit='s')).isoformat()
    decision=clean({
        'action':action,'allowed':allowed,'status':'ready' if allowed else 'blocked_by_guardrail',
        'priority':recommendation.get('priority'),'goal':goal,'blockers':blockers,
        'evidence':{'prediction_s':prediction,'current_deviation_s':current,'late_probability':probability,'horizon_s':horizon,'expected_delay_s':expected_delay_s,'stale':stale},
        'expected_effect':effect,'next_check_in_s':round(base_check,1),'next_check_at':next_check,
        'recommended_action':recommendation.get('action'),'recommended_title':recommendation.get('title'),
    })
    decision['utility']=action_benefit_model(vehicle,action,decision)
    return decision


def reserve_release_decision(vehicle,placement):
    """Gate a real reserve-release record with explicit operational evidence."""
    blockers=list(placement.get('blockers',[]))
    prediction=vehicle.get('prediction_s')
    probability=vehicle.get('late_probability')
    if prediction is None or float(prediction)<=0:
        blockers.append('Нет положительного прогноза задержки')
    if probability is None or float(probability)<.35:
        blockers.append('Риск задержки ниже порога выпуска резерва (35%)')
    if vehicle.get('degraded'):
        blockers.append('Прогноз работает в fallback-режиме')
    expected_before=None if prediction is None or probability is None else round(max(0.0,float(prediction))*min(1.0,max(0.0,float(probability))),1)
    effect_ratio=float(placement.get('effect_ratio') or 0.0)
    expected_after=None if expected_before is None else round(expected_before*(1-effect_ratio),1)
    return clean({
        'action':'release_reserve','allowed':not blockers,
        'status':'ready' if not blockers else 'blocked_by_guardrail',
        'goal':'Снизить ожидаемое воздействие задержки до контрольной остановки',
        'blockers':blockers,
        'evidence':{'prediction_s':prediction,'late_probability':probability,'expected_delay_s':expected_before,'expected_after_s':expected_after,'effect_ratio':effect_ratio,'horizon_s':placement.get('horizon_s'),'reserve_eta_s':placement.get('reserve_eta_s'),'slack_s':placement.get('slack_s'),'confidence':placement.get('confidence')},
        'expected_effect':{'saved_expected_delay_s':None if expected_before is None else round(expected_before-expected_after,1),'note':'Сценарная оценка на основе геометрии и текущей вероятности, не гарантия фактического результата.'},
    })


def telemetry_fallback_active(now=None):
    """Return whether live data has been unavailable long enough to degrade.

    The check is intentionally based on packet receipt time, not the timestamp
    supplied by a device.  A disconnected emulator can leave an old but valid
    event timestamp in memory; that must not keep the service in live mode.
    """
    if state.get('mode') != 'live':
        return False
    if state.get('ingest_paused'):
        return True
    now = time.time() if now is None else float(now)
    last = float(last_telemetry_received_at or 0.0)
    if not last:
        last = float(runtime_stats.get('started_at', now))
    return now - last >= LIVE_FALLBACK_S


def historical_fallback_state(dispatcher_id=None):
    """Build a safe archive/last-known response while live input is down."""
    merged = {}
    for tr, archived in archive_vehicles.items():
        item = dict(archived)
        live = vehicles.get(tr, {})
        if live.get('position_time') is not None:
            for key in ('lon', 'lat', 'position_time', 'telemetry_source', 'live_track'):
                if key in live:
                    item[key] = live[key]
            item['live_position'] = True
            item['live_position_time'] = live.get('T') or live.get('position_time')
        item.update(
            source='historical_fallback',
            connection_state='historical',
            status_label='Исторические данные · поток недоступен',
            reason='Поток телеметрии недоступен; показано историческое состояние',
            reason_is_hypothesis=True,
            degraded=True,
            stale=False,
            trip_status=item.get('trip_status', 'active'),
            on_route=item.get('on_route', True),
        )
        merged[tr] = item

    # If a vehicle has no archived row, preserve its last known live state and
    # explicitly remove the forecast so it cannot be mistaken for fresh data.
    for tr, live in vehicles.items():
        if tr in merged:
            continue
        item = dict(live)
        item.update(
            source='last_known_live',
            connection_state='offline',
            status_label='Последнее известное состояние',
            reason='Нет связи с эмулятором; показано последнее известное состояние',
            prediction_s=None,
            late_probability=None,
            lower_s=None,
            upper_s=None,
            level='unknown',
            attention_level='critical',
            degraded=True,
            stale=True,
        )
        merged[tr] = item

    output = []
    for item in sorted(merged.values(), key=lambda value: int(value['tr_id'])):
        vehicle = dict(item)
        track = live_track(int(vehicle['tr_id']))
        if track:
            vehicle['live_track'] = track
        vehicle['dispatcher_recommendation']=dispatcher_recommendation(vehicle)
        output.append(clean(vehicle))

    if dispatcher_id:
        profile = get_dispatcher(dispatcher_id)
        if profile is None:
            raise HTTPException(404, 'Dispatcher not found')
        assigned = set(profile['assigned_tr_ids'])
        output = [item for item in output if int(item['tr_id']) in assigned]

    fallback_state = dict(state)
    fallback_state.update(mode='replay', fallback_mode='historical', source_mode='live', snapshot=True)
    return {'vehicles': output, 'state': fallback_state, 'counters': dict(counters),
            'business': business_kpis(output), 'total_points': len(points), 'dispatcher_id': dispatcher_id}


@app.get('/api/state', **operation('state'))
async def get_state(
    dispatcher_id: str | None = None
):

    if telemetry_fallback_active():
        return historical_fallback_state(dispatcher_id)

    if state['mode']=='live':
        ids={int(tr) for tr in schedule.tr_id.unique()}|set(vehicles)
        if dispatcher_id:
            profile=get_dispatcher(dispatcher_id)
            if profile is None:raise HTTPException(404,'Dispatcher not found')
            ids &= set(profile['assigned_tr_ids'])
        now=time.time()
        output=[operational_vehicle(tr,now) for tr in sorted(ids)]
        return {'vehicles':output,'state':state,'business':business_kpis(output),
                'counters':dict(counters),'total_points':len(points),'dispatcher_id':dispatcher_id}

    # Replay retains its historical clock and never raises live connection alarms.
    merged={tr:dict(v) for tr,v in archive_vehicles.items()}
    merged.update({tr:dict(v) for tr,v in vehicles.items()})

    output = []

    for original in sorted(
        merged.values(),
        key=lambda item: int(
            item['tr_id']
        )
    ):
        vehicle = dict(original)

        route=schedule[schedule.tr_id==int(vehicle['tr_id'])].sort_values('ts')
        if not route.empty:
            vehicle['route_start_stop']=display_text(route.iloc[0].building_address)
            vehicle['route_end_stop']=display_text(route.iloc[-1].building_address)

        track = live_track(
            int(
                vehicle['tr_id']
            )
        )

        if track:
            vehicle['live_track'] = track

        vehicle['dispatcher_recommendation']=dispatcher_recommendation(vehicle)

        output.append(vehicle)

    # -------------------------------------------------
    # Ограничение по диспетчеру
    # -------------------------------------------------

    if dispatcher_id:
        profile = get_dispatcher(
            dispatcher_id
        )

        if profile is None:
            raise HTTPException(
                404,
                'Dispatcher not found'
            )

        assigned = set(
            profile[
                'assigned_tr_ids'
            ]
        )

        output = [
            item
            for item in output
            if int(item['tr_id'])
            in assigned
        ]

    return {
        'vehicles': output,
        'state': state,
        'business': business_kpis(output),
        'counters': dict(counters),
        'total_points': len(points),
        'dispatcher_id': dispatcher_id,
    }

@app.get('/api/incidents',**operation('incidents'))
async def get_incidents():
    snapshot=await get_state()
    items=incidents(snapshot['vehicles'])
    return {'items':items,'total':len(items),'as_of':state['clock']}

@app.get('/api/risk',**operation('risk'))
async def get_risk():
    routes=route_risk(list(vehicles.values()))
    return {'routes':routes,'high_routes':sum(r['level']=='high' for r in routes),'medium_routes':sum(r['level']=='medium' for r in routes),'as_of':state['clock']}

@app.post('/api/what-if',**operation('what_if'))
async def what_if(body:WhatIf):
    source_items=list(vehicles.values())
    baseline=route_risk(source_items)
    vehicle=None
    placement=None
    reserve_effect_ratio=0.15
    if body.tr_id is not None:
        vehicle=vehicles.get(int(body.tr_id)) or archive_vehicles.get(int(body.tr_id))
        if vehicle is None:
            raise HTTPException(422,'Unknown vehicle/route for reserve scenario')
        placement=reserve_placement(vehicle)
        if placement is None:
            raise HTTPException(422,'Selected vehicle has no planned geometry')
        if placement.get('feasible'):
            reserve_effect_ratio=float(placement.get('effect_ratio') or 0.0)
    relief=max(0.5,1-reserve_effect_ratio*body.extra_vehicles-body.headway_reduction_pct/100)
    projected=[]
    for item in baseline:
        probability=item['max_late_probability']
        adjusted=None if probability is None else probability*relief
        level='unknown' if adjusted is None else 'high' if adjusted>=.7 else 'medium' if adjusted>=.35 else 'low'
        projected.append({**item,'projected_late_probability':adjusted,'projected_level':level})
    projected_items=[]
    for item in source_items:
        probability=item.get('late_probability')
        adjusted=None if probability is None else float(probability)*relief
        projected_items.append({**item,'late_probability':adjusted,'level':'unknown' if adjusted is None else 'high' if adjusted>=.7 else 'medium' if adjusted>=.35 else 'low'})
    baseline_business=business_kpis(source_items)
    projected_business=business_kpis(projected_items)
    response={'assumptions':{'extra_vehicles':body.extra_vehicles,'headway_reduction_pct':body.headway_reduction_pct,'reserve_effect_ratio':reserve_effect_ratio,'risk_multiplier':relief},'baseline':baseline,'projected':projected,
              'impact':{'baseline':baseline_business,'projected':projected_business,
                        'saved_expected_delay_minutes':round(max(0.0,(baseline_business.get('expected_delay_minutes') or 0)-(projected_business.get('expected_delay_minutes') or 0)),1),
                        'note':'Сценарная оценка: риск масштабируется заданным коэффициентом и не является вторым ML-прогнозом.'}}
    if body.tr_id is None:
        return response
    selected=next((item for item in projected if int(item['tr_id'])==int(body.tr_id)),None)
    if selected is None:
        selected={'tr_id':int(body.tr_id),'level':vehicle.get('level','unknown'),'max_late_probability':vehicle.get('late_probability'),'projected_late_probability':None,'projected_level':'unknown'}
    response.update({
        'selected_tr_id':int(body.tr_id),
        'placement':placement,
        'affected_vehicles':[
            {'tr_id':int(body.tr_id),'role':'Основное ТС','before_prediction_s':placement['before_prediction_s'],'after_prediction_s':placement['after_prediction_s'],'before_current_deviation_s':placement['before_current_deviation_s'],'after_current_deviation_s':placement['after_current_deviation_s'],'before_late_probability':selected.get('max_late_probability'),'after_late_probability':selected.get('projected_late_probability')},
            {'tr_id':-abs(int(body.tr_id)),'role':'Резервное ТС','placement':'На текущем плановом сегменте','target_stop_address':placement['target_stop_address'],'track':placement['track']},
        ],
        'decision':reserve_release_decision(vehicle,placement),
    })
    return response

@app.post('/api/map-match',**operation('map_match'))
async def map_match(body:MapMatch):
    result=match_stop(body.tr_id,body.lon,body.lat)
    if result is None:raise HTTPException(404,'No planned geometry for this route')
    return result

@app.get('/api/driver-commands',**operation('commands'))
async def get_driver_commands(tr_id:int|None=None):
    expire_stale_driver_commands()
    items=stored_driver_commands(tr_id)
    return {'items':items,'channel':'local_dispatch_outbox','external_delivery':False}

@app.get('/api/action-plan',**operation('action_plan'))
async def get_action_plan(tr_id:int,dispatcher_id:str|None=None):
    vehicle=action_vehicle(tr_id)
    if not vehicle:
        raise HTTPException(404,'Unknown vehicle/route for action plan')
    if dispatcher_id:
        dispatcher=get_dispatcher(dispatcher_id)
        if dispatcher is None:
            raise HTTPException(404,'Dispatcher not found')
        if int(tr_id) not in set(dispatcher['assigned_tr_ids']):
            raise HTTPException(403,'Vehicle is not assigned to this dispatcher')
    options=[]
    for action in ('contact','accelerate_safely','maintain','slow_down_safely'):
        decision=driver_decision(vehicle,action)
        options.append({'action':action,'title':DRIVER_ACTION_TITLES[action],'decision':decision,'utility':decision['utility']})
    options.sort(key=lambda item:(not item['decision']['allowed'],-float(item['utility'].get('score') or 0.0)))
    recommended=next((item for item in options if item['decision']['allowed']),options[0])
    return clean({
        'tr_id':int(tr_id),'model':'benefit-v1-rule-based','recommended_action':recommended['action'],
        'recommended_title':recommended['title'],'options':options,
        'evidence':{'prediction_s':vehicle.get('prediction_s'),'late_probability':vehicle.get('late_probability'),'current_deviation_s':vehicle.get('current_deviation_s'),'horizon_s':vehicle.get('horizon_s'),'stale':vehicle.get('stale')},
    })

@app.post('/api/driver-commands',status_code=201,**operation('queue_command'))
async def queue_driver_command(body:DriverCommand):
    dispatcher=get_dispatcher(body.dispatcher_id)
    if dispatcher is None:raise HTTPException(422,'Unknown dispatcher profile')
    vehicle=action_vehicle(body.tr_id)
    if not vehicle:
        raise HTTPException(422,'Unknown vehicle/route for driver action')
    decision=driver_decision(vehicle,body.action)
    command={
        'id':f"cmd-{uuid.uuid4().hex[:12]}",
        'tr_id':body.tr_id,'role':body.role,'dispatcher':dispatcher,'action':body.action,
        'action_title':DRIVER_ACTION_TITLES[body.action],'message':body.message,
        'created_at':pd.Timestamp.now(tz='Europe/Moscow').isoformat(),
        'status':'queued_for_integration' if decision['allowed'] else 'blocked_by_guardrail','channel':'local_dispatch_outbox',
        'external_delivery':False,'simulation_available':decision['allowed'],
        'decision':decision,
    }
    save_driver_command(command)
    if decision['allowed']:
        register_action_case(command,'driver_command')
    return command

@app.post('/api/driver-commands/{command_id}/simulate',**operation('simulate_command'))
async def simulate_driver_command(command_id:str):
    command=stored_driver_command(command_id)
    if command is None:
        raise HTTPException(404,'Driver command not found')
    if command.get('status')=='blocked_by_guardrail':
        raise HTTPException(status_code=409,detail={'message':'Заблокированное guardrail указание нельзя передать симулятору','blockers':command.get('decision',{}).get('blockers',[])})
    if command.get('status')=='simulated_completed':
        return command
    if command.get('status')!='queued_for_integration' or command.get('simulation_available') is False:
        raise HTTPException(status_code=409,detail={'message':'Для этого указания локальная симуляция больше недоступна','status':command.get('status')})
    vehicle=action_vehicle(command['tr_id'])
    if not vehicle:
        raise HTTPException(422,'Vehicle is no longer available for driver simulation')
    simulation=simulate_driver_response(vehicle,command)
    command.update(status='simulated_completed',delivery_mode='local_driver_simulator',external_delivery=False,simulation_available=False,simulated_response=simulation,resolved_at=simulation['simulated_at'])
    update_driver_command(command)
    update_action_case_from_delivery(command,'driver_command')
    return command

@app.get('/api/reserve-dispatches',**operation('reserve_dispatches'))
async def get_reserve_dispatches(tr_id:int|None=None):
    expire_stale_reserve_actions()
    items=stored_reserve_actions(tr_id)
    return {'items':items,'channel':'reserve_integration_outbox','external_execution':False}

@app.post('/api/reserve-dispatches',status_code=201,**operation('reserve_dispatch'))
async def release_reserve(body:ReserveDispatch):
    expire_stale_reserve_actions()
    dispatcher=get_dispatcher(body.dispatcher_id)
    if dispatcher is None:
        raise HTTPException(422,'Unknown dispatcher profile')
    vehicle=action_vehicle(body.tr_id)
    if not vehicle:
        raise HTTPException(422,'Unknown vehicle/route for reserve release')
    placement=reserve_placement(vehicle)
    if placement is None:
        raise HTTPException(422,'Selected vehicle has no planned geometry')
    decision=reserve_release_decision(vehicle,placement)
    if not decision['allowed']:
        raise HTTPException(status_code=409,detail={'message':'Выпуск резерва заблокирован правилами безопасности данных','decision':decision,'placement':placement})
    now=pd.Timestamp.now(tz='Europe/Moscow')
    recent=stored_reserve_actions(body.tr_id,limit=1)
    if recent:
        try:
            if recent[0].get('status')=='queued_for_integration' and (now-pd.Timestamp(recent[0]['created_at'])).total_seconds()<900:
                raise HTTPException(status_code=409,detail={'message':'Для этой линии уже есть активная заявка на резерв','existing_action':recent[0]})
        except HTTPException:
            raise
        except (TypeError,ValueError):
            pass
    action={
        'id':f"reserve-{uuid.uuid4().hex[:12]}",'tr_id':int(body.tr_id),'role':dispatcher['role'],'dispatcher':dispatcher,
        'action':'release_reserve','action_title':'Выпуск резервного ТС','status':'queued_for_integration',
        'channel':'reserve_integration_outbox','external_execution':False,'created_at':now.isoformat(),
        'placement':placement,'decision':decision,
        'message':'Заявка подготовлена после проверки горизонта, ETA резерва, уверенности map matching и риска задержки.',
    }
    save_reserve_action(action)
    register_action_case(action,'reserve_release')
    return action

@app.post('/api/reserve-dispatches/{action_id}/simulate',**operation('simulate_reserve'))
async def simulate_reserve_dispatch(action_id:str):
    action=next((item for item in stored_reserve_actions(limit=2000) if item.get('id')==action_id),None)
    if action is None:
        raise HTTPException(404,'Reserve action not found')
    if action.get('status')=='simulated_completed':
        return action
    if action.get('status')!='queued_for_integration':
        raise HTTPException(status_code=409,detail={'message':'Для этой заявки локальная симуляция больше недоступна','status':action.get('status')})
    simulation=simulate_reserve_response(action)
    action.update(
        status='simulated_completed',delivery_mode='local_fleet_simulator',external_execution=False,
        simulated_response=simulation,resolved_at=simulation['simulated_at'],
    )
    update_reserve_action(action)
    update_action_case_from_delivery(action,'reserve_release')
    return action

@app.get('/api/action-center',**operation('action_center'))
async def get_action_center(dispatcher_id:str):
    expire_stale_driver_commands()
    expire_stale_reserve_actions()
    items=action_center_items(dispatcher_id)
    commands=[command for command in stored_driver_commands(limit=2000) if _case_dispatcher(command)==dispatcher_id]
    reserve_actions=[action for action in stored_reserve_actions(limit=2000) if _case_dispatcher(action)==dispatcher_id]
    reserve_timed_out=sum(action.get('status')=='integration_timeout' for action in reserve_actions)
    status_counts={key:sum(item.get('status')==key for item in items) for key in ACTION_CASE_STATUSES}
    return {
        'items':items,
        'summary':{
            'open':len(items),
            'due':status_counts['worsened']+status_counts['completed_no_result']+status_counts['not_delivered'],
            'blocked':sum(command.get('status')=='blocked_by_guardrail' for command in commands),
            'timed_out':sum(command.get('status')=='integration_timeout' for command in commands)+reserve_timed_out,
            'reserve_timed_out':reserve_timed_out,
            'high_priority':status_counts['worsened'],
            'pending':status_counts['pending'],'success':status_counts['completed_success']+status_counts['improved_independently'],
            'no_result':status_counts['completed_no_result'],'not_delivered':status_counts['not_delivered'],
            'worsened':status_counts['worsened'],
        },
        'as_of':pd.Timestamp.now(tz='Europe/Moscow').isoformat(),
        'dispatcher_id':dispatcher_id,
    }

@app.get('/api/admin/simulation',**operation('simulation_status'))
async def simulation_status():
    return {'role_required':'admin','mode':state['mode'],'supported_scenarios':['normal','slow','stop'],'active_vehicles':len(vehicles),'runs':stored_simulations(limit=10)}

async def custom_emulator_call(path:str, method:str='GET', payload:dict|None=None):
    """Call the built-in emulator through the backend network, fail-soft for UI status."""
    if client is None:
        return {'status':'unavailable','detail':'backend client is not ready'}
    try:
        response=await client.request(method, f'{CUSTOM_EMULATOR_URL}{path}', json=payload, timeout=1.5)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {'status':'unavailable','detail':str(exc)}

def _remember_official_config(config):
    """Keep the last non-empty original-emulator config across a stop."""
    global official_config_cache
    if not config.get('units'):
        return
    official_config_cache=json.loads(json.dumps(config))
    try:
        OFFICIAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFICIAL_CONFIG_PATH.write_text(json.dumps(official_config_cache, ensure_ascii=False), encoding='utf-8')
    except OSError:
        # Runtime control must remain available even when the optional state
        # volume is read-only.
        pass


def _cached_official_config():
    if official_config_cache:
        return json.loads(json.dumps(official_config_cache))
    try:
        payload=json.loads(OFFICIAL_CONFIG_PATH.read_text(encoding='utf-8'))
        return payload if payload.get('units') else None
    except (OSError, ValueError):
        return None


def _default_official_config():
    """Return a small, valid config for a fresh prototype installation.

    The upstream image starts with an empty in-memory ``units`` list.  That
    made the admin "Запустить поток" action look broken until somebody knew
    to post a private config to the image first.  Seed all mapped original
    units on demand; operators can still replace them through the image API.
    """
    scheduled_ids = {
        int(item) for item in schedule.tr_id.unique()
        if schedule is not None and int(item) < CUSTOM_TR_ID_OFFSET
    } if schedule is not None else set()
    original_ids = sorted(
        int(unit_id) for unit_id, tr_id in mapping.items()
        if int(unit_id) < CUSTOM_UNIT_ID_OFFSET and int(tr_id) in scheduled_ids
    )
    if not original_ids:
        original_ids = [985940]
    official_host = urllib.parse.urlparse(OFFICIAL_EMULATOR_URL).hostname or ''
    if official_host in {'emulator', 'official-emulator'}:
        target_host = 'backend'
    elif official_host in {'localhost', '127.0.0.1', '::1'}:
        target_host = '127.0.0.1'
    else:
        target_host = os.getenv('OFFICIAL_TARGET_HOST', 'host.docker.internal')
    return {
        'targetHost': target_host,
        'targetPort': int(os.getenv('NDTP_PORT', '9201')),
        'units': [{
            'unitId': unit_id,
            'intervalMs': 3000,
            'autoGenerate': True,
            'cells': [],
        } for unit_id in original_ids],
    }


def _normalized_official_config(current=None, running=True):
    """Return the canonical 13-unit config required by the prototype.

    The upstream image keeps its config in memory.  A manual one-unit config
    therefore survives until the next restart and makes the remaining mapped
    vehicles look like they are waiting forever.  The prototype has one
    deterministic original fleet: rebuild the unit list from the mapping,
    while retaining the current target host/port when the image supplied them.
    """
    default=_default_official_config()
    current=current or {}
    config={**default}
    if current.get('targetHost'):
        config['targetHost']=current['targetHost']
    if current.get('targetPort'):
        config['targetPort']=current['targetPort']
    config['units']=[
        {**unit,'autoGenerate':bool(running)}
        for unit in default.get('units',[])
    ]
    return config


async def official_emulator_config(enabled:bool|None=None):
    """Read or toggle the original image generator through its config API.

    The official image stops by replacing its in-memory ``units`` with an
    empty list.  We retain the last valid config locally so a later resume
    restores exactly the same devices instead of posting an empty config.
    """
    if client is None:
        return {'status':'unavailable','detail':'backend client is not ready'}
    try:
        response=await client.get(f'{OFFICIAL_EMULATOR_URL}/api/config',timeout=1.5)
        response.raise_for_status(); config=response.json()
        if config.get('units'):
            _remember_official_config(config)
        cached=_cached_official_config()
        if enabled is not None:
            if enabled:
                # A cached one-unit config is an old/partial setup.  Resume
                # always restores the complete official fleet.
                config=_normalized_official_config(config if config.get('units') else cached, running=True)
            else:
                _remember_official_config(config)
                config={**config,'units':[]}
            response=await client.post(f'{OFFICIAL_EMULATOR_URL}/api/config',json=config,timeout=1.5)
            response.raise_for_status(); config=response.json() if response.content else config
        else:
            units=config.get('units',[])
            expected=_default_official_config().get('units',[])
            expected_ids={int(unit['unitId']) for unit in expected}
            current_ids={int(unit.get('unitId')) for unit in units if unit.get('unitId') is not None}
            if not units and cached is None:
                # Fresh image: seed and start the mandatory 13-unit source.
                config=_normalized_official_config(None,running=True)
                response=await client.post(f'{OFFICIAL_EMULATOR_URL}/api/config',json=config,timeout=1.5)
                response.raise_for_status(); config=response.json() if response.content else config
            elif units and current_ids != expected_ids:
                # Existing partial config: repair it without waiting for a
                # manual admin action.  Preserve whether its stream was live.
                running=any(item.get('autoGenerate') for item in units)
                config=_normalized_official_config(config,running=running)
                response=await client.post(f'{OFFICIAL_EMULATOR_URL}/api/config',json=config,timeout=1.5)
                response.raise_for_status(); config=response.json() if response.content else config
        units=config.get('units',[])
        if units:
            _remember_official_config(config)
        status='running' if any(item.get('autoGenerate') for item in units) else ('paused' if units else ('not_configured' if not _cached_official_config() else 'paused'))
        return {'status':status,'units':len(units)}
    except (httpx.HTTPError, ValueError) as exc:
        return {'status':'unavailable','detail':str(exc)}


async def warm_official_source():
    """Seed and start the mandatory official source when its container is ready."""
    for _ in range(12):
        # A previous pause is persisted so that the source can be resumed with
        # the same connection settings.  Prototype startup, however, is a new
        # run and must always bring the original emulator online.
        status=await official_emulator_config(True)
        if status.get('status')!='unavailable':
            return status
        await asyncio.sleep(1)
    return {'status':'unavailable','detail':'official emulator did not become ready during startup warm-up'}

@app.get('/api/admin/emulators',**operation('emulator_status'))
async def emulator_status():
    custom=await custom_emulator_call('/status')
    official=await official_emulator_config()
    active=[run for run in simulation_runs.values() if run.get('status') in {'queued','running'}]
    return {'ingest_paused':bool(state.get('ingest_paused')),'sources':[{
        'id':'custom-emulator','label':'custom-emulator','kind':'built_in_ndtp','control':'pause_resume',
        'status':'paused' if custom.get('paused') else ('running' if custom.get('connected') else custom.get('status','unknown')),
        'details':custom,
    },{
        'id':'official-emulator','label':'Оригинальный NDTP-эмулятор','kind':'external_image','control':'config_api',
        'status':official.get('status','unavailable'),'details':official,
    }],'active_simulations':len(active)}

async def cancel_active_simulations(operator_id='admin-01'):
    for run in simulation_runs.values():
        if run.get('status') in {'queued','running'}:
            run['cancel_requested']=True
            run['cancelled_by']=operator_id
            save_simulation(run)

@app.post('/api/admin/emulators/{emulator_id}/{action}',**operation('emulator_control'))
async def emulator_control(emulator_id:str,action:str,body:EmulatorControl):
    operator=get_dispatcher(body.dispatcher_id)
    if operator is None:
        raise HTTPException(403,'Valid dispatcher profile required')
    if emulator_id not in {'custom-emulator','official-emulator','all'} or action not in {'pause','resume','stop'}:
        raise HTTPException(404,'Unknown emulator control')
    paused=action in {'pause','stop'}
    if emulator_id=='all':
        state['ingest_paused']=paused
        await cancel_active_simulations(operator['id']) if paused else None
    custom=await custom_emulator_call('/pause' if paused else '/resume','POST') if emulator_id in {'custom-emulator','all'} else {'status':'unchanged'}
    official=await official_emulator_config(not paused) if emulator_id in {'official-emulator','all'} else {'status':'unchanged'}
    return {'accepted':True,'emulator_id':emulator_id,'action':action,'ingest_paused':bool(state.get('ingest_paused')),'custom':custom,'official':official}

@app.post('/api/debug/custom-emulator/speed',**operation('debug_speed'))
async def set_debug_speed(body:DebugSpeedOverride):
    """Forward a temporary speed overlay without changing dispatcher logic."""
    if get_dispatcher(body.dispatcher_id) is None:
        raise HTTPException(403,'Valid dispatcher profile required')
    if body.tr_id < CUSTOM_TR_ID_OFFSET:
        raise HTTPException(409,'Debug speed is available only for custom-emulator vehicles')
    result=await custom_emulator_call(
        f'/debug/vehicles/{body.tr_id}/speed',
        'POST',
        {'speed_kmh':body.speed_kmh},
    )
    if result.get('status')=='unavailable':
        raise HTTPException(503,result.get('detail','Custom emulator unavailable'))
    return {**result,'dispatcher_id':body.dispatcher_id}

@app.delete('/api/debug/custom-emulator/speed/{tr_id}',**operation('debug_speed'))
async def clear_debug_speed(tr_id:int, body:EmulatorControl):
    if get_dispatcher(body.dispatcher_id) is None:
        raise HTTPException(403,'Valid dispatcher profile required')
    if tr_id < CUSTOM_TR_ID_OFFSET:
        raise HTTPException(409,'Debug speed is available only for custom-emulator vehicles')
    result=await custom_emulator_call(f'/debug/vehicles/{tr_id}/speed','DELETE')
    if result.get('status')=='unavailable':
        raise HTTPException(503,result.get('detail','Custom emulator unavailable'))
    return {**result,'dispatcher_id':body.dispatcher_id}

async def execute_simulation(run):
    run['status']='running';run['started_at']=pd.Timestamp.now(tz='Europe/Moscow').isoformat();save_simulation(run)
    try:
        current=vehicles.get(run['tr_id']) or archive_vehicles.get(run['tr_id'])
        if current and current.get('lon') is not None:
            lon,lat=float(current['lon']),float(current['lat'])
        else:
            route=schedule[schedule.tr_id==run['tr_id']].dropna(subset=['lon','lat'])
            if route.empty:raise ValueError('No coordinates for this vehicle')
            lon,lat=float(route.iloc[0].lon),float(route.iloc[0].lat)
        before=clean(vehicles.get(run['tr_id']) or archive_vehicles.get(run['tr_id']) or {})
        speed={'normal':35,'slow':8,'stop':0}[run['scenario']]
        now=time.time()-run['count']*run['interval_s']
        for index in range(run['count']):
            if run.get('cancel_requested'):
                run['status']='cancelled';run['cancelled_at']=pd.Timestamp.now(tz='Europe/Moscow').isoformat();save_simulation(run)
                return
            event={'tr_id':run['tr_id'],'event_time':timestamp(now+index*run['interval_s']).isoformat(),'lon':lon+index*0.00005,'lat':lat+index*0.00003,'speed':speed,'location_valid':True,'simulated':True,'simulation_id':run['id']}
            await ingest(event)
            run['events'].append({'sequence':index+1,'event_time':event['event_time'],'lon':event['lon'],'lat':event['lat'],'speed_kmh':speed,'accepted':True,'simulated':True})
            run['progress']={'completed':index+1,'total':run['count']}
            save_simulation(run)
            await asyncio.sleep(.05)
        after=clean(vehicles.get(run['tr_id']) or archive_vehicles.get(run['tr_id']) or {})
        run['effect']={'before':{'prediction_s':before.get('prediction_s'),'late_probability':before.get('late_probability'),'level':before.get('level')},'after':{'prediction_s':after.get('prediction_s'),'late_probability':after.get('late_probability'),'level':after.get('level')}}
        run['status']='completed';run['completed_at']=pd.Timestamp.now(tz='Europe/Moscow').isoformat();save_simulation(run)
    except Exception as exc:
        run['status']='failed';run['error']=str(exc);run['completed_at']=pd.Timestamp.now(tz='Europe/Moscow').isoformat();save_simulation(run)
    finally:
        simulation_tasks.pop(run['id'],None)

@app.post('/api/admin/simulations',status_code=202,**operation('create_simulation'))
async def create_simulation(body:SimulationCreate):
    operator=get_dispatcher(body.dispatcher_id)
    if operator is None or operator['role']!='Администратор':raise HTTPException(403,'Admin account required')
    if state['mode']!='live':raise HTTPException(409,'Switch to live mode before starting simulation')
    run={'id':f"sim-{uuid.uuid4().hex[:10]}",'status':'queued','created_at':pd.Timestamp.now(tz='Europe/Moscow').isoformat(),'operator':operator,'tr_id':body.tr_id,'scenario':body.scenario,'count':body.count,'interval_s':body.interval_s,'progress':{'completed':0,'total':body.count},'events':[],'effect':None}
    simulation_runs[run['id']]=run;save_simulation(run)
    task=asyncio.create_task(execute_simulation(run));simulation_tasks[run['id']]=task
    return run

@app.get('/api/admin/simulations',**operation('list_simulations'))
async def list_simulations(status:str|None=None,tr_id:int|None=None,limit:int=50):
    if status is not None and status not in {'queued','running','completed','cancelled','failed'}:raise HTTPException(422,'Unknown simulation status filter')
    if tr_id is not None and tr_id<=0:raise HTTPException(422,'tr_id must be positive')
    if not 1<=limit<=200:raise HTTPException(422,'limit must be 1..200')
    return {'items':stored_simulations(status,tr_id,limit),'mode':state['mode'],'filters':{'status':status,'tr_id':tr_id,'limit':limit}}

@app.get('/api/admin/simulations/{run_id}',**operation('get_simulation'))
async def get_simulation(run_id:str):
    run=simulation_runs.get(run_id)
    if run is None:
        row=db.execute('SELECT payload FROM simulations WHERE id=?',(run_id,)).fetchone()
        if row is None:raise HTTPException(404,'Simulation not found')
        return json.loads(row['payload'])
    return run

@app.post('/api/admin/simulations/{run_id}/cancel',**operation('cancel_simulation'))
async def cancel_simulation(run_id:str,body:SimulationCancel):
    operator=get_dispatcher(body.dispatcher_id)
    if operator is None or operator['role']!='Администратор':raise HTTPException(403,'Admin account required')
    run=simulation_runs.get(run_id)
    if run is None:
        row=db.execute('SELECT payload FROM simulations WHERE id=?',(run_id,)).fetchone()
        if row is None:raise HTTPException(404,'Simulation not found')
        run=json.loads(row['payload'])
        if run.get('status') in {'completed','cancelled','failed'}:raise HTTPException(409,'Simulation is already terminal')
        raise HTTPException(409,'Simulation is not active in this backend process')
    if run['status'] in {'completed','cancelled','failed'}:raise HTTPException(409,'Simulation is already terminal')
    run['cancel_requested']=True;run['cancel_requested_at']=pd.Timestamp.now(tz='Europe/Moscow').isoformat();run['cancelled_by']=operator['id'];save_simulation(run)
    return run

@app.post('/api/admin/simulation',**operation('legacy_simulation'))
async def admin_simulation(body:AdminSimulation):
    if body.role!='admin':raise HTTPException(403,'Admin role required')
    if state['mode']!='live':raise HTTPException(409,'Switch to live mode before starting admin simulation')
    current=vehicles.get(body.tr_id)
    if current and current.get('lon') is not None:
        lon,lat=float(current['lon']),float(current['lat'])
    else:
        route=schedule[schedule.tr_id==body.tr_id].dropna(subset=['lon','lat'])
        if route.empty:raise HTTPException(404,'No coordinates for this route')
        lon,lat=float(route.iloc[0].lon),float(route.iloc[0].lat)
    speed={'normal':35,'slow':8,'stop':0}[body.scenario]
    accepted=0;now=time.time()-body.count*body.interval_s
    for index in range(body.count):
        event={'tr_id':body.tr_id,'event_time':timestamp(now+index*body.interval_s).isoformat(),'lon':lon+index*0.00005,'lat':lat+index*0.00003,'speed':speed,'location_valid':True}
        await ingest(event);accepted+=1
    return {'accepted':accepted,'scenario':body.scenario,'tr_id':body.tr_id,'role':'admin','message':'Simulation events injected into live pipeline'}

@app.get('/api/network',**operation('network'))
async def network():
    # No route IDs in source: expose planned stop sequences, explicitly labeled.
    paths=[]
    for tr,g in schedule.groupby('tr_id'):
        g=g.dropna(subset=['lon','lat']).head(500)
        paths.append({'tr_id':int(tr),'points':g[['lon','lat']].values.tolist()})
    return {'kind':'planned_stop_sequences','paths':paths}

@app.get('/api/metrics',**operation('metrics'))
async def metrics():
    legacy=json.loads((ARTIFACT/'metrics.json').read_text(encoding='utf-8'))
    model_meta=json.loads((ARTIFACT/'model.json').read_text(encoding='utf-8')) if (ARTIFACT/'model.json').exists() else {}
    predictions=pd.read_csv(ARTIFACT/'test_predictions.csv') if (ARTIFACT/'test_predictions.csv').exists() else pd.DataFrame()
    mae_report=legacy.get('mae_test',{})
    leaderboard=legacy.get('leaderboard',{})
    v5=dict(model='v5',version=model_meta.get('version'),target_mode=model_meta.get('target_mode'),ensemble=bool(model_meta.get('ensemble',False)),features=len(model_meta.get('features',[])),test_points=len(predictions),mae_s=mae_report.get('catboost',legacy.get('test_mae_s')),baseline_mae_s=mae_report.get('cur_dev_s',legacy.get('persistence_mae_s')),leaderboard_score=leaderboard.get('score',legacy.get('leaderboard_score')),leaderboard_reference=leaderboard.get('commit'),interval_radius_s=model_meta.get('interval_radius_s'),coverage=legacy.get('interval_coverage'),late_threshold_s=model_meta.get('late_threshold_s',120),batch_inference_ms=legacy.get('batch_inference_ms'))
    if v5['mae_s'] is not None and v5['baseline_mae_s']:
        v5['improvement_pct']=100*(1-v5['mae_s']/v5['baseline_mae_s'])
    importance=[]
    importance_path=ARTIFACT/'feature_importance.csv'
    if importance_path.exists():
        importance=pd.read_csv(importance_path).sort_values('importance',ascending=False).head(6).to_dict('records')
    return {**legacy,'v5':v5,'feature_importance':importance,'readable':{'mae':f"{v5['mae_s']:.1f} с" if v5['mae_s'] is not None else '—','baseline':f"{v5['baseline_mae_s']:.1f} с" if v5['baseline_mae_s'] is not None else '—','coverage':f"{100*v5['coverage']:.1f}%" if v5['coverage'] is not None else '—'}}

app.mount('/static',StaticFiles(directory=ROOT/'dashboard'),name='static')

@app.get('/code/',include_in_schema=False)
async def code_documentation_index():
    """Serve the generated Sphinx landing page before the static mount."""
    return FileResponse(ROOT/'docs'/'_build'/'html'/'index.html')

@app.get('/docs',include_in_schema=False)
async def documentation():
    """Serve the human-readable API guide without modifying the dashboard UI."""
    return FileResponse(ROOT/'dashboard/docs.html')

app.mount(
    "/code",
    StaticFiles(
        directory=ROOT / "docs" / "_build" / "html",
        html=True,
        check_dir=False,
    ),
    name="code_docs",
)

@app.get('/',include_in_schema=False)
async def index():return FileResponse(ROOT/'dashboard/dispatcher.html')

@app.get('/admin',include_in_schema=False)
async def admin_page():return FileResponse(ROOT/'dashboard/admin-control.html')
