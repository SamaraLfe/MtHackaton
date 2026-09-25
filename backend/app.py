"""Dispatcher backend: NDTP/JSON -> causal features -> independent ML service."""
import asyncio, json, logging, math, os, sqlite3, time, uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from ml.features import build_one, epoch, timestamp, load_traffic, load_schedule, haversine
from backend.ndtp import handle
from backend.api_docs import DESCRIPTION, TAGS, operation

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.getenv('DATA_DIR','dataset'));ARTIFACT=Path(os.getenv('ARTIFACT_DIR','artifacts'))
ML_URL=os.getenv('ML_URL','http://127.0.0.1:8001')
LIVE_TRACK_TTL_S=int(os.getenv('LIVE_TRACK_TTL_S','3600'))
history=defaultdict(lambda:deque(maxlen=1000));vehicles={};archive_vehicles={};counters=defaultdict(int)
deviations={};last_forecast={};state={'mode':'replay','clock':None,'index':0,'snapshot':False}
schedule=None;schedule_template=None;live_schedule_day=None;traffic=None;points=None;mapping={};client=None;lock=asyncio.Lock()
DB_PATH=Path(os.getenv('STATE_DIR','state'))/'dispatcher.db';db=None
simulation_runs={};simulation_tasks={}
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
    """Locally queued instruction; it is never delivered outside the MVP."""
    role:str=Field(pattern='^(dispatcher|admin)$',description='Заявленная роль автора записи.',examples=['dispatcher'])
    dispatcher_id:str=Field(pattern='^dispatcher-[a-z0-9]{2,40}$',description='Существующий локальный профиль диспетчера.',examples=['dispatcher-01'])
    tr_id:int=Field(gt=0,description='Получатель указания.',examples=[131672])
    action:str=Field(pattern='^(contact|maintain|accelerate_safely|slow_down_safely)$',description='contact, maintain, accelerate_safely или slow_down_safely.',examples=['contact'])
    message:str=Field(min_length=5,max_length=300,description='Текст локального указания.',examples=['Уточните причину задержки и текущую обстановку.'])

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

def _account(row):
    return {'id':row['id'],'name':row['name'],'login':row['login'],'role':row['role'],'status':row['status']}

