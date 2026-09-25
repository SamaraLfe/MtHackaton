# Backend

Ветка HTTP API и приёмника телематических пакетов NDTP.

## Содержимое

- `backend/app.py` — FastAPI, replay, состояние транспорта и вызов ML-сервиса;
- `backend/ndtp.py` — разбор NDTP-пакетов и проверка CRC;
- `backend/__init__.py` — Python-пакет backend.

По умолчанию backend читает данные из `dataset/` и модельные результаты из `artifacts/`. Эти каталоги подключаются в собранной ветке `prototype`.

## Ветки

- `backend_dev` — разработка и тестирование;
- `backend` — стабильная версия для включения в `prototype`.

## Запуск в составе prototype

```sh
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Swagger: `http://127.0.0.1:8000/docs`  
NDTP TCP: `127.0.0.1:9201`
