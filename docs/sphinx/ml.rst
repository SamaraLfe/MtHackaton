Модель и признаки
=================

ML запускается как самостоятельный FastAPI-сервис ``ml.service:app``. Backend
передаёт ему исходную точку, причинную историю и план остановок. Модель
``champion-b4b`` строит через ``build_v5_row`` 60 причинных GPS-признаков.
Идентификаторы транспорта и остановки в модель не передаются. Обучаемая цель —
непосредственно
``target_delay_s``; ``cur_dev_s`` является причинным признаком и не прибавляется
к прогнозу повторно.

Сервис инференса
----------------

.. automodule:: ml.service
   :members:
   :show-inheritance:

Загрузка и запуск модели
------------------------

.. automodule:: ml.model
   :members:
   :show-inheritance:

Компактные причинные признаки
-----------------------------

.. automodule:: ml.features
   :members:
   :show-inheritance:

Признаки модели
---------------

.. automodule:: ml.feature_builder
   :members: normalize_bool, haversine_m, parse_point, prepare_traffic, prepare_schedule, build_feature_row, build_feature_table, build_v5_row

Обучение
--------

.. automodule:: ml.train
   :members:
