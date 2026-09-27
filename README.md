# Данные «Такт»

Ветка содержит только README и dataset/: исходные таблицы задания,
описание форматов и спецификацию NDTP. Исходный код, модели и runtime
сюда не входят. Состав dataset синхронизирован с полной веткой prototype.

## Прогнозируемая величина

Для (tr_id, T) оценивается отклонение фактического прибытия от планового
на первой остановке, где 600 < target_time_begin - T <= 900 секунд.
Положительное значение — опоздание, отрицательное — опережение.
Используются только наблюдения event_time <= T. Отсутствие контрольной точки
не разрешает расширять горизонт или подставлять другую остановку.

## Состав

- train/traffic.csv и train/schedule.csv — обучение и расписание.
- test/traffic.csv и test/schedule.csv — диагностическая test-часть.
- labels/labels_train.csv и labels/labels_test.csv — целевые значения.
- validate/traffic.csv, validate/points.csv, validate/schedule_plan.csv — replay.
- sample_submission.csv — формат sample_id;prediction.
- [dataset/README.md](dataset/README.md) — колонки и правила причинности.
- [Спецификация NDTP](dataset/docs/Emulator-and-Telematic-Packets-Specification.md).

Рабочая модель использует 4434 обучающие, 353 test и 151 validate-точку.
Число прогнозных точек не равно числу GPS-сообщений. CSV — UTF-8;
данные разделены запятой, submission — точкой с запятой.

## Ограничения

Test и validate телеметрия совпадают и встречаются в train. Оценка на test
не является независимым backtest будущего дня. Причины задержек, дорожный
граф, пассажиропоток и подтверждённые статусы дверей не предоставлены.
Система строит плановую геометрию по остановкам, а диагностические причины
показывает как гипотезы. Распространение датасета определяется условиями
организаторов задания.

## Документация полного решения

- [Запуск трёх модулей в Docker](https://github.com/SamaraLfe/MtHackaton/tree/prototype).
- [Инструкция для жюри](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/jury-guide.md).
- [OpenAPI/Swagger и Sphinx](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/README.md).
- [Производительность и дополнительные возможности](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/capabilities.md).

Команды запуска выполняются из полного checkout ветки prototype, а не из
этой компонентной ветки. Компонентные ветки сохраняют разделение исходников
и не являются самостоятельной Docker-поставкой.
