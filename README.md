# Dataset

Ветка исходных данных задачи прогнозирования задержек транспорта.

## Структура

- `dataset/train/` — обучающий период;
- `dataset/test/` — тестовый период;
- `dataset/validate/` — данные для итогового прогноза;
- `dataset/labels/` — разметка train и test;
- `dataset/docs/` — описание телематических пакетов;
- `dataset/sample_submission.csv` — формат результата.

CSV хранятся непосредственно в Git без Git LFS.

Архив `ndtp-telemetry-emulator.tar` превышает лимит обычного файла GitHub и опубликован отдельно в GitHub Release `dataset-v1`. После скачивания образ загружается командой:

```sh
docker load -i ndtp-telemetry-emulator.tar
```

Собранное решение, использующее эти данные, находится в ветке `prototype`.