def init_store():
    global db
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
    ''')
    seeds=[('admin-01','Администратор','admin','Администратор'),('dispatcher-01','Диспетчер №01','dispatcher01','Маршрутный диспетчер'),('dispatcher-02','Диспетчер №02','dispatcher02','Старший диспетчер')]
    for account in seeds:
        db.execute('INSERT OR IGNORE INTO dispatchers(id,name,login,role,status,created_at) VALUES(?,?,?,?,?,?)',(*account,'active',pd.Timestamp.now(tz='Europe/Moscow').isoformat()))
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
    """Read durable scenario history with conservative server-side filters."""
    rows=db.execute('SELECT payload FROM simulations ORDER BY created_at DESC').fetchall()
    items=[json.loads(row['payload']) for row in rows]
    if status: items=[item for item in items if item.get('status')==status]
    if tr_id is not None: items=[item for item in items if item.get('tr_id')==tr_id]
    return items[:limit]

def save_driver_command(command):
    """Persist an auditable local outbox command before returning it to the UI."""
    db.execute('INSERT INTO driver_commands(id,tr_id,payload,created_at) VALUES(?,?,?,?)',
               (command['id'],command['tr_id'],json.dumps(clean(command),ensure_ascii=False),command['created_at']))
    db.commit()

def stored_driver_commands(tr_id=None,limit=200):
    """Return durable outbox records newest first; no delivery is implied."""
    query='SELECT payload FROM driver_commands'
    params=[]
    if tr_id is not None:
        query+=' WHERE tr_id=?';params.append(tr_id)
    query+=' ORDER BY created_at DESC LIMIT ?';params.append(limit)
    return [json.loads(row['payload']) for row in db.execute(query,params)]

def live_track(tr_id,limit=100):
    """Return recent valid GPS points, preserving simulated-point provenance."""
    rows=[row for row in history[tr_id] if row.get('location_valid') and np.isfinite(row.get('lon',np.nan)) and np.isfinite(row.get('lat',np.nan))]
    if rows:
        cutoff=max(float(row['ts']) for row in rows)-LIVE_TRACK_TTL_S
        rows=[row for row in rows if float(row['ts'])>=cutoff]
    return [clean({'lon':row['lon'],'lat':row['lat'],'event_time':timestamp(row['event_time']).isoformat(),
                   'simulated':bool(row.get('simulated',False))}) for row in rows[-limit:]]

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

def target_for(tr,t,stop_id=None):
    candidates=schedule[(schedule.tr_id==tr)&(schedule.ts>t+600)&(schedule.ts<=t+900)]
    if candidates.empty:return None
    first_time=candidates.ts.min()
    candidates=candidates[candidates.ts==first_time]
    if stop_id is not None:candidates=candidates[candidates.tt_action_item_id==stop_id]
    return None if candidates.empty else candidates.sort_values('tt_action_item_id').iloc[0].to_dict()

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

def load_historical_snapshot():
    """Build the dispatcher default view from the latest archived point per route."""
    vehicles.clear();archive_vehicles.clear();history.clear();deviations.clear();last_forecast.clear()
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
        prediction=float(point.cur_dev_s)
        probability=float(1/(1+np.exp(-(prediction-120)/45)))
        level='high' if probability>=.7 else 'medium' if probability>=.35 else 'low'
        address='Контрольная точка не указана' if pd.isna(stop.building_address) else str(stop.building_address)
        snapshot_vehicle=dict(
            tr_id=tr,T=timestamp(event_time).isoformat(),target_time_begin=timestamp(point.target_time_begin).isoformat(),
            target_stop_id=int(point.target_stop_id),stop_address=address,prediction_s=prediction,
            previous_stop='Контрольная точка не указана' if pd.isna(previous) else str(previous),next_stop='Контрольная точка не указана' if pd.isna(following) else str(following),
            lower_s=prediction-104.78,upper_s=prediction+104.78,late_probability=probability,level=level,
            reason='Историческое отклонение по архивной телеметрии',recommendation='Связаться с водителем и уточнить обстановку' if level in {'high','medium'} else 'Продолжить наблюдение',
            source='historical_archive',model='v5',degraded=False,stale=False,position_time=float(last.ts) if last is not None else None,
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

def match_stop(tr_id, lon, lat):
    candidates=schedule[schedule.tr_id==tr_id].dropna(subset=['lon','lat']).sort_values('ts')
    if candidates.empty:return None
    if len(candidates)==1:
        row=candidates.iloc[0];distance=haversine(lon,lat,row.lon,row.lat)
        return {'tr_id':int(tr_id),'match_kind':'planned_stop','road_graph_matched':False,'stop_id':int(row.tt_action_item_id),'stop_address':str(row.building_address),'distance_m':round(distance,2),'segment_index':0,'next_stop_id':int(row.tt_action_item_id),'confidence':round(max(0.,1-distance/250),3)}
    best=None
    rows=list(candidates.itertuples())
    for position,(start,end) in enumerate(zip(rows,rows[1:])):
        fraction,projected_lon,projected_lat,distance=_segment_projection(lon,lat,start.lon,start.lat,end.lon,end.lat)
        if best is None or distance<best['distance']:
            heading=(math.degrees(math.atan2((end.lon-start.lon)*math.cos(math.radians((start.lat+end.lat)/2)),end.lat-start.lat))+360)%360
            best={'position':position,'start':start,'end':end,'fraction':fraction,'lon':projected_lon,'lat':projected_lat,'distance':distance,'heading':heading}
    start,end=best['start'],best['end']
    nearest=start if best['fraction']<.5 else end
    distance_to_next=haversine(best['lon'],best['lat'],end.lon,end.lat)
    return {'tr_id':int(tr_id),'match_kind':'planned_trajectory_segment','road_graph_matched':False,
            'stop_id':int(nearest.tt_action_item_id),'stop_address':str(nearest.building_address),
            'distance_m':round(best['distance'],2),'segment_index':best['position'],
            'segment_start_stop_id':int(start.tt_action_item_id),'next_stop_id':int(end.tt_action_item_id),
            'projected_lon':round(best['lon'],7),'projected_lat':round(best['lat'],7),
            'distance_to_next_stop_m':round(distance_to_next,2),'direction_degrees':round(best['heading'],1),
            'direction':'along_planned_trajectory','confidence':round(max(0.,1-best['distance']/250),3)}

async def forecast(point,records,source):
    t=epoch(point['T']);tr=int(point['tr_id'])
    stop=target_for(tr,t,int(point['target_stop_id']))
    if stop is None or int(stop['tt_action_item_id'])!=int(point['target_stop_id']) or abs(stop['ts']-epoch(point['target_time_begin']))>1:
        raise ValueError('Точка не соответствует первой остановке в окне (T+10, T+15]')
    features=build_one(point,records,stop)
    start=time.perf_counter();degraded=False
    try:
        response=await client.post(ML_URL+'/predict_v5',json={'points':[clean(point)],'histories':[[clean(x) for x in records]],'schedules':[[clean(x) for x in schedule[(schedule.tr_id==tr)&(schedule.ts>=t-1800)&(schedule.ts<=t+1800)].to_dict('records')]]});response.raise_for_status()
        result=response.json()['predictions'][0]
        result.update(lower_s=result['prediction_s']-104.7845011097,upper_s=result['prediction_s']+104.7845011097,late_probability=float(1/(1+np.exp(-(result['prediction_s']-120)/45))))
    except (httpx.HTTPError,KeyError,ValueError):
        counters['ml_failures']+=1;degraded=True
        result=dict(prediction_s=point['cur_dev_s'],lower_s=None,upper_s=None,late_probability=None,model='persistence_fallback')
    counters['last_inference_ms']=round((time.perf_counter()-start)*1000,2)
    stale=features['age_s']>120 or bool(features['missing_gps'])
    risk=result['late_probability']
    level='unknown' if degraded or stale else ('high' if risk>=.7 else 'medium' if risk>=.35 else 'low')
    reason='Устойчивое отклонение от графика'
    if features['idle_s']>=90:reason='Длительная остановка: возможный простой'
    elif features['speed_trend']<-2:reason='Снижение скорости за последние 10 минут'
    if stale:reason='Нет свежей достоверной телеметрии'
    valid=[r for r in records if r.get('location_valid') and r['ts']<=t and np.isfinite(r.get('lon',np.nan)) and np.isfinite(r.get('lat',np.nan))]
    last=max(valid,key=lambda r:r['ts']) if valid else None
    result.update(tr_id=tr,T=timestamp(point['T']).isoformat(),target_time_begin=timestamp(point['target_time_begin']).isoformat(),target_stop_id=int(stop['tt_action_item_id']),
      stop_address=str(stop['building_address']),level=level,reason=reason,reason_is_hypothesis=True,
      recommendation='Проверить ситуацию с водителем и доступность резерва' if level=='high' else 'Наблюдать за движением',
      source=source,degraded=degraded,stale=stale,features=features,lon=last['lon'] if last else None,lat=last['lat'] if last else None,
      position_time=last['ts'] if last else None,horizon_s=epoch(point['target_time_begin'])-t)
    vehicles[tr]=clean(result);counters['predictions']+=1
    return clean(result)

async def ingest(event):
    global schedule,live_schedule_day
    tr=event['tr_id'];ts=epoch(event['event_time'])
    if ts>time.time()+60: raise ValueError('Телеметрия из будущего')
    event_day=timestamp(event['event_time']).date()
    if state['mode']=='live' and live_schedule_day!=event_day:
        schedule=align_schedule_to_event_day(schedule, event['event_time'])
        live_schedule_day=event_day
    records=history[tr]
    if records and ts<=records[-1]['ts']:
        counters['late_or_duplicate_packets']+=1;return
    row=dict(event,ts=ts);records.append(row);counters['telemetry_rows']+=1
    if state['mode']!='live':
        # The historical forecast stays visible by default. Only a fresh NDTP
        # coordinate can overlay it, so delayed source traffic never looks live.
        if event['location_valid'] and ts>=time.time()-LIVE_TRACK_TTL_S:
            vehicles[tr]={
                'tr_id':tr,'T':timestamp(event['event_time']).isoformat(),
                'level':'unknown','reason':'Live NDTP: позиция получена; прогноз остаётся историческим',
                'source':'live','prediction_s':None,'late_probability':None,
                'lon':event['lon'],'lat':event['lat'],'position_time':ts,
            }
        return
    state['clock']=timestamp(event['event_time']).isoformat()
    # Conservative arrival match: only already observed stop proximity. No schedule actuals.
    known=schedule[(schedule.tr_id==tr)&(schedule.ts>=ts-1800)&(schedule.ts<=ts+300)]
    if event['location_valid'] and event['speed']<5 and not known.empty:
        dist=known.apply(lambda s:haversine(event['lon'],event['lat'],s.lon,s.lat),axis=1)
        nearest=known.loc[dist.idxmin()]
        if dist.min()<60 and deviations.get(tr,{}).get('stop')!=int(nearest.tt_action_item_id):
            deviations[tr]={'stop':int(nearest.tt_action_item_id),'value':ts-nearest.ts}
    if ts-last_forecast.get(tr,0)<30:return
    last_forecast[tr]=ts
    stop=target_for(tr,ts)
    if stop is None:
        vehicles[tr]=dict(tr_id=tr,T=timestamp(event['event_time']).isoformat(),level='unknown',reason='Нет плановой остановки через 10–15 минут',lon=event['lon'] if event['location_valid'] else None,lat=event['lat'] if event['location_valid'] else None,source='live',prediction_s=None,late_probability=None,position_time=ts)
        return
    point=dict(tr_id=tr,T=event['event_time'],target_stop_id=int(stop['tt_action_item_id']),target_time_begin=stop['time_begin'],cur_dev_s=deviations.get(tr,{}).get('value',0))
    result=await forecast(point,list(records),'live')
    if tr not in deviations:
        result.update(level='unknown',reason='Нет подтверждённого текущего отклонения',deviation_estimated=True)
        vehicles[tr]=result

async def on_ndtp(event):
    unit=event.pop('unit_id');tr=mapping.get(unit)
    if tr is None: counters['unknown_units']+=1;return
    event['tr_id']=tr
    try:
        validated=Telemetry(**event)
        async with lock: await ingest(validated.model_dump())
    except ValueError: counters['invalid_events']+=1

@asynccontextmanager
async def lifespan(app):
    global schedule,schedule_template,traffic,points,mapping,client
    init_store()
    schedule=load_schedule(DATA/'validate/schedule_plan.csv')
    schedule_template=schedule.copy()
    all_ids=sorted({int(item) for item in schedule.tr_id.unique()})
    for dispatcher_id,parity in (('dispatcher-01',0),('dispatcher-02',1)):
        if db.execute('SELECT COUNT(*) AS count FROM assignments WHERE dispatcher_id=?',(dispatcher_id,)).fetchone()['count']==0:
            db.executemany('INSERT OR IGNORE INTO assignments(dispatcher_id,tr_id) VALUES(?,?)',[(dispatcher_id,tr_id) for index,tr_id in enumerate(all_ids) if index%2==parity])
    db.commit()
    traffic=load_traffic(DATA/'validate/traffic.csv')
    points=pd.read_csv(DATA/'validate/points.csv').sort_values('T').reset_index(drop=True)
    ids=pd.read_csv(DATA/'validate/traffic.csv',usecols=['unit_id','tr_id']).drop_duplicates()
    mapping={int(r.unit_id):int(r.tr_id) for r in ids.itertuples()}
    mapping.update({int(k):int(v) for k,v in json.loads(os.getenv('UNIT_MAP','{}')).items()})
    client=httpx.AsyncClient(timeout=2)
    load_historical_snapshot()
    server=await asyncio.start_server(lambda r,w:handle(r,w,on_ndtp,counters),'0.0.0.0',int(os.getenv('NDTP_PORT','9201')))
    yield
    server.close();await server.wait_closed();await client.aclose()
    if db is not None:db.close()

app=FastAPI(title='Такт — Backend API',version='1.0.0',lifespan=lifespan,
    description=DESCRIPTION,openapi_tags=TAGS,docs_url='/docs/swagger',
    swagger_ui_parameters={'docExpansion':'none','displayRequestDuration':True,'filter':True})

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
            'artifacts':(ARTIFACT/'metrics.json').exists() and (ARTIFACT/'model_v5.json').exists()}
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

@app.post('/api/telemetry',**operation('telemetry'))
async def telemetry(events:list[Telemetry]):
    if not 1<=len(events)<=1000:raise HTTPException(422,'Batch size must be 1..1000')
    try:
        # Validate all timestamps before mutating the buffer.
        for e in events:
            if epoch(e.event_time)>time.time()+60:raise ValueError('Future timestamp')
        async with lock:
            for e in events:await ingest(e.model_dump())
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
    global schedule,live_schedule_day
    async with lock:
        schedule=schedule_template.copy();live_schedule_day=None
        if body.mode=='replay':load_historical_snapshot()
        else:
            state.update(mode='live',index=0,clock=None,snapshot=False);vehicles.clear();history.clear();deviations.clear();last_forecast.clear()
    return state

@app.post('/api/replay/step',**operation('replay'))
async def replay():
    async with lock:
        if state['mode']!='replay':raise HTTPException(409,'Switch to replay mode first')
        if state.get('snapshot'):
            vehicles.clear();history.clear();deviations.clear();last_forecast.clear()
            state.update(index=0,clock=None,snapshot=False)
        i=state['index']
        if i>=len(points):return {'done':True,**state}
        p=points.iloc[i].to_dict();t=epoch(p['T'])
        rows=traffic[(traffic.tr_id==p['tr_id'])&(traffic.ts<=t)&(traffic.ts>=t-600)].to_dict('records')
        try:result=await forecast(p,rows,'historical_replay')
        except ValueError as e:raise HTTPException(422,str(e))
        state['clock']=timestamp(p['T']).isoformat();state['index']+=1
        return {'done':False,'prediction':result,**state}

@app.get('/api/state',**operation('state'))
async def get_state(dispatcher_id:str|None=None):
    now=epoch(state['clock']) if state['mode']=='replay' and state['clock'] else time.time()
    merged={tr:dict(vehicle) for tr,vehicle in archive_vehicles.items()}
    for tr,live in vehicles.items():
        if tr in merged and live.get('source')=='live':
            # Keep an archived forecast internally coherent when live telemetry
            # has no valid current 10–15 minute target. Live coordinates are an
            # overlay, not permission to pair today's timestamp with January's
            # archived target stop and prediction.
            position={key:live[key] for key in ('lon','lat','position_time') if key in live}
            merged[tr].update(position)
            merged[tr]['live_position']=True
            merged[tr]['live_position_time']=live.get('T')
            merged[tr]['live_status']=live.get('reason')
        else:merged[tr]=dict(live)
    output=[]
    for original in sorted(merged.values(),key=lambda item:(not item.get('live_position',False),int(item['tr_id']))):
        v=dict(original)
        track=live_track(int(v['tr_id']))
        if track:v['live_track']=track
        if v.get('source')!='historical_archive' and now-epoch(v['T'])>120:
            v.update(stale=True,level='unknown',reason='Прогноз устарел; ожидается новая телеметрия')
        output.append(v)
    if dispatcher_id:
        profile=get_dispatcher(dispatcher_id)
        if profile is None:raise HTTPException(404,'Dispatcher not found')
        assigned=set(profile['assigned_tr_ids'])
        output=[item for item in output if int(item['tr_id']) in assigned]
    return {'vehicles':output,'state':state,'counters':dict(counters),'total_points':len(points),'dispatcher_id':dispatcher_id}

@app.get('/api/incidents',**operation('incidents'))
async def get_incidents():
    return {'items':incidents(list(vehicles.values())),'total':len(incidents(list(vehicles.values()))),'as_of':state['clock']}

@app.get('/api/risk',**operation('risk'))
async def get_risk():
    routes=route_risk(list(vehicles.values()))
    return {'routes':routes,'high_routes':sum(r['level']=='high' for r in routes),'medium_routes':sum(r['level']=='medium' for r in routes),'as_of':state['clock']}

@app.post('/api/what-if',**operation('what_if'))
async def what_if(body:WhatIf):
    baseline=route_risk(list(vehicles.values()))
    relief=max(0.5,1-0.15*body.extra_vehicles-body.headway_reduction_pct/100)
    projected=[]
    for item in baseline:
        probability=item['max_late_probability']
        adjusted=None if probability is None else probability*relief
        level='unknown' if adjusted is None else 'high' if adjusted>=.7 else 'medium' if adjusted>=.35 else 'low'
        projected.append({**item,'projected_late_probability':adjusted,'projected_level':level})
    return {'assumptions':{'extra_vehicles':body.extra_vehicles,'headway_reduction_pct':body.headway_reduction_pct,'risk_multiplier':relief},'baseline':baseline,'projected':projected}

@app.post('/api/map-match',**operation('map_match'))
async def map_match(body:MapMatch):
    result=match_stop(body.tr_id,body.lon,body.lat)
    if result is None:raise HTTPException(404,'No planned geometry for this route')
    return result

@app.get('/api/driver-commands',**operation('commands'))
async def get_driver_commands(tr_id:int|None=None):
    items=stored_driver_commands(tr_id)
    return {'items':items,'channel':'local_dispatch_outbox','external_delivery':False}

@app.post('/api/driver-commands',status_code=201,**operation('queue_command'))
async def queue_driver_command(body:DriverCommand):
    dispatcher=get_dispatcher(body.dispatcher_id)
    if dispatcher is None:raise HTTPException(422,'Unknown dispatcher profile')
    action_titles={
        'contact':'Связаться с водителем',
        'maintain':'Продолжать по графику',
        'accelerate_safely':'При возможности сократить отставание безопасно',
        'slow_down_safely':'При необходимости снизить темп безопасно',
    }
    command={
        'id':f"cmd-{uuid.uuid4().hex[:12]}",
        'tr_id':body.tr_id,'role':body.role,'dispatcher':dispatcher,'action':body.action,
        'action_title':action_titles[body.action],'message':body.message,
        'created_at':pd.Timestamp.now(tz='Europe/Moscow').isoformat(),
        'status':'queued_for_integration','channel':'local_dispatch_outbox',
        'external_delivery':False,
    }
    save_driver_command(command)
    return command

@app.get('/api/admin/simulation',**operation('simulation_status'))
async def simulation_status():
    return {'role_required':'admin','mode':state['mode'],'supported_scenarios':['normal','slow','stop'],'active_vehicles':len(vehicles),'runs':stored_simulations(limit=10)}

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
    model_meta=json.loads((ARTIFACT/'model_v5.json').read_text(encoding='utf-8')) if (ARTIFACT/'model_v5.json').exists() else {}
    predictions=pd.read_csv(ARTIFACT/'test_predictions_v5.csv') if (ARTIFACT/'test_predictions_v5.csv').exists() else pd.DataFrame()
    v5=dict(model='v5',features=len(model_meta.get('features',[])),test_points=len(predictions),mae_s=float(predictions.abs_error.mean()) if not predictions.empty else None,baseline_mae_s=float(predictions.baseline_abs_error.mean()) if not predictions.empty else None,interval_radius_s=model_meta.get('interval_radius_s'),coverage=legacy.get('interval_coverage'),late_threshold_s=model_meta.get('late_threshold_s',120),batch_inference_ms=legacy.get('batch_inference_ms'))
    if v5['mae_s'] is not None and v5['baseline_mae_s']:
        v5['improvement_pct']=100*(1-v5['mae_s']/v5['baseline_mae_s'])
    importance=[]
    importance_path=ARTIFACT/'feature_importance_v5.csv'
    if importance_path.exists():
        importance=pd.read_csv(importance_path).sort_values('importance',ascending=False).head(6).to_dict('records')
    return {**legacy,'v5':v5,'feature_importance':importance,'readable':{'mae':f"{v5['mae_s']:.1f} с" if v5['mae_s'] is not None else '—','baseline':f"{v5['baseline_mae_s']:.1f} с" if v5['baseline_mae_s'] is not None else '—','coverage':f"{100*v5['coverage']:.1f}%" if v5['coverage'] is not None else '—'}}

app.mount('/static',StaticFiles(directory=ROOT/'dashboard'),name='static')

@app.get('/docs',include_in_schema=False)
async def documentation():
    """Serve the human-readable API guide without modifying the dashboard UI."""
    return FileResponse(ROOT/'dashboard/docs.html')

@app.get('/',include_in_schema=False)
async def index():return FileResponse(ROOT/'dashboard/dispatcher.html')

@app.get('/admin',include_in_schema=False)
async def admin_page():return FileResponse(ROOT/'dashboard/admin-control.html')
