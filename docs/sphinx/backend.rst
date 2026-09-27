Backend и NDTP
==============

Backend принимает телеметрию, хранит оперативное состояние и предоставляет
REST API. Подробный контракт запросов и ответов находится в пользовательском
руководстве API; ниже — устройство реализации.

.. note::

   ``backend.app`` создаёт клиент ML и TCP-сервер NDTP в lifespan FastAPI.
   Импортируйте и запускайте приложение через ``uvicorn backend.app:app``;
   не вызывайте ``lifespan`` вручную.

Приложение диспетчера
---------------------

.. automodule:: backend.app
   :members: Telemetry, Point, Mode, WhatIf, MapMatch, AdminSimulation, DriverCommand, DispatcherCreate, AssignmentUpdate, SimulationCreate, SimulationCancel, EmulatorControl, AdminAction, init_store, get_dispatcher, list_dispatchers, save_simulation, stored_simulations, save_driver_command, stored_driver_commands, live_track, align_schedule_to_event_day, clean, display_text, target_for, planned_position_at, stop_neighbors, route_risk, incidents, reserve_placement, calibrated_uncertainty, load_historical_snapshot, match_stop, estimate_position, forecast, ingest, on_ndtp, operational_vehicle, telemetry_fallback_active, historical_fallback_state, lifespan, custom_openapi, health, readiness, observability, dispatchers, dispatcher_profile, create_dispatcher, set_dispatcher_assignments, delete_dispatcher, telemetry, predict, mode, replay, get_state, get_incidents, get_risk, what_if, map_match, get_driver_commands, queue_driver_command, simulation_status, emulator_status, emulator_control, warm_official_source, execute_simulation, create_simulation, list_simulations, get_simulation, cancel_simulation, admin_simulation, network, metrics
   :show-inheritance:

NDTP-декодер
------------

.. _module-backend.ndtp:

.. automodule:: backend.ndtp
   :members:
   :show-inheritance:

OpenAPI-описания
----------------

.. automodule:: backend.api_docs
   :members:

Тикеты и debug-управление
----------------------------------------------

.. autofunction:: backend.app.get_action_center

.. autofunction:: backend.app.simulate_driver_command

.. autofunction:: backend.app.driver_command_acknowledgement

.. autofunction:: backend.app.set_debug_speed

.. autofunction:: backend.app.clear_debug_speed
