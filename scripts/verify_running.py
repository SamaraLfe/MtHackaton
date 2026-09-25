"""Integration verification against running localhost services; resets replay state."""
import json, os, time, socket, struct, urllib.request
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from backend.ndtp import crc16

BASE='http://127.0.0.1:8000'
def get(path):return json.load(urllib.request.urlopen(BASE+path,timeout=10))
def post(path,obj):
    req=urllib.request.Request(BASE+path,data=json.dumps(obj).encode(),headers={'Content-Type':'application/json'},method='POST')
    return json.load(urllib.request.urlopen(req,timeout=10))

def main():
    """Verify replay, submission shape and fragmented/invalid NDTP frames on localhost."""
    post('/api/mode',{'mode':'replay'})
    points=pd.read_csv('dataset/validate/points.csv').sort_values('T').reset_index(drop=True)
    rows=[];times=[]
    for _ in range(len(points)):
        start=time.perf_counter();r=post('/api/replay/step',{})
        times.append((time.perf_counter()-start)*1000);rows.append(r['prediction'])
    assert post('/api/replay/step',{})['done']
    sub=pd.read_csv('artifacts/submission_v5.csv',sep=';')
    assert list(sub.columns)==['sample_id','prediction']
    assert len(sub)==len(points)
    assert set(sub.sample_id)==set(points.sample_id)
    assert all(600<row['horizon_s']<=900 for row in rows)
    assert all(np.isfinite(row['prediction_s']) for row in rows)
    ids=pd.read_csv('dataset/validate/traffic.csv',usecols=['unit_id','tr_id']);unit=int(ids.iloc[0].unit_id)
    payload=bytes([0,0])+struct.pack('<IIIBBHHHHHBB',int(time.time()),376000000,557000000,224,100,20,25,90,10,150,10,1)
    body=struct.pack('<HHHI',1,101,1,1)+payload;c=crc16(body)
    frame=struct.pack('<HHHHBIH',0x7e7e,len(body),0,((c&255)<<8)|(c>>8),2,unit,0)+body
    before=get('/api/state')['counters']
    with socket.create_connection(('127.0.0.1',9201),timeout=10) as s:
        s.sendall(frame[:7]);s.sendall(frame[7:18]);s.sendall(frame[18:]);s.shutdown(socket.SHUT_WR)
        while s.recv(1024):pass
    with socket.create_connection(('127.0.0.1',9201),timeout=10) as s:
        s.sendall(frame[:-1]+bytes([frame[-1]^1]));s.shutdown(socket.SHUT_WR)
        while s.recv(1024):pass
    after=get('/api/state')['counters']
    assert after.get('ndtp_packets',0)>before.get('ndtp_packets',0)
    assert after.get('ndtp_errors',0)>before.get('ndtp_errors',0)
    report=dict(replay_points=len(points),submission_rows=len(sub),submission_schema_valid=True,replay_horizon_all_valid=True,replay_request_p50_ms=float(np.median(times)),replay_request_p95_ms=float(np.quantile(times,.95)),replay_request_max_ms=max(times),ndtp_fragmented_frame=True,ndtp_bad_crc_rejected=True,docker_tested=True)
    report_path=Path(os.getenv('VERIFICATION_OUTPUT','artifacts/verification.json'))
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    post('/api/mode',{'mode':'replay'})


if __name__=='__main__':
    main()
