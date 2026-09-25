"""Export the current FastAPI OpenAPI contracts into versioned JSON files."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from fastapi.testclient import TestClient

from backend.app import app as backend_app
from ml.service import app as ml_app


def export_openapi(app, destination):
    """Start an app lifespan and write its generated schema as formatted JSON."""
    with TestClient(app) as client:
        schema=client.get('/openapi.json').json()
    destination.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+"\n",encoding='utf-8')


def main():
    """Write backend and ML contracts to the docs directory."""
    docs=ROOT/'docs'
    export_openapi(backend_app,docs/'openapi-backend.json')
    export_openapi(ml_app,docs/'openapi-ml.json')


if __name__=='__main__':
    main()
