"""One causal feature implementation shared by offline and online prediction."""
import numpy as np
import pandas as pd

TRAFFIC_COLUMNS = ['tr_id', 'event_time', 'location_valid', 'lon', 'lat', 'speed']
FEATURES = ['cur_dev_s', 'horizon_s', 'hour_sin', 'hour_cos', 'speed_last',
            'speed_mean', 'speed_std', 'speed_trend', 'stopped_fraction',
            'idle_s', 'age_s', 'observations', 'distance_m', 'required_kmh', 'missing_gps']

def timestamp(value):
    """Naive dataset times match UTC Unix suffixes of sample_id; display Moscow."""
    t = pd.Timestamp(value)
    return t.tz_localize('UTC').tz_convert('Europe/Moscow') if t.tzinfo is None else t.tz_convert('Europe/Moscow')

def epoch(value):
    """Convert a timestamp accepted by :func:`timestamp` to Unix seconds."""
    return timestamp(value).timestamp()

def load_traffic(path):
    """Load telemetry columns permitted by the feature pipeline."""
    df = pd.read_csv(path, usecols=TRAFFIC_COLUMNS)
    df['ts'] = pd.to_datetime(df.event_time, format='mixed').map(epoch)
    df['location_valid'] = df.location_valid.astype(str).str.lower().eq('true')
    return df.sort_values('ts').drop_duplicates(['tr_id', 'ts'], keep='last')

def load_schedule(path):
    """Load plan stops without actual-arrival fields and parse point geometry."""
    # Explicit allow-list prevents accidental reads of time_fact_begin.
    df = pd.read_csv(path, usecols=['tt_action_item_id', 'tr_id', 'time_begin', 'geom', 'building_address'])
    df['ts'] = pd.to_datetime(df.time_begin, format='mixed').map(epoch)
    xy = df.geom.str.extract(r'POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)').astype(float)
    df['lon'], df['lat'] = xy[0], xy[1]
    return df.sort_values('ts')

def haversine(lon, lat, lon2, lat2):
    """Return the great-circle distance between two points in metres."""
    p1, p2 = np.radians(lat), np.radians(lat2)
    a = np.sin((p2-p1)/2)**2 + np.cos(p1)*np.cos(p2)*np.sin(np.radians(lon2-lon)/2)**2
    return float(6371000*2*np.arcsin(np.sqrt(np.clip(a, 0, 1))))

def build_one(point, history, stop=None):
    """Build 15 causal features from one forecast point and preceding history."""
    t, target = epoch(point['T']), epoch(point['target_time_begin'])
    horizon = target-t
    # Sparse live routes may expose the nearest stop after T+10 when the
    # strict 10–15 minute window is empty.  Backend marks that point with
    # ``horizon_fallback`` so it remains explicit instead of being confused
    # with a regular training target.
    if not 600 < horizon <= 900 and not (point.get('horizon_fallback') and horizon > 600):
        raise ValueError('Целевая остановка должна находиться в окне (T+10, T+15] минут')
    dt = timestamp(point['T'])
    f = dict.fromkeys(FEATURES, 0.0)
    f.update(cur_dev_s=float(point['cur_dev_s']), horizon_s=horizon,
             hour_sin=float(np.sin(2*np.pi*(dt.hour+dt.minute/60)/24)),
             hour_cos=float(np.cos(2*np.pi*(dt.hour+dt.minute/60)/24)),
             age_s=3600., missing_gps=1., distance_m=-1., required_kmh=-1.)
    # A strict causal window; future rows can never affect output.
    rows = [r for r in history if t-600 <= float(r['ts']) <= t]
    rows.sort(key=lambda r:r['ts'])
    if rows:
        f['age_s'] = min(3600.,t-float(rows[-1]['ts']))
    rows = [r for r in rows if r.get('location_valid', False) and
            all(np.isfinite(float(r.get(k, np.nan))) for k in ['lon','lat','speed']) and
            abs(float(r['lat'])) <= 90 and abs(float(r['lon'])) <= 180 and 0 <= float(r['speed']) <= 130]
    if not rows: return f
    speeds = np.array([r['speed'] for r in rows], dtype=float)
    f.update(speed_last=float(speeds[-1]),speed_mean=float(speeds.mean()),
             speed_std=float(speeds.std()),observations=float(len(rows)),
             stopped_fraction=float((speeds<2).mean()), missing_gps=0.,
             age_s=min(3600.,t-float(rows[-1]['ts'])))
    if len(rows)>1:
        xs=np.array([r['ts'] for r in rows],dtype=float);xs-=xs.mean()
        f['speed_trend']=float(np.dot(xs,speeds-speeds.mean())/max(np.dot(xs,xs),1)*60)
    # Do not count unobserved communication gaps as continuous standstill.
    idle=0.
    for i in range(len(rows)-1,0,-1):
        gap=rows[i]['ts']-rows[i-1]['ts']
        if rows[i]['speed']>=2 or rows[i-1]['speed']>=2 or gap>60: break
        idle+=gap
    f['idle_s']=float(idle)
    if stop is not None and np.isfinite(stop['lon']) and np.isfinite(stop['lat']):
        f['distance_m']=haversine(rows[-1]['lon'],rows[-1]['lat'],stop['lon'],stop['lat'])
        f['required_kmh']=f['distance_m']/horizon*3.6
    return f

def build_table(points, traffic, schedule):
    """Build compact causal feature rows for every labelled forecast point."""
    groups = {int(k):g.to_dict('records') for k,g in traffic.groupby('tr_id')}
    stops = {(int(r.tr_id),int(r.tt_action_item_id)):r._asdict() for r in schedule.itertuples()}
    # Searchsorted avoids rescanning a full vehicle day per point.
    times = {k:np.array([r['ts'] for r in v]) for k,v in groups.items()}
    output=[]
    for p in points.to_dict('records'):
        key=int(p['tr_id']);t=epoch(p['T']);g=groups.get(key,[]);ts=times.get(key,np.array([]))
        history=g[np.searchsorted(ts,t-600):np.searchsorted(ts,t,side='right')]
        output.append(build_one(p,history,stops.get((key,int(p['target_stop_id'])))))
    return pd.DataFrame(output,columns=FEATURES)
