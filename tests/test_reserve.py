import copy

import pytest
from pydantic import ValidationError

from backend.reserve import ReservePolicy, compensation


def scenario(delay=180, **policy):
    stops=[{'stop_id':i,'ts':i*100.0} for i in range(1,6)]
    vehicle={'prediction_s':delay,'late_probability':.8}
    return compensation(stops,[100,200,300,400,500],0,vehicle,1,ReservePolicy(**policy))


def test_delay_boundary_and_midpoint():
    assert not scenario(179.99)['justified']
    result=scenario()
    assert result['justified']
    assert result['start_index']==1  # Tie resolves to the earlier stop.
    assert result['stops'][1]['reserve_eta_s']==0
    assert result['compensated_stops']==4
    assert result['compensation_s']==720
    assert result['expected_compensation_s']==576
    assert result['benefit_probability']==.8
    assert result['stops'][0]['compensation_s']==0


def test_policy_thresholds_and_invalid_values():
    assert not scenario(min_compensated_stops=5)['justified']
    assert not scenario(min_expected_compensation_s=577)['justified']
    assert not scenario(min_benefit_probability=.81)['justified']
    for value in (179,float('nan'),float('inf')):
        with pytest.raises(ValidationError):
            ReservePolicy(min_delay_s=value)


@pytest.mark.parametrize('override',[{'stale':True},{'degraded':True},{'trip_status':'completed'},
                                     {'prediction_s':None},{'prediction_s':-50},{'connection_state':'waiting'}])
def test_no_benefit_without_evidence_and_inputs_immutable(override):
    vehicle={'prediction_s':300,'late_probability':.8,**override}
    original=copy.deepcopy(vehicle)
    result=compensation([{'ts':100}],[100],0,vehicle,.8,ReservePolicy())
    assert not result['justified']
    assert result['expected_compensation_s']==0
    assert vehicle==original


def test_end_of_route_and_zero_probability():
    assert not compensation([],[],0,{},1,ReservePolicy())['justified']
    result=compensation([{'ts':100}],[100],0,{'prediction_s':300,'late_probability':0},1,ReservePolicy())
    assert result['expected_compensation_s']==0
    assert not result['justified']


def test_midpoint_uses_distance_not_stop_count_and_probability_uses_confidence():
    result=compensation([{'ts':i*100} for i in range(1,5)], [10,20,900,1000],0,
                        {'prediction_s':300,'late_probability':.8},.5,ReservePolicy())
    assert result['start_index']==2
    assert result['benefit_probability']==.4
    assert result['expected_compensation_s']==240
