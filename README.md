# Интеграционный контур «Такт»

Ветка `integration` содержит только glue-код и проверки сборки: Dockerfile,
Compose-конфигурацию, локальные скрипты, integration-тесты и снимки
документации/API. Здесь намеренно нет исходников `backend/`, `ml/`,
`dashboard/`, датасета и артефактов модели.

## Что находится в ветке

| Путь | Назначение |
|---|---|
| `Dockerfile` | общий образ Python для интеграционного запуска |
| `compose.yaml` | локальная связка ML, backend, dashboard и optional NDTP image |
| `requirements.txt` | зависимости runtime/test |
| `scripts/run_local.py` | запуск ML на 8001 и backend на 8000 |
| `scripts/verify_running.py` | smoke-проверка работающего контура |
| `scripts/extract_data.py` | безопасное извлечение только CSV/Markdown из архива |
| `start-local.ps1` | Windows-обёртка локального запуска |
| `tests/test_api.py` | проверки API, what-if, профилей и UI-контрактов |
| `tests/test_core.py` | причинность признаков, NDTP, CRC и временная логика |
| `docs/ANALYSIS.md` | зафиксированный анализ качества и ограничений |
| `docs/openapi-*.json` | снимки backend/ML OpenAPI 1.1.0 |
| `docs/code/` | сохранённые страницы документации модулей |
| `docs/evaluation.png` | визуализация оценки модели |

Снимки в `docs/` — результат интеграционной проверки, а не замена живого
Swagger. Источник API-контракта находится в коде компонентных веток и доступен
после запуска по `/openapi.json`.

## Требования к полному checkout

Для запуска этой ветки рядом должны быть предоставлены исходники и данные из
компонентных веток:

```text
backend/       исходники FastAPI и NDTP
ml/            модельный сервис и feature pipeline
dashboard/     HTML/CSS/JS диспетчерской
dataset/       train/test/validate и расписание
artifacts/     model_v5.cbm, schema, metrics, submission
```

Поэтому checkout только `integration` не является самостоятельным приложением:
его назначение — собрать полный прототип и проверить стыки между компонентами.

## Локальный запуск

После объединения с компонентными ветками и установки зависимостей:

```sh
python -m pip install -r requirements.txt
python scripts/run_local.py
```

Локальные адреса:

```text
Backend:  http://127.0.0.1:8000
ML API:   http://127.0.0.1:8001
Swagger:   http://127.0.0.1:8000/docs/swagger
API guide: http://127.0.0.1:8000/docs
NDTP TCP: 127.0.0.1:9201
```

Compose-вариант:

```sh
docker compose up --build
```

В этом integration Compose официальный `ndtp-telemetry-emulator` включается
только с профилем `emulator`:

```sh
docker compose --profile emulator up --build
```

Образ `ndtp-telemetry-emulator:1.0` является внешним артефактом и не хранится в
ветке. Полный `prototype` дополнительно подключает собственный custom-emulator
и его настройки.

## Проверка работающего контура

После запуска сервисов:

```sh
python scripts/verify_running.py --target local
```

Проверка выполняет:

- `/health/ready` и `/api/observability`;
- полный replay по `dataset/validate/points.csv`;
- формат и покрытие `artifacts/submission_v5.csv`;
- фрагментированный TCP NDTP-кадр;
- отказ кадра с неверным CRC;
- восстановление исходного режима после проверки.

Отчёт сохраняется в `artifacts/verification.json`. Этот путь должен быть
доступен для записи, поэтому артефакты не входят в integration-ветку.

Тесты запускаются из полного checkout:

```sh
python -m pytest -q
```

Они не обучают модель заново и используют сохранённые артефакты. Для изменения
схемы API сначала обновляются backend/ML-ветки, затем выполняется экспорт
OpenAPI и обновляются снимки `docs/openapi-backend.json` и
`docs/openapi-ml.json`.

## Границы интеграционного контура

Ветка проверяет соединение компонентов, а не заменяет их ответственность:

- данные и анти-утечка описаны в ветке `data`;
- CatBoost V5 и `/predict_v5` описаны в ветке `ml`;
- NDTP, прогноз, what-if и административные API описаны в ветке `backend`;
- карта и рабочие места описаны в ветке `dashboard`.

Такой состав позволяет обновлять интеграционные проверки, не копируя исходники
компонентов и не создавая вторую версию бизнес-логики.
