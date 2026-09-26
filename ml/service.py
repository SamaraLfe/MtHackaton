"""Independent HTTP inference service."""
import os
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ConfigDict, create_model
from ml.features import FEATURES
from ml.model import Predictor

FeatureRow=create_model('FeatureRow',__config__=ConfigDict(extra='forbid',allow_inf_nan=False),**{k:(float,...) for k in FEATURES})
class Batch(BaseModel):
    rows: list[FeatureRow] = Field(min_length=1,max_length=512,description='Legacy: 1…512 строк по 15 числовых признаков. Для V5 используйте /predict_v5.')

class V5Batch(BaseModel):
    points: list[dict] = Field(min_length=1,max_length=512,description='Точки: tr_id, T, target_stop_id, target_time_begin, cur_dev_s.')
    histories: list[list[dict]] = Field(min_length=1,max_length=512,description='История телеметрии для каждой точки в том же порядке.')
    schedules: list[list[dict]] = Field(min_length=1,max_length=512,description='Плановые остановки для каждой точки в том же порядке.')

app=FastAPI(title='Такт — ML API',version='1.0.0',description='''
Внутренний сервис инференса V5. Он возвращает отклонение от расписания в
секундах: положительное значение означает опоздание. Основной контракт —
**POST /predict_v5**. Backend выбирает цель и хранит историю, а ML-сервис
возвращает прогноз, split-conformal интервал и риск задержки по отдельной
калибровочной части. Внешняя интеграция обычно обращается именно к backend.
''',openapi_tags=[
    {'name':'Состояние','description':'Доступность процесса и имя загруженной модели.'},
    {'name':'V5','description':'Прогноз по исходной точке, телеметрии и плану остановок.'},
    {'name':'Legacy','description':'Старый контракт 15 готовых признаков; не подходит для V5.'},
])
model=Predictor(os.getenv('ARTIFACT_DIR','artifacts'))

@app.get('/health',tags=['Состояние'],summary='Проверить ML-сервис',description='Возвращает status=ok и имя загруженной модели.')
def health():
    """Report ML process availability and the loaded predictor name."""
    return {'status':'ok','model':model.kind}

@app.post('/predict',tags=['Legacy'],summary='Прогноз по 15 признакам (legacy)',deprecated=True,
    description='При загруженной V5 набор из 15 legacy-признаков недостаточен. Используйте /predict_v5.',
    responses={410:{'description':'Legacy-контракт отключён для артефакта V5.'},422:{'description':'Неверные признаки или число строк вне 1…512.'}})
def predict(batch:Batch):
    if model.kind=='v5':
        raise HTTPException(410,'Legacy model is not packaged; use /predict_v5')
    return {'predictions':model.forecast(pd.DataFrame([r.model_dump() for r in batch.rows]))}

@app.post('/predict_v5',tags=['V5'],summary='Пакетный прогноз V5 по исходным данным',
    description='points, histories и schedules имеют одинаковую длину от 1 до 512; элементы с одним индексом относятся к одному прогнозу. Результат сохраняет порядок входа и содержит prediction_s, интервал и late_probability. Прямой ML API не проверяет выбор целевой остановки.',
    responses={422:{'description':'Массивы пусты, слишком велики или имеют разную длину.'}})
def predict_v5(batch:V5Batch):
    if not (len(batch.points)==len(batch.histories)==len(batch.schedules)):
        raise HTTPException(422,'points, histories and schedules must have equal length')
    return {'predictions':model.forecast_v5(batch.points,batch.histories,batch.schedules)}
