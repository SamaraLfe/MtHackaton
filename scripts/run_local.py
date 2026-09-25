"""Run two independent local services; Ctrl+C terminates both."""
import os, subprocess, sys, time
from pathlib import Path

root=Path(__file__).resolve().parents[1]
processes=[]
try:
    for module,port in [('ml.service:app','8001'),('backend.app:app','8000')]:
        processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',module,'--host','127.0.0.1','--port',port],cwd=root,env=os.environ.copy()))
    print('Dashboard: http://127.0.0.1:8000 | Swagger: http://127.0.0.1:8000/docs',flush=True)
    while all(p.poll() is None for p in processes): time.sleep(1)
    raise SystemExit('One service stopped. See its error above.')
except KeyboardInterrupt:pass
finally:
    for p in processes:
        if p.poll() is None:p.terminate()
    for p in processes:
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill()
