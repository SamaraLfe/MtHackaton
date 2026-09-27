# Производительность и качество: что именно измерено

Оценка разделяет скорость CatBoost, задержку HTTP pipeline и частоту обновления UI.
Нагрузочный предел, число одновременных пользователей и production SLA не измерены.

## Сохранённые результаты

Источник: [metrics.json](../artifacts/metrics.json).

| Метрика | Значение |
|---|---:|
| Batch CatBoost, 353 строки | 6,9273 мс |
| MAE, test (353 строки) | 62,0938 с |
| MAE persistence | 93,3598 с |
| MAE zero baseline | 103,3371 с |
| Покрытие номинального 90% интервала | 90,3683% |
| Радиус интервала | 129,6948 с |
| Brier score | 0,103265 |
| Holdout по реальным ТС, weighted MAE | 80,2440 с |
| Baseline того же holdout | 86,8379 с |

Batch-замер не включает чтение CSV, построение признаков, NDTP, HTTP или
отрисовку. Окружение библиотек сохранено в [environment.json](../artifacts/environment.json).
CPU/RAM и число повторов исходного замера не сохранены: это ориентир,
не аппаратно воспроизводимая гарантия времени ответа.

В [verification.json](../artifacts/verification.json) сохранён **local**, не
Docker, прогон: 151 точка, p50 33,8965 мс, p95 50,4439 мс, max 152,6421 мс.
Он проверяет весь replay HTTP-запрос, строгий горизонт, submission и NDTP/CRC.
Docker-замер приведён отдельно: окружение и границы измерения отличаются.

## Проверка текущей версии в Docker

Проверка реализации от 27.09.2026:
[verification-docker.json](../artifacts/verification-docker.json).
Docker Compose, запросы из backend-контейнера к localhost, один последовательный
прогон 151 точки: **p50 16,2818 мс; p95 20,2324 мс; max 148,9764 мс**.
Оба эмулятора запущены; это не изолированный нагрузочный тест и не измерение
сети браузера. NDTP fragmentation/CRC, submission и горизонт прошли проверки.
Регрессионная проверка той же версии: 199 Python + 19 Node тестов; Sphinx -W
без предупреждений сборки. Python сообщает deprecation warning зависимости
Starlette/httpx, не ошибку тестов.

Окружение: Docker Desktop, aarch64, 8 CPU, доступная Docker память
8 319 770 624 байта (около 7,75 GiB); индивидуальные лимиты Compose не заданы.

## Ограничения качества

Test и validate телеметрия совпадают и встречаются в train. MAE на test —
диагностика pipeline, не независимая оценка будущего дня. Holdout по ТС полезнее,
но также не заменяет временной backtest. Покрытие интервала не гарантируется
на другом дне. Score 1,00000 сообщён для b4b636b; локальная проверка не
подтверждает самостоятельно результат внешней платформы.

## Частота обновления

- Текущая позиция и отклонение: каждый принятый GPS-пакет.
- ML: на пакет, ограничение LIVE_FORECAST_INTERVAL_S=1 на ТС.
- Custom NDTP: EMULATOR_INTERVAL_S=3; чаще перерисовывать не значит получить новые данные.
- Dashboard: следующий опрос спустя 1 с после завершения предыдущего, без перекрытия циклов.
- Action plan: кэш 3 с; геометрия: 60 с; ML retry: 5 с при ошибках.
- Потеря связи: 15 с; общий исторический fallback: 60 с.

## Повторная проверка

На свободном демонстрационном стенде (проверка изменяет режим и телеметрию):

```sh
docker compose exec -e VERIFICATION_OUTPUT=/app/state/verification-docker.json backend python scripts/verify_running.py --target docker
docker compose exec backend python -c "from pathlib import Path; print(Path('state/verification-docker.json').read_text())"
```

Сопоставимость замеров зависит от commit, ОС/архитектуры, CPU, RAM, лимитов Docker
и параллельной нагрузки. /api/observability показывает runtime-счётчики
и задержки текущего процесса; /api/metrics — качество модели. Ни один из этих
endpoint не доказывает точность прогноза на ещё не размеченном live-потоке.
