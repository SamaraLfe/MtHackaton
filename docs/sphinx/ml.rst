Модель и признаки
=================

ML запускается как самостоятельный FastAPI-сервис ``ml.service:app``. Backend
передаёт ему исходную точку, причинную историю и план остановок. В V5.3 модель
не принимает заранее вычисленный набор 15 признаков: она строит 104 сырых
признака через ``build_v5_row`` и передаёт модели 102 признака после
исключения идентификаторов. Обучаемая цель V5.3 — residual
``target_delay_s - cur_dev_s``; runtime возвращает уже восстановленный прогноз,
добавляя известное в момент ``T`` ``cur_dev_s``.

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

Признаки V5
-----------

.. automodule:: ml.v5_features
   :members: empty_feature_dict, normalize_bool, haversine_m, bearing_deg, parse_point, prepare_traffic, prepare_schedule, add_target_coordinates, make_basic_features, build_telemetry_features, build_route_features, build_feature_matrix, build_v5_row

Обучение
--------

.. automodule:: ml.train
   :members:
