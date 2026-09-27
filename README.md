# Интеграционный контур «Такт»

Ветка содержит Docker/config, зависимости, scripts/, tests/ и docs/.
Исходники backend/, ml/, dashboard/, emulator/, dataset/ и artifacts/
в эту ветку не входят. Полная запускаемая поставка находится в prototype.
Интеграционные файлы синхронизированы с этой реализацией; ветка отдельно
не собирается без модулей и данных.

## Состав и запуск

Dockerfile создаёт Python-образ с runtime и Sphinx-зависимостями;
requirements.txt и requirements-docs.txt разделяют их описание.
compose.yaml связывает ML, backend, nginx dashboard и два NDTP-источника.
Официальный образ ndtp-telemetry-emulator:1.0 предоставляется организаторами;
собственный эмулятор находится в emulator/ полной ветки.

Из полного checkout: docker compose up -d --build. Без официального образа:
docker compose up -d --build ml backend dashboard custom-emulator.
Порты на localhost: dashboard 8080, backend 8000, NDTP 9201,
официальный источник 18080, собственный 18081. ML :8001 доступен внутри сети.

## Проверки и документация

scripts/run_prototype.py запускает полный стенд и ожидает готовности;
start_backend.py собирает Sphinx перед стартом. export_openapi.py генерирует
схемы из FastAPI; при экспорте используется отдельный STATE_DIR.
verify_running.py проверяет 151 replay-точку, submission, горизонт и NDTP/CRC.
Скрипт изменяет режим/телеметрию тестового стенда; --target только маркирует
отчёт. scripts/package_submission.py собирает полный проект из чистого commit
с HTML Sphinx и SHA256-манифестом; предназначен для ветки prototype.

Проверенная реализация: 199 Python-тестов, 19 Node-тестов, строгая сборка
Sphinx. Docker replay: p50 16,28 мс, p95 20,23 мс, максимум 148,98 мс.
Это последовательный прогон из backend-контейнера, не нагрузочный SLA.
Документация и ограничения измерений находятся в docs/.

Устаревшие отдельные HTML-снимки docs/code заменены исходниками Sphinx;
HTML строится из полного проекта, поэтому содержит актуальные сигнатуры кода.

## Документация полного решения

- [Запуск трёх модулей в Docker](https://github.com/SamaraLfe/MtHackaton/tree/prototype).
- [Инструкция для жюри](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/jury-guide.md).
- [OpenAPI/Swagger и Sphinx](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/README.md).
- [Производительность и дополнительные возможности](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/capabilities.md).

Команды запуска выполняются из полного checkout ветки prototype, а не из
этой компонентной ветки. Компонентные ветки сохраняют разделение исходников
и не являются самостоятельной Docker-поставкой.
