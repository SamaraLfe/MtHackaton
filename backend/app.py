"""Dispatcher backend: NDTP/JSON -> causal features -> independent ML service."""
import asyncio, json, os, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from ml.features import build_one, epoch, timestamp, load_traffic, load_schedule, haversine
from backend.ndtp import handle

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.getenv('DATA_DIR','dataset'));ARTIFACT=Path(os.getenv('ARTIFACT_DIR','artifacts'))
ML_URL=os.getenv('ML_URL','http://127.0.0.1:8001')
history=defaultdict(lambda:deque(maxlen=1000));vehicles={};counters=defaultdict(int);driver_commands=deque(maxlen=200)
deviations={};last_forecast={};state={'mode':'replay','clock':None,'index':0,'snapshot':False}
schedule=None;schedule_template=None;live_schedule_day=None;traffic=None;points=None;mapping={};client=None;lock=asyncio.Lock()
DISPATCHER_PROFILES=[
    {'id':'dispatcher-01','name':'Диспетчер №01','role':'Маршрутный диспетчер'},
    {'id':'dispatcher-02','name':'Диспетчер №02','role':'Старший диспетчер'},
]

class Telemetry(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    tr_id:int=Field(gt=0)
    event_time:str
    lon:float=Field(ge=-180,le=180)
    lat:float=Field(ge=-90,le=90)
    speed:float=Field(ge=0,le=130)
    location_valid:bool=True

class Point(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    tr_id:int=Field(gt=0)
    T:str
    target_stop_id:int
    target_time_begin:str
    cur_dev_s:float

class Mode(BaseModel):
    mode:str=Field(pattern='^(live|replay)$')

class WhatIf(BaseModel):
    extra_vehicles:int=Field(default=0,ge=0,le=20)
    headway_reduction_pct:float=Field(default=0,ge=0,le=50)

class MapMatch(BaseModel):
    tr_id:int=Field(gt=0)
    lon:float=Field(ge=-180,le=180)
    lat:float=Field(ge=-90,le=90)

class AdminSimulation(BaseModel):
    role:str
    tr_id:int=Field(gt=0)
    scenario:str=Field(default='slow',pattern='^(normal|slow|stop)$')
    count:int=Field(default=3,ge=1,le=20)
    interval_s:int=Field(default=30,ge=5,le=300)

class DriverCommand(BaseModel):
    role:str=Field(pattern='^(dispatcher|admin)$')
    dispatcher_id:str=Field(pattern='^dispatcher-\\d{2}$')
    tr_id:int=Field(gt=0)
    action:str=Field(pattern='^(contact|maintain|accelerate_safely|slow_down_safely)$')
    message:str=Field(min_length=5,max_length=300)

def align_schedule_to_event_day(frame, event_time):
    """Move a historical day-plan to the local calendar day of live telemetry."""
    if frame.empty:return frame.copy()
    result=frame.copy()
    event_local=timestamp(event_time)
    schedule_start=pd.to_datetime(result['ts'].min(), unit='s', utc=True).tz_convert('Europe/Moscow').normalize()
    shift=event_local.normalize().timestamp()-schedule_start.timestamp()
    result['ts']=result['ts'].astype(float)+shift
    shifted=pd.to_datetime(result['time_begin'], format='mixed')+pd.to_timedelta(shift, unit='s')
    result['time_begin']=shifted.map(lambda value:value.isoformat(sep=' '))
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
    vehicles.clear();history.clear();deviations.clear();last_forecast.clear()
    snapshot=points.sort_values('T').groupby('tr_id',as_index=False).tail(1).sort_values('tr_id')
    for _,point in snapshot.iterrows():
        tr=int(point['tr_id']);event_time=point['T'];t=epoch(event_time)
        stop_rows=schedule[(schedule.tr_id==tr)&(schedule.tt_action_item_id==int(point['target_stop_id']))]
        if stop_rows.empty:continue
        stop=stop_rows.sort_values('ts').iloc[0]
        records=traffic[(traffic.tr_id==tr)&(traffic.ts<=t)&(traffic.ts>=t-600)]
        valid=records[records.location_valid].dropna(subset=['lon','lat'])
        last=valid.iloc[-1] if not valid.empty else None
        prediction=float(point.cur_dev_s)
        probability=float(1/(1+np.exp(-(prediction-120)/45)))
        level='high' if probability>=.7 else 'medium' if probability>=.35 else 'low'
        address='Контрольная точка не указана' if pd.isna(stop.building_address) else str(stop.building_address)
        vehicles[tr]=dict(
            tr_id=tr,T=timestamp(event_time).isoformat(),target_time_begin=timestamp(point.target_time_begin).isoformat(),
            target_stop_id=int(point.target_stop_id),stop_address=address,prediction_s=prediction,
            lower_s=prediction-104.78,upper_s=prediction+104.78,late_probability=probability,level=level,
            reason='Историческое отклонение по архивной телеметрии',recommendation='Связаться с водителем и уточнить обстановку' if level in {'high','medium'} else 'Продолжить наблюдение',
            source='historical_archive',model='v5',degraded=False,stale=False,position_time=float(last.ts) if last is not None else None,
            lon=float(last.lon) if last is not None else None,lat=float(last.lat) if last is not None else None,
        )
    state.update(mode='replay',clock=timestamp(snapshot['T'].max()).isoformat(),index=len(points),snapshot=True)

def match_stop(tr_id, lon, lat):
    candidates=schedule[schedule.tr_id==tr_id].dropna(subset=['lon','lat']).sort_values('ts')
    if candidates.empty:return None
    distances=candidates.apply(lambda row:haversine(lon,lat,row.lon,row.lat),axis=1)
    index=distances.idxmin();position=int(candidates.index.get_loc(index));row=candidates.loc[index]
    distance=float(distances.loc[index])
    return {'tr_id':int(tr_id),'stop_id':int(row.tt_action_item_id),'stop_address':str(row.building_address),'distance_m':round(distance,2),'segment_index':position,'next_stop_id':int(candidates.iloc[min(position+1,len(candidates)-1)].tt_action_item_id),'confidence':round(max(0.,1-distance/250),3)}

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
    if state['mode']!='live':return
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
    schedule=load_schedule(DATA/'validate/schedule_plan.csv')
    schedule_template=schedule.copy()
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

app=FastAPI(title='Предиктор движения — Backend',version='1.0.0',lifespan=lifespan)

@app.get('/health')
async def health():return {'status':'ok','mode':state['mode']}

@app.get('/api/dispatchers')
async def dispatchers():return {'profiles':DISPATCHER_PROFILES,'authentication':'local_profile_selection'}

@app.post('/api/telemetry')
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

@app.post('/api/predict')
async def predict(point:Point):
    try:
        if epoch(point.T)>time.time()+60:raise ValueError('Future forecast time')
        async with lock:return await forecast(point.model_dump(),list(history[point.tr_id]),'api')
    except ValueError as e:raise HTTPException(422,str(e))

@app.post('/api/mode')
async def mode(body:Mode):
    global schedule,live_schedule_day
    async with lock:
        schedule=schedule_template.copy();live_schedule_day=None
        if body.mode=='replay':load_historical_snapshot()
        else:
            state.update(mode='live',index=0,clock=None,snapshot=False);vehicles.clear();history.clear();deviations.clear();last_forecast.clear()
    return state

@app.post('/api/replay/step')
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

@app.get('/api/state')
async def get_state():
    now=epoch(state['clock']) if state['mode']=='replay' and state['clock'] else time.time()
    output=[]
    for original in list(vehicles.values()):
        v=dict(original)
        if v.get('source')!='historical_archive' and now-epoch(v['T'])>120:
            v.update(stale=True,level='unknown',reason='Прогноз устарел; ожидается новая телеметрия')
        output.append(v)
    return {'vehicles':output,'state':state,'counters':dict(counters),'total_points':len(points)}

@app.get('/api/incidents')
async def get_incidents():
    return {'items':incidents(list(vehicles.values())),'total':len(incidents(list(vehicles.values()))),'as_of':state['clock']}

@app.get('/api/risk')
async def get_risk():
    routes=route_risk(list(vehicles.values()))
    return {'routes':routes,'high_routes':sum(r['level']=='high' for r in routes),'medium_routes':sum(r['level']=='medium' for r in routes),'as_of':state['clock']}

@app.post('/api/what-if')
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

@app.post('/api/map-match')
async def map_match(body:MapMatch):
    result=match_stop(body.tr_id,body.lon,body.lat)
    if result is None:raise HTTPException(404,'No planned geometry for this route')
    return result

@app.get('/api/driver-commands')
async def get_driver_commands(tr_id:int|None=None):
    items=[item for item in driver_commands if tr_id is None or item['tr_id']==tr_id]
    return {'items':items,'channel':'local_dispatch_outbox','external_delivery':False}

@app.post('/api/driver-commands',status_code=201)
async def queue_driver_command(body:DriverCommand):
    dispatcher=next((profile for profile in DISPATCHER_PROFILES if profile['id']==body.dispatcher_id),None)
    if dispatcher is None:raise HTTPException(422,'Unknown dispatcher profile')
    action_titles={
        'contact':'Связаться с водителем',
        'maintain':'Продолжать по графику',
        'accelerate_safely':'При возможности сократить отставание безопасно',
        'slow_down_safely':'При необходимости снизить темп безопасно',
    }
    command={
        'id':f"cmd-{int(time.time()*1000)}-{len(driver_commands)+1}",
        'tr_id':body.tr_id,'role':body.role,'dispatcher':dispatcher,'action':body.action,
        'action_title':action_titles[body.action],'message':body.message,
        'created_at':pd.Timestamp.now(tz='Europe/Moscow').isoformat(),
        'status':'queued_for_integration','channel':'local_dispatch_outbox',
        'external_delivery':False,
    }
    driver_commands.appendleft(command)
    return command

@app.get('/api/admin/simulation')
async def simulation_status():
    return {'role_required':'admin','mode':state['mode'],'supported_scenarios':['normal','slow','stop'],'active_vehicles':len(vehicles)}

@app.post('/api/admin/simulation')
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

@app.get('/api/network')
async def network():
    # No route IDs in source: expose planned stop sequences, explicitly labeled.
    paths=[]
    for tr,g in schedule.groupby('tr_id'):
        g=g.dropna(subset=['lon','lat']).head(500)
        paths.append({'tr_id':int(tr),'points':g[['lon','lat']].values.tolist()})
    return {'kind':'planned_stop_sequences','paths':paths}

@app.get('/api/metrics')
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

@app.get('/')
async def index():return FileResponse(ROOT/'dashboard/dispatcher.html')

@app.get('/admin')
async def admin_page():return FileResponse(ROOT/'dashboard/admin-control.html')
