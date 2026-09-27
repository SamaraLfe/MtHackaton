Скрипты проекта
===============

Скрипты не входят в runtime API, но задают воспроизводимый запуск, сборку
контрактов, извлечение датасета и интеграционную проверку. Некоторые действия
меняют локальное окружение: ``run_prototype`` запускает Docker Compose,
``start_backend`` собирает Sphinx и запускает backend, а ``verify_running``
переключает backend в replay.

Полный запуск прототипа
-----------------------

.. automodule:: scripts.run_prototype
   :members:

Локальный запуск сервисов
-------------------------

.. automodule:: scripts.run_local
   :members:

Генерация OpenAPI-снимков
-------------------------

.. automodule:: scripts.export_openapi
   :members:

Запуск backend и сборка кода
----------------------------

.. automodule:: scripts.start_backend
   :members:

Проверка запущенного контура
----------------------------

.. automodule:: scripts.verify_running
   :members:

Извлечение датасета
-------------------

.. automodule:: scripts.extract_data
   :members:

Упаковка итогового проекта
--------------------------------------------------

.. automodule:: scripts.package_submission
   :members:

Собственный NDTP-эмулятор
--------------------------------------------------

.. automodule:: emulator.service
   :members:
