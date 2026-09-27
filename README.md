# ML-ядро «Такт»

Ветка содержит README, ml/ и artifacts/. Рабочая модель — CatBoost
champion-b4b, напрямую прогнозирующая target_delay_s. Backend, dashboard,
датасет и Docker-конфигурация находятся в профильных ветках и prototype.

## Модель и признаки

60 причинных GPS-признаков включают текущее отклонение, горизонт, время,
последнюю валидную координату, скорость, простои и перемещение в окнах
1/3/5/10/15 минут, направление и расстояние до цели. tr_id и target_stop_id
исключены. Будущие точки после T не используются. cur_dev_s — признак,
а не добавка к выходу модели. Положительный прогноз означает опоздание,
отрицательный — опережение на целевой остановке, не изменение отклонения.

Параметры: 500 деревьев, глубина 6, learning_rate=0.04, l2_leaf_reg=5,
MAE-loss. Основной API — пакетный POST /predict_v5; /predict устаревший,
/health проверяет готовность. Выбор остановки в окне 600 < horizon_s <= 900
выполняет backend. ML не хранит поток между запросами.

## Подтверждённые показатели

MAE test: 62,0938 с против 93,3598 с у persistence (улучшение 33,49%).
Покрытие номинального 90% интервала: 90,3683%, радиус 129,6948 с.
Brier score: 0,103265. Holdout по реальным ТС: weighted MAE 80,2440 с
против 86,8379 с baseline. Чистый batch 353 строк: 6,9273 мс без построения
признаков и HTTP. Источники: artifacts/metrics.json и environment.json.

Test и validate телеметрия совпадают и встречаются в train, поэтому test MAE
не является независимой оценкой будущего дня. Score 1,00000 сообщён для
b4b636b; локальный отчёт не является проверкой внешней платформы.
Риск задержки более 120 секунд и интервал основаны на эмпирических остатках,
а не на отдельном классификаторе или гарантии покрытия нового дня.

## Артефакты и воспроизведение

artifacts/model.cbm — модель; model.json и feature_schema.json — метаданные;
submission.csv — 151 прогноз; test_predictions.csv и feature_importance.csv —
диагностика. verification.json и verification-docker.json относятся к полному
интеграционному контуру, а не только ML-инференсу.

Из полного checkout: python -m ml.train --data dataset --out artifacts.
Docker-сервис ml слушает :8001 внутри Compose и не публикует порт на хост.

## Документация полного решения

- [Запуск трёх модулей в Docker](https://github.com/SamaraLfe/MtHackaton/tree/prototype).
- [Инструкция для жюри](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/jury-guide.md).
- [OpenAPI/Swagger и Sphinx](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/README.md).
- [Производительность и дополнительные возможности](https://github.com/SamaraLfe/MtHackaton/blob/prototype/docs/capabilities.md).

Команды запуска выполняются из полного checkout ветки prototype, а не из
этой компонентной ветки. Компонентные ветки сохраняют разделение исходников
и не являются самостоятельной Docker-поставкой.
