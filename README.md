# ML

Ветка обучения и инференса модели прогноза задержек городского транспорта.

## Содержимое

- `ml/features.py` — причинные признаки по истории телеметрии;
- `ml/model.py` — загрузка модели и инференс;
- `ml/service.py` — FastAPI-сервис прогнозирования;
- `ml/train.py` — обучение и формирование submission;
- `artifacts/` — модель, метрики, прогнозы и проверочные результаты.

## Ветки

- `ml_dev` — разработка и эксперименты;
- `ml` — стабильная версия для включения в `prototype`.

## Обучение в составе prototype

```sh
python -m ml.train --data dataset --out artifacts
```

## Запуск ML API

```sh
python -m uvicorn ml.service:app --host 127.0.0.1 --port 8001
```
