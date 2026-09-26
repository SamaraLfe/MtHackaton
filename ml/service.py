"""Independent HTTP inference service."""
import os
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field, ConfigDict, create_model
from ml.features import FEATURES
from ml.model import Predictor

FeatureRow=create_model('FeatureRow',__config__=ConfigDict(extra='forbid',allow_inf_nan=False),**{k:(float,...) for k in FEATURES})

class Forecast(BaseModel):
    """One calibrated prediction returned by the ML service."""
    prediction_s:float=Field(description='Прогноз отклонения в секундах; плюс означает опоздание.',examples=[61.2])
    lower_s:float=Field(description='Нижняя граница эмпирического интервала, секунды.',examples=[-68.5])
    upper_s:float=Field(description='Верхняя граница эмпирического интервала, секунды.',examples=[190.9])
    late_probability:float|None=Field(description='Эмпирическая вероятность отклонения больше 120 с.',examples=[0.42])
    model:str=Field(description='Выбранный артефакт модели.',examples=['v5'])
    uncertainty:str|None=Field(default=None,description='Метод оценки неопределённости.',examples=['empirical_test_residual'])

class ForecastResponse(BaseModel):
    """Ordered batch of forecasts; item i corresponds to input item i."""
    predictions:list[Forecast]=Field(description='Прогнозы в том же порядке, что и входные строки.')

class Batch(BaseModel):
    rows: list[FeatureRow] = Field(min_length=1,max_length=512,description='Legacy: 1…512 строк по 15 числовых признаков. Для V5 используйте /predict_v5.')

class V5Batch(BaseModel):
    model_config=ConfigDict(json_schema_extra={"examples":[{"points":[{"tr_id":131672,"T":"2026-01-06T03:35:00Z","target_stop_id":53700172828,"target_time_begin":"2026-01-06T03:50:00Z","cur_dev_s":74}],"histories":[[{"event_time":"2026-01-06T03:34:50Z","lon":37.60,"lat":55.75,"speed":25,"location_valid":True}]],"schedules":[[{"tr_id":131672,"tt_action_item_id":53700172828,"time_begin":"2026-01-06T03:50:00Z","geom":"POINT (37.60 55.75)"}]]}]})
    points: list[dict] = Field(min_length=1,max_length=512,description='Точки: tr_id, T, target_stop_id, target_time_begin, cur_dev_s.')
    histories: list[list[dict]] = Field(min_length=1,max_length=512,description='История телеметрии для каждой точки в том же порядке.')
    schedules: list[list[dict]] = Field(min_length=1,max_length=512,description='Плановые остановки для каждой точки в том же порядке.')

app=FastAPI(title='Такт — ML API',version='1.1.0',description='''
Внутренний сервис инференса V5. Он возвращает отклонение от расписания в
секундах: положительное значение означает опоздание. Основной контракт —
**POST /predict_v5**. Backend выбирает цель и хранит историю, а ML-сервис
возвращает прогноз, эмпирический интервал и риск задержки по сохранённым
остаткам. Модель champion-b4b напрямую оценивает задержку на целевой остановке;
`cur_dev_s` остаётся одним из причинных признаков, а не прибавляется постфактум.
Внешняя интеграция обычно обращается именно к backend.
''',openapi_tags=[
    {'name':'Состояние','description':'Доступность процесса и имя загруженной модели.'},
    {'name':'V5','description':'Прогноз по исходной точке, телеметрии и плану остановок.'},
    {'name':'Legacy','description':'Старый контракт 15 готовых признаков; не подходит для V5.'},
],servers=[
    {'url':'http://127.0.0.1:8001','description':'ML API при локальном запуске'},
    {'url':'/','description':'ML API внутри Docker-сети'},
],contact={'name':'Команда прототипа «Такт»','url':'https://github.com/SamaraLfe/MtHackaton'},
license_info={'name':'Prototype / internal use','url':'https://github.com/SamaraLfe/MtHackaton'},
swagger_ui_parameters={'docExpansion':'list','defaultModelsExpandDepth':0,'defaultModelExpandDepth':1,
                       'displayOperationId':True,'displayRequestDuration':True,'filter':True,
                       'deepLinking':True,'tryItOutEnabled':True,'requestSnippetsEnabled':True,
                       'showExtensions':False,'syntaxHighlight':{'theme':'arta'}})
model=Predictor(os.getenv('ARTIFACT_DIR','artifacts'))

def custom_openapi():
    """Publish useful examples for the standalone inference contract."""
    if app.openapi_schema:
        return app.openapi_schema
    schema=get_openapi(title=app.title,version=app.version,description=app.description,
                       routes=app.routes,tags=app.openapi_tags,servers=app.servers,
                       contact=app.contact,license_info=app.license_info)
    schema['externalDocs']={'description':'Backend contract и руководство','url':'http://127.0.0.1:8000/docs'}
    operation_schema=schema.get('paths',{}).get('/predict_v5',{}).get('post')
    if operation_schema:
        operation_schema.setdefault('requestBody',{}).setdefault('content',{}).setdefault('application/json',{}).setdefault('examples',{})['basic']={
            'summary':'Одна точка V5','value':V5Batch.model_config['json_schema_extra']['examples'][0]}
        operation_schema.setdefault('responses',{}).setdefault('200',{}).setdefault('content',{}).setdefault('application/json',{}).setdefault('examples',{})['basic']={
            'summary':'Форма ответа','value':{'predictions':[{'prediction_s':61.2,'lower_s':-68.5,'upper_s':190.9,'late_probability':0.42,'model':'v5','uncertainty':'empirical_test_residual'}]}}
    app.openapi_schema=schema
    return schema

app.openapi=custom_openapi

@app.get('/health',tags=['Состояние'],summary='Проверить ML-сервис',description='Возвращает status=ok и имя загруженной модели.')
def health():
    """Report ML process availability and the loaded predictor name."""
    return {'status':'ok','model':model.kind}

@app.post('/predict',response_model=ForecastResponse,tags=['Legacy'],summary='Прогноз по 15 признакам (legacy)',deprecated=True,
    description='При загруженной V5 набор из 15 legacy-признаков недостаточен. Используйте /predict_v5.',
    responses={410:{'description':'Legacy-контракт отключён для артефакта V5.'},422:{'description':'Неверные признаки или число строк вне 1…512.'}})
def predict(batch:Batch):
    if model.kind=='v5':
        raise HTTPException(410,'Legacy model is not packaged; use /predict_v5')
    return {'predictions':model.forecast(pd.DataFrame([r.model_dump() for r in batch.rows]))}

@app.post('/predict_v5',response_model=ForecastResponse,tags=['V5'],summary='Пакетный прогноз V5 по исходным данным',
    description='points, histories и schedules имеют одинаковую длину от 1 до 512; элементы с одним индексом относятся к одному прогнозу. Горизонт target_time_begin − T должен быть строго в (600, 900] секунд. Результат сохраняет порядок входа и содержит восстановленный prediction_s, интервал и late_probability. Прямой ML API проверяет числовой горизонт, но не выбирает целевую остановку.',
    responses={422:{'description':'Массивы пусты, слишком велики или имеют разную длину.'}})
def predict_v5(batch:V5Batch):
    if not (len(batch.points)==len(batch.histories)==len(batch.schedules)):
        raise HTTPException(422,'points, histories and schedules must have equal length')
    try:
        return {'predictions':model.forecast_v5(batch.points,batch.histories,batch.schedules)}
    except (TypeError,ValueError,KeyError) as exc:
        raise HTTPException(422,str(exc)) from exc
