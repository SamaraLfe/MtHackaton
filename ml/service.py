"""Independent HTTP inference service."""
import os
import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel, Field, ConfigDict, create_model
from ml.features import FEATURES
from ml.model import Predictor

FeatureRow=create_model('FeatureRow',__config__=ConfigDict(extra='forbid',allow_inf_nan=False),**{k:(float,...) for k in FEATURES})
class Batch(BaseModel):
    rows: list[FeatureRow] = Field(min_length=1,max_length=512)

app=FastAPI(title='Transit ML',version='1.0.0')
model=Predictor(os.getenv('ARTIFACT_DIR','artifacts'))

@app.get('/health')
def health(): return {'status':'ok','model':model.kind}

@app.post('/predict')
def predict(batch:Batch):
    return {'predictions':model.forecast(pd.DataFrame([r.model_dump() for r in batch.rows]))}
