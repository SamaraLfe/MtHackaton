# Integration

Ветка общей инфраструктуры проекта. Код отдельных компонентов находится в ветках `dashboard`, `backend` и `ml`.

## Содержимое

- `Dockerfile` и `compose.yaml` — контейнерная сборка и запуск сервисов;
- `requirements.txt` — общие Python-зависимости;
- `scripts/` — извлечение данных, локальный запуск и проверка сервисов;
- `tests/` и `pytest.ini` — интеграционные и модульные тесты;
- `docs/` — аналитика, OpenAPI и документация коду;
- `start-local.ps1` — локальный запуск на Windows.

Эта ветка не содержит реализацию dashboard, backend, ML и датасет. Полный проект собран в `prototype`.

## Проверка после объединения компонентов

```sh
python -m pytest -q
docker compose up --build
```
