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
history=defaultdict(lambda:deque(maxlen=1000));vehicles={};counters=defaultdict(int)
deviations={};last_forecast={};state={'mode':'replay','clock':None,'index':0}
schedule=None;traffic=None;points=None;mapping={};client=None;lock=asyncio.Lock()

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
    tr=event['tr_id'];ts=epoch(event['event_time'])
    if ts>time.time()+60: raise ValueError('Телеметрия из будущего')
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
    global schedule,traffic,points,mapping,client
    schedule=load_schedule(DATA/'validate/schedule_plan.csv')
    traffic=load_traffic(DATA/'validate/traffic.csv')
    points=pd.read_csv(DATA/'validate/points.csv').sort_values('T').reset_index(drop=True)
    ids=pd.read_csv(DATA/'validate/traffic.csv',usecols=['unit_id','tr_id']).drop_duplicates()
    mapping={int(r.unit_id):int(r.tr_id) for r in ids.itertuples()}
    mapping.update({int(k):int(v) for k,v in json.loads(os.getenv('UNIT_MAP','{}')).items()})
    client=httpx.AsyncClient(timeout=2)
    server=await asyncio.start_server(lambda r,w:handle(r,w,on_ndtp,counters),'0.0.0.0',int(os.getenv('NDTP_PORT','9201')))
    yield
    server.close();await server.wait_closed();await client.aclose()

app=FastAPI(title='Предиктор движения — Backend',version='1.0.0',lifespan=lifespan)

@app.get('/health')
async def health():return {'status':'ok','mode':state['mode']}

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
    async with lock:
        state.update(mode=body.mode,index=0,clock=None);vehicles.clear();history.clear();deviations.clear();last_forecast.clear()
    return state

@app.post('/api/replay/step')
async def replay():
    async with lock:
        if state['mode']!='replay':raise HTTPException(409,'Switch to replay mode first')
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
        if now-epoch(v['T'])>120:
            v.update(stale=True,level='unknown',reason='Прогноз устарел; ожидается новая телеметрия')
        output.append(v)
    return {'vehicles':output,'state':state,'counters':dict(counters),'total_points':len(points)}

@app.get('/api/network')
async def network():
    # No route IDs in source: expose planned stop sequences, explicitly labeled.
    paths=[]
    for tr,g in schedule.groupby('tr_id'):
        g=g.dropna(subset=['lon','lat']).head(500)
        paths.append({'tr_id':int(tr),'points':g[['lon','lat']].values.tolist()})
    return {'kind':'planned_stop_sequences','paths':paths}

@app.get('/api/metrics')
async def metrics():return json.loads((ARTIFACT/'metrics.json').read_text(encoding='utf-8'))

app.mount('/static',StaticFiles(directory=ROOT/'dashboard'),name='static')

@app.get('/')
async def index():return FileResponse(ROOT/'dashboard/index.html')
