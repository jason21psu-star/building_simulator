"""Offline tests of the unit heater process model (no network needed)."""

import pytest

from hvacsim.models import MODELS
from hvacsim.models.unit_heater import HotWaterUnitHeater


def make(**params):
    return HotWaterUnitHeater(params, seed=1)


def run(m, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        m.step(dt)


def first_time(m, predicate, limit, dt=1.0):
    t = 0.0
    while t < limit:
        m.step(dt)
        t += dt
        if predicate(m.signals):
            return t
    return None


def test_registry():
    assert MODELS["hw_unit_heater"] is HotWaterUnitHeater


def test_idle_unit_stays_off():
    m = make()
    run(m, 600)
    assert not m.signals["aquastat_closed"]
    assert m.signals["fan_amps"] < 0.1
    assert m.signals["space_temp_actual"] < 64.0  # cooling toward OA


def test_call_for_heat_starts_fan_via_aquastat():
    m = make()
    m.set_input("valve_cmd", True)
    t = first_time(m, lambda s: s["aquastat_closed"], 300)
    assert t is not None and 20 < t < 120, t
    assert m.signals["pipe_temp"] >= 90
    run(m, 30)
    assert m.signals["fan_running"]
    assert 1.8 < m.signals["fan_amps"] < 2.2
    run(m, 1200)
    assert m.signals["space_temp_actual"] > 66  # it actually heats


def test_fan_runs_on_after_valve_closes_until_aquastat_opens():
    m = make()
    m.set_input("valve_cmd", True)
    run(m, 600)
    m.set_input("valve_cmd", False)
    t = first_time(m, lambda s: not s["aquastat_closed"], 3600)
    assert t is not None and 180 < t < 1500, t  # minutes of run-on, not seconds or hours
    assert m.signals["pipe_temp"] <= 80.5
    run(m, 30)
    assert not m.signals["fan_running"]
    assert m.signals["fan_amps"] < 0.1


def test_inrush_current_on_start():
    m = make()
    m.set_input("valve_cmd", True)
    first_time(m, lambda s: s["motor_energized"], 300, dt=0.1)
    peak = 0.0
    for _ in range(30):
        m.step(0.1)
        peak = max(peak, m.motor_amps)
    assert peak > 4.0


@pytest.mark.parametrize("fault", ["aquastat_failed_open", "disconnect_off", "valve_stuck_closed"])
def test_faults_that_prevent_fan(fault):
    m = make()
    m.set_fault(fault, True)
    m.set_input("valve_cmd", True)
    run(m, 600)
    assert not m.signals["fan_running"]


def test_aquastat_failed_closed_runs_fan_without_heat():
    m = make()
    m.set_fault("aquastat_failed_closed", True)
    run(m, 60)
    assert m.signals["fan_running"] and m.signals["heat_output"] < 1000


def test_low_hw_supply_just_below_aquastat():
    m = make()
    m.set_fault("hw_supply_low", True, 85.0)
    m.set_input("valve_cmd", True)
    run(m, 900)
    assert m.signals["valve_position"] == 100 and not m.signals["aquastat_closed"]


def test_leaking_valve_keeps_fan_running():
    m = make()
    m.set_fault("valve_leak", True, 0.15)
    m.set_input("valve_cmd", True)
    run(m, 300)
    m.set_input("valve_cmd", False)
    run(m, 3600)
    assert m.signals["aquastat_closed"] and m.signals["fan_running"]


def test_ct_failure_hides_running_motor():
    m = make()
    m.set_input("valve_cmd", True)
    run(m, 300)
    m.set_fault("ct_failed_zero", True)
    m.step(1)
    assert m.signals["fan_running"] and m.signals["fan_amps"] == 0.0
    m.set_fault("ct_failed_zero", False)
    m.set_fault("ct_failed_high", True)
    m.step(1)
    assert m.signals["fan_amps"] == 10.0


def test_locked_rotor_trips_protector_and_retries():
    m = make()
    m.set_fault("motor_locked_rotor", True)
    m.set_input("valve_cmd", True)
    t = first_time(m, lambda s: s["motor_energized"], 300, dt=0.5)
    assert m.motor_amps > 5.5 and not m.signals["fan_running"]
    t = first_time(m, lambda s: s["protector_tripped"], 60, dt=0.5)
    assert t is not None
    t = first_time(m, lambda s: not s["protector_tripped"], 400, dt=1.0)
    assert t is not None and 150 < t < 220


def test_sensor_faults():
    m = make()
    m.set_fault("space_sensor_offset", True, 5.0)
    m.step(1)
    assert abs(m.signals["space_temp"] - m.signals["space_temp_actual"] - 5.0) < 0.3
    m.set_fault("space_sensor_open", True, -40.0)
    m.step(1)
    assert m.signals["space_temp"] == -40.0


def test_change_order_fan_command():
    m = make(fan_control="controller_and_aquastat")
    m.set_input("valve_cmd", True)
    run(m, 300)
    assert m.signals["aquastat_closed"] and not m.signals["fan_running"]
    m.set_input("fan_cmd", True)
    run(m, 10)
    assert m.signals["fan_running"]


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        make(not_a_param=1)
