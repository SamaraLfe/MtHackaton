# Документация «Такт»

Документация соответствует локальному коду интеграционной ветки prototype.
Публичный контракт генерируется из FastAPI, а справочник кода — из Python.

| Материал | В репозитории | После запуска Docker |
|---|---|---|
| Инструкция для жюри | [jury-guide.md](jury-guide.md) | — |
| Backend OpenAPI | [openapi-backend.json](openapi-backend.json) | http://127.0.0.1:8000/openapi.json |
| Swagger UI | Схема выше | http://127.0.0.1:8000/docs/swagger |
| ReDoc | Схема выше | http://127.0.0.1:8000/redoc |
| ML OpenAPI | [openapi-ml.json](openapi-ml.json) | Внутри Compose: http://ml:8001/openapi.json |
| Руководство API | [HTML](../dashboard/docs.html) | http://127.0.0.1:8080/docs |
| Sphinx: код и архитектура | [исходники](sphinx/index.rst) | http://127.0.0.1:8080/code/ |
| Формулы и бизнес-правила | [business-metrics.md](business-metrics.md) | — |
| Математический аудит | [MATH_AUDIT.md](MATH_AUDIT.md) | — |
| Производительность | [performance.md](performance.md) | /api/observability |
| Комплект сдачи | [delivery.md](delivery.md) | — |

ML не публикует порт на хост: backend вызывает его в сети Compose. Примеры
запросов доступны в Swagger. Код callback принятия команды — демонстрационный
контракт, не готовый аутентифицированный канал интеграции с водителем.

## Обновление

Из корня проекта с установленными requirements.txt и requirements-docs.txt:

```sh
STATE_DIR=/tmp/takt-openapi-docs python scripts/export_openapi.py
python -m sphinx -W --keep-going -b html docs/sphinx docs/_build/html
```

Экспорт OpenAPI запускает lifespan приложений, поэтому используйте отдельный
STATE_DIR, а не рабочую БД. HTML из docs/_build/html входит в итоговый архив,
но не в Git; его можно открыть через index.html без запуска backend.
