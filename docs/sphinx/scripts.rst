Скрипты проекта
===============

Скрипты не входят в runtime API, но задают воспроизводимый запуск, извлечение
датасета и интеграционную проверку. Некоторые действия меняют локальное
окружение: ``run_prototype`` запускает Docker Compose, а ``verify_running``
переключает backend в replay.

Полный запуск прототипа
-----------------------

.. automodule:: scripts.run_prototype
   :members:

Локальный запуск сервисов
-------------------------

.. automodule:: scripts.run_local
   :members:

Проверка запущенного контура
----------------------------

.. automodule:: scripts.verify_running
   :members:

Извлечение датасета
-------------------

.. automodule:: scripts.extract_data
   :members:
