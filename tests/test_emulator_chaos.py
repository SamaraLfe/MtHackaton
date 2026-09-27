"""Seeded incidents affect movement, not packets or the ML output."""
from collections import Counter

import pytest

from emulator import service as emulator


def vehicle(number=1):
    return dict(tr_id=1000000+number,base_tr_id=number,
                path=[(37.,55.),(37.02,55.),(37.04,55.)],timings=[0.,600.,1200.])


def simulate(number=1,seed=42):
    route=vehicle(number);state=emulator.initial_vehicle_state(route,chaos_enabled=True,seed=seed)
    trace=[]
    for _ in range(200):
        position=emulator.advance_vehicle(route,state,elapsed_s=3)
        trace.append((position,state['chaos_phase'],state['elapsed_route_s']))
    return trace


def test_seeded_motion_is_reproducible_and_independent_per_vehicle():
    assert simulate()==simulate()
    assert simulate()!=simulate(2)
    assert simulate()!=simulate(seed=43)


def test_fleet_has_incidents_recovery_and_real_delay_variation():
    phases=Counter();delays=[]
    for number in range(1,14):
        trace=simulate(number)
        phases.update(row[1] for row in trace)
        delays.append(600-trace[-1][2])
    assert all(phases[phase]>0 for phase in ('normal','slow','stop','recovery'))
    assert max(delays)>120
    assert max(delays)-min(delays)>120


def test_stop_and_resume_do_not_teleport_or_break_speed_contract():
    trace=simulate()
    for before,after in zip(trace,trace[1:]):
        (lon,lat,speed),phase,elapsed=after
        assert 0<=speed<=130
        assert 37<=lon<=37.04 and lat==55
        if phase==before[1]=='stop':
            assert elapsed==before[2]
            assert (lon,lat)==before[0][:2]
            assert speed==0


def test_chaos_clock_is_independent_of_step_partitioning():
    one=emulator.initial_vehicle_state(vehicle(),chaos_enabled=True,seed=42)
    many=emulator.initial_vehicle_state(vehicle(),chaos_enabled=True,seed=42)
    total,_=emulator.chaotic_motion(one,600)
    pieces=sum(emulator.chaotic_motion(many,3)[0] for _ in range(200))
    assert total==pytest.approx(pieces,abs=1e-8)
    assert one['chaos_phase']==many['chaos_phase']


def test_debug_speed_overrides_incident_and_reset_resumes_incident(monkeypatch):
    route=vehicle();state=emulator.initial_vehicle_state(route,chaos_enabled=True)
    state.update(chaos_phase='stop',chaos_pace=0.,chaos_remaining_s=100.)
    monkeypatch.setitem(emulator.debug_speed_overrides,route['tr_id'],36.)
    start=state['lon'],state['lat']
    lon,lat,speed=emulator.advance_vehicle(route,state,elapsed_s=3)
    assert speed==36
    assert emulator.haversine_m(*start,lon,lat)==pytest.approx(30,abs=1e-5)
    emulator.debug_speed_overrides.pop(route['tr_id'])
    assert emulator.advance_vehicle(route,state,elapsed_s=3)==(lon,lat,0)


def test_chaos_can_be_disabled_without_changing_normal_movement():
    route=vehicle();state=emulator.initial_vehicle_state(route,chaos_enabled=False)
    emulator.advance_vehicle(route,state,elapsed_s=60)
    assert state['elapsed_route_s']==pytest.approx(60*state['pace_factor'])
    assert state['chaos_phase']=='normal'
