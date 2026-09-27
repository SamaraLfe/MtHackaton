# Backend «Такт»

Ветка содержит только README и backend/: FastAPI-приложение, NDTP-декодер
и описания OpenAPI. Реализация синхронизирована с интеграционной prototype.
ML, интерфейс, данные и эмуляторы в эту ветку не входят.

## Обработка телеметрии и прогнозов

Backend принимает Nav00 по TCP :9201 и JSON через POST /api/telemetry.
Проверяет CRC16, собирает фрагментированные кадры, отклоняет повторы.
История ограничена временем прогноза T. Координата проецируется на сегмент
планового маршрута для расчёта текущего отклонения; это не дорожный map matching.
Цель прогноза — первая остановка со строгим горизонтом 600 < horizon_s <= 900.
ML вызывается через /predict_v5; его прогноз, интервал и риск не подменяются
эвристикой. Отсутствие цели или данных не считается нулевым риском.

Позиция обновляется на каждом GPS-пакете, ML — с ограничением
LIVE_FORECAST_INTERVAL_S=1 на ТС. ML_RETRY_INTERVAL_S=5 ограничивает повторы
при сбое. LIVE_STALE_S=15 определяет потерю связи, LIVE_FALLBACK_S=60 —
переход к явно маркированному архивному fallback при общем отсутствии потока.
Рейсы имеют независимые статусы not_started, active, completed.

## Состояние и действия

Один backend-процесс владеет live-историей. Профили, назначения, команды и
резервный outbox сохраняются в SQLite на диске. Тикеты хранятся исключительно
в SQLite TEMP с temp_store=MEMORY: перезапуск очищает их, F5 — нет.
Pending/недоставка обозначены серым, executing — синим. После приёма команды
новый свежий live-прогноз выхода ранее рискованного ТС в зелёную зону закрывает
тикет; через 15 секунд он физически удаляется единственной lifespan-задачей.
Audit-журнал остаётся отдельно и не восстанавливает тикеты.

Демо-приём команды повышает скорость выбранного custom-ТС через debug-оверлей;
для оригинального потока подтверждает только приём. Повторный callback
идемпотентен. What-if резерва не меняет прогноз основного ТС. Guardrails
ограничивают действия по качеству данных и риску; реальная внешняя доставка
водителю и физический выпуск резерва не подключены.

## API

Основные ресурсы: /api/state, /api/predict, /api/incidents, /api/risk,
/api/network, /api/map-match, /api/what-if, /api/action-plan,
/api/action-center, /api/driver-commands, /api/reserve-dispatches,
/api/admin/emulators и /api/debug/custom-emulator/speed.
Контракты /acknowledge и /simulate относятся к приёму команды.
/health/ready проверяет данные, SQLite, модель и ML API;
/api/observability показывает метрики процесса, /api/metrics — качества модели.

После запуска полной системы Swagger: http://127.0.0.1:8000/docs/swagger,
OpenAPI: http://127.0.0.1:8000/openapi.json. Аутентификация локальная
демонстрационная, не production SSO; несколько backend workers не поддерживаются.

## Документация полного решения

- [Запуск трёх модулей в Docker](https://github.com/SamaraLfe/MtHackaton/tree/prototype).
- [Инструкция для жюри](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/jury-guide.md).
- [OpenAPI/Swagger и Sphinx](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/README.md).
- [Производительность и дополнительные возможности](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/capabilities.md).

Команды запуска выполняются из полного checkout ветки prototype, а не из
этой компонентной ветки. Компонентные ветки сохраняют разделение исходников
и не являются самостоятельной Docker-поставкой.
