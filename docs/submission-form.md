# Готовые поля формы сдачи

Ветка prototype. Ссылки ведут на исходники; localhost доступен после запуска
у жюри. Публичное размещение веб-системы не заявляется.

## 1. Рабочая система из трёх модулей в Docker

https://github.com/SamaraLfe/MtHackaton/tree/prototype

«Такт»: ML-ядро CatBoost/FastAPI, backend FastAPI с NDTP/TCP и BI-дашборд.
Запуск по README через Docker Compose. Датасет и обученная модель включены.
Доступны полный стенд с официальным образом организаторов и вариант без него
с собственным NDTP-источником.

## 2. Инструкция для жюри

https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/jury-guide.md

NDTP и исторический replay, dashboard, прогнозы, алерты, метрики и демо-сценарий.

## 3. Документация API и кода

https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/README.md

Каталог OpenAPI backend/ML и Sphinx. После запуска: Sphinx
http://127.0.0.1:8080/code/ ; Swagger http://127.0.0.1:8000/docs/swagger ;
OpenAPI http://127.0.0.1:8000/openapi.json . Готовый Sphinx HTML также в ZIP.

## 4. Производительность и дополнительные возможности

https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/performance.md

Сохранённый чистый CatBoost batch-инференс: 6,93 мс на 353 строки.
Docker replay от 27.09.2026: 151 точка, p50 16,28 мс, p95 20,23 мс,
max 148,98 мс на HTTP-запрос из backend-контейнера. Это не нагрузочный предел
или SLA. Проверки: 199 Python-тестов, 19 Node-тестов, строгая сборка Sphinx.
MAE test 62,09 с против 93,36 с persistence; покрытие 90% интервала 90,37%.
Test пересекается с train: это диагностика pipeline, не независимый backtest.
Методика повторной проверки Docker и ограничения приведены по ссылке выше.

Дополнительно: два управляемых NDTP-потока до 26 ТС; хаотичный custom-эмулятор;
debug-скорость выбранного ТС без подмены ML; causal replay; CRC16,
фрагментация TCP и дедупликация; карта, интервалы и риск; потеря связи и
маркированный архивный fallback; профили диспетчеров; тикеты в памяти с цветами
ожидания/исполнения/закрытия и удалением через 15 секунд; демо-приём команды;
audit-outbox; guardrails; what-if и подтверждение резерва без изменения
основного прогноза; синхронизация выбора резерва и карточки; тёмная тема;
readiness, observability, OpenAPI/Swagger и автоматически собираемый Sphinx.

## Перед отправкой

- Проверить доступ жюри и актуальный commit в prototype.
- Запустить Compose и проверить readiness, dashboard, Swagger и Sphinx.
- Проверить тесты Python/Node и строгую сборку документации.
- На свободном стенде выполнить runtime-проверку.
- При необходимости приложить ZIP по [delivery.md](delivery.md).
