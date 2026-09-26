# Данные «Такт»

Ветка `data` содержит только датасет задания и документацию по его форматам.
Здесь нет backend, ML-кода, dashboard, Docker Compose или готового runtime.

## Что предсказывается

Для прогнозной точки `(tr_id, T)` нужно оценить задержку на первой плановой
остановке, для которой выполняется:

```text
T + 10 минут < target_time_begin <= T + 15 минут
```

`prediction = факт − план`: положительное значение означает опоздание,
отрицательное — опережение. Для точки `T` разрешены только телеметрия с
`event_time <= T`, план и известное на этот момент `cur_dev_s`.

## Состав ветки

| Путь | Назначение |
|---|---|
| `train/traffic.csv` | размеченная обучающая телеметрия |
| `train/schedule.csv` | план и факт прибытия для train |
| `test/traffic.csv` | локальный тестовый поток |
| `test/schedule.csv` | план и факт прибытия для test |
| `labels/labels_train.csv` | точки train с `target_delay_s` |
| `labels/labels_test.csv` | точки test с `target_delay_s` |
| `validate/traffic.csv` | входная телеметрия без target |
| `validate/schedule_plan.csv` | только план validate, без `time_fact_begin` |
| `validate/points.csv` | точки прогноза validate |
| `sample_submission.csv` | шаблон `sample_id;prediction` |
| `dataset/README.md` | подробная спецификация колонок и анти-утечки |
| `dataset/docs/Emulator-and-Telematic-Packets-Specification.md` | внешний NDTP-эмулятор и бинарный формат |

Фактический размер текущей раздачи:

```text
train/traffic.csv:   287 849 строк
test/traffic.csv:    105 945 строк
validate/traffic.csv:105 945 строк
labels_train.csv:      4 434 точки
labels_test.csv:         353 точки
validate/points.csv:     151 точки
```

В train есть синтетические транспортные записи для объёма; тестовые и validate
точки используются как отдельные контрольные периоды. Фактические задержки
validate не входят в ветку и не восстанавливаются из других файлов.

## Формат результата

Нужен CSV UTF-8 с разделителем `;`, ровно с двумя колонками:

```text
sample_id;prediction
131672_1767670500;120.0
```

В `prediction` должны присутствовать все 151 `sample_id` из
`validate/points.csv`, без дублей и пропусков. Значение измеряется в секундах,
знак сохраняется.

## Проверки набора

- CSV с данными используют `,`, submission — `;`;
- времена CSV и Unix-время NDTP трактуются как UTC;
- `schedule.csv` train/test содержит `time_fact_begin`, но validate содержит
  только план;
- `target_delay_s` и `target_class` есть только в labels;
- нельзя читать строки телеметрии после `T`;
- `tr_id` и `target_stop_id` — идентификаторы, а не обучающие признаки сами по
  себе.

## NDTP-эмулятор

CSV — уже декодированная телеметрия и достаточен для офлайн-обучения. Официальный
NDTP-образ нужен только для интеграционного live-контура и в этой ветке не
хранится. Его REST/TCP-конфиг и соответствие полей описаны в
`dataset/docs/Emulator-and-Telematic-Packets-Specification.md`.

Для запуска live-контура образ загружается отдельно из выданного архива
`ndtp-telemetry-emulator.tar`; отсутствие этого архива не является ошибкой
датасета.

Модель, API и интеграционные сценарии описаны в README веток `ml`, `backend` и
`integration` соответственно.
