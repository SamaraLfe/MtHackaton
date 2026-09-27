from datetime import datetime, timezone
from pathlib import Path

from backend.ndtp import decode
from emulator.service import (
    advance_vehicle,
    build_nav00_frame,
    debug_speed_overrides,
    haversine_m,
    initial_vehicle_state,
    load_vehicles,
)


def test_emulator_builds_a_valid_ndtp_nav00_frame():
    sent_at = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    frame = build_nav00_frame(
        unit_id=985940,
        event_time=sent_at,
        lon=37.81,
        lat=55.75,
        speed_kmh=32,
    )

    events = decode(frame)

    assert len(events) == 1
    event = events[0]
    assert event['unit_id'] == 985940
    assert event['location_valid'] is True
    assert event['speed'] == 32
    assert abs(event['lon'] - 37.81) < 1e-7
    assert abs(event['lat'] - 55.75) < 1e-7
    assert event['event_time'] == sent_at.isoformat()


def test_emulator_loads_multiple_scheduled_vehicles():
    vehicles = load_vehicles('dataset')

    assert len(vehicles) >= 10
    assert len({vehicle['tr_id'] for vehicle in vehicles}) == len(vehicles)
    assert len({vehicle['unit_id'] for vehicle in vehicles}) == len(vehicles)
    assert all(len(vehicle['path']) >= 2 for vehicle in vehicles)


def test_emulator_advances_vehicle_smoothly_along_its_planned_path():
    vehicle = load_vehicles('dataset')[0]
    state = initial_vehicle_state(vehicle)
    previous_lon, previous_lat, previous_speed = state['lon'], state['lat'], state['speed_kmh']

    for _ in range(5):
        lon, lat, speed = advance_vehicle(vehicle, state, elapsed_s=3)
        distance_m = haversine_m(previous_lon, previous_lat, lon, lat)
        assert 1 <= distance_m <= 45
        assert abs(speed - previous_speed) <= 2
        previous_lon, previous_lat, previous_speed = lon, lat, speed


def test_debug_speed_overlay_changes_only_the_custom_vehicle_motion():
    vehicle = load_vehicles('dataset')[0]
    state = initial_vehicle_state(vehicle)
    initial_position = (state['lon'], state['lat'])

    debug_speed_overrides[vehicle['tr_id']] = 0
    try:
        lon, lat, speed = advance_vehicle(vehicle, state, elapsed_s=3)
    finally:
        debug_speed_overrides.pop(vehicle['tr_id'], None)

    assert (lon, lat) == initial_position
    assert speed == 0


def test_compose_runs_the_multi_vehicle_emulator_against_backend():
    compose = Path('compose.yaml').read_text(encoding='utf-8')

    assert 'command: python -m emulator.service' in compose
    assert 'NDTP_HOST: backend' in compose
    assert 'EMULATOR_MAX_VEHICLES: "0"' in compose
