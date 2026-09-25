# Dashboard

Ветка интерфейса диспетчера транспортного предиктора.

## Содержимое

- `dashboard/index.html` — разметка интерфейса;
- `dashboard/app.js` — загрузка состояния, управление replay и отображение транспорта;
- `dashboard/style.css` — стили;
- `dashboard/nginx.conf` — конфигурация nginx и проксирование `/api` в backend.

## Ветки

- `dashboard_dev` — разработка и проверка изменений;
- `dashboard` — стабильная версия для включения в `prototype`.

## Запуск

Полный запуск выполняется из ветки `prototype`:

```sh
docker compose up --build
```

После запуска интерфейс доступен на `http://localhost:8080`.
