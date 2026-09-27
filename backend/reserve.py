"""Stop-based reserve scenario; never modifies the primary vehicle forecast."""
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field


class ReservePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    min_delay_s: float = Field(default=180, ge=180)
    min_benefit_probability: float = Field(default=.35, ge=0, le=1)
    min_compensated_stops: int = Field(default=1, ge=1)
    min_expected_compensation_s: float = Field(default=60, ge=0)
    min_confidence: float = Field(default=.25, ge=0, le=1)


def compensation(stops, distances, current_plan_ts, vehicle, confidence, policy):
    """Distances are cumulative metres from the current position to future stops.

    Reserve appears instantly at the nearest stop to half the remaining distance,
    then follows scheduled segment durations. Each stop has equal weight (no
    passenger counts available). Probability is a scenario heuristic, not ML.
    """
    forecast = vehicle.get('prediction_s')
    valid = forecast is not None and isfinite(float(forecast))
    delay = max(0., float(forecast)) if valid else 0.
    start = min(range(len(stops)), key=lambda i: abs(distances[i] - distances[-1]/2)) if stops else None
    probability = vehicle.get('late_probability')
    probability = float(probability) if probability is not None else 0.
    probability = max(0., min(1., probability)) if isfinite(probability) else 0.
    blockers = []
    if not valid or delay < policy.min_delay_s:
        blockers.append(f'Прогноз задержки меньше {policy.min_delay_s:g} секунд')
    if not stops or vehicle.get('trip_status') == 'completed':
        blockers.append('Нет оставшихся остановок')
    if confidence < policy.min_confidence:
        blockers.append('Низкая уверенность сопоставления с плановой траекторией')
    if vehicle.get('stale') or vehicle.get('connection_state') in {'offline', 'waiting'}:
        blockers.append('Нет свежей телеметрии')
    if vehicle.get('degraded'):
        blockers.append('Прогноз работает в fallback-режиме')
    eligible = not blockers
    rows = []
    for i, stop in enumerate(stops):
        planned = max(0., stop['ts'] - current_plan_ts)
        covered = start is not None and i >= start
        eta = stop['ts'] - stops[start]['ts'] if covered else None
        # An early reserve waits for the planned service time.
        service = max(planned, eta) if covered else None
        saved = min(delay, max(0., planned + delay - service)) if covered and eligible else 0.
        rows.append({**stop, 'planned_eta_s':planned, 'primary_eta_s':planned+delay,
                     'reserve_eta_s':eta, 'reserve_service_eta_s':service,
                     'compensation_s':saved})
    count = sum(row['compensation_s'] > 0 for row in rows)
    total = sum(row['compensation_s'] for row in rows)
    benefit_probability = probability * confidence if eligible and total > 0 else 0.
    expected = total * benefit_probability
    if benefit_probability < policy.min_benefit_probability:
        blockers.append('Сценарная вероятность пользы ниже порога')
    if count < policy.min_compensated_stops:
        blockers.append('Недостаточно компенсированных остановок')
    if expected < policy.min_expected_compensation_s:
        blockers.append('Ожидаемая компенсация ниже порога')
    return {'start_index':start, 'stops':rows, 'compensated_stops':count,
            'compensation_s':total, 'expected_compensation_s':expected,
            'benefit_probability':benefit_probability, 'blockers':blockers,
            'justified':not blockers, 'policy':policy.model_dump()}
