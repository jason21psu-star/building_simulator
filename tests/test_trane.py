"""Trane CenTraVac CVHE with Symbio 800 (docs/TRANE_CVHE_DESIGN.md): sequence, diagnostics, integration."""

import struct

import pytest

from hvacsim.models.components.chillers.trane_cvhe import DIAGNOSTICS, TraneCVHE, r514a_psia
from hvacsim.models.components.vendor import bacnet_device

CFG = {"tag": "CH-3", "pre": "ch3", "sim_device": 4010033, "mac": 33, "capacity_tons": 1200.0,
       "chw_design_gpm": 2400.0, "cw_design_gpm": 3600.0, "chws_design_temp": 44.0, "kw_per_ton": 0.57,
       "min_load_pct": 10.0}


def chiller(faults=None, **settings):
    return TraneCVHE("CH-3", {**CFG, **settings}, faults if faults is not None else set())


def run(u, seconds, ewt=52.0, evap=2400.0, cond=3600.0, h=1.0):
    for _ in range(int(seconds / h)):
        u.step(h, evap, cond, ewt, 80.0, 75.0)


def started(faults=None):
    u = chiller(faults)
    run(u, 200)
    assert u.state == "running", u.status_text()
    return u


def test_power_up_auto_and_differential_to_start():
    u = chiller()
    u.lchwt = u.echwt = 45.0
    run(u, 10, ewt=45.0)
    assert u.state == "boot" and u.status_text() == "Power Up"          # controller boot 15 s
    run(u, 120, ewt=45.0)                                              # 45 < 44 + 2: no need to cool
    assert u.state == "auto" and u.evap_pump_req and not u.cond_pump_req
    assert "Waiting to Start" in u.status_text()
    run(u, 30, ewt=47.0)
    assert u.state == "starting" and u.cond_pump_req


def test_start_sequence_prelube_and_wye_delta():
    u = chiller()
    run(u, 20)
    assert u.state == "starting" and u.sub in ("cond_flow", "prelube")
    run(u, 30)
    assert u.sub == "prelube" and u.oil_dp > 12.0 and not u.running
    run(u, 35)
    assert u.sub == "accel" and u.running and u.starts == 1             # 60 s of 12 psid oil, then the motor
    run(u, 10)
    assert u.state == "running" and u.points()["running_mode"] == 2    # MI-10100 3 = Run (code + 1)
    run(u, 1800)
    assert abs(u.lchwt - 44.0) < 1.0 and 0 < u.igv <= 100
    assert u.points()["evap_psig"] < 0                                  # R-514A: evaporator in a vacuum


def test_satisfied_setpoint_postlube_and_restart_inhibit():
    u = started()
    run(u, 600)
    run(u, 120, ewt=42.5)                                              # load gone: below 44 - 2
    assert u.state in ("preparing", "shutting")
    run(u, 60, ewt=42.5)
    assert u.state == "shutting" and not u.running and not u.cond_pump_req and u.evap_pump_req
    assert u.oil_dp > 5.0                                               # post-lube: the oil pump still runs
    run(u, 200, ewt=42.5)
    assert u.state == "auto"
    run(u, 60, ewt=52.0)                                               # needs cooling again within 20 min of the start
    assert u.state == "auto" and "restart_inhibit" in u.diags and u.restart_inhibit_left() > 0
    assert u.points()["restart_inhibit_s"] > 0
    run(u, 1500, ewt=52.0)
    assert u.running


def test_evaporator_flow_overdue_and_lost_are_non_latching():
    u = chiller()
    run(u, 300, evap=0.0)
    assert "evap_flow_overdue" in u.diags and u.last_diag == 31001 and not u.alarm
    run(u, 300)
    assert "evap_flow_overdue" not in u.diags and u.running or u.state == "starting"
    u = started()
    run(u, 10, evap=0.0)
    assert not u.running and "evap_flow_lost" in u.diags and u.last_diag == 31002
    assert u.points()["diag_evap_flow_lost"] and u.points()["diag_shutdown"]
    run(u, 30)
    assert "evap_flow_lost" not in u.diags                              # MAR: clears with flow


def test_latching_diagnostic_needs_reset_over_bacnet():
    f = set()
    u = started(f)
    f.add("starter_fault")
    run(u, 5)
    assert not u.running and u.alarm and u.last_diag == 211081
    p = u.points()
    assert p["diag_manual_reset"] and p["diag_starter_fault"] and u.generic_state() == "lockout"
    u.bas_write("reset_diag", True)
    run(u, 2)
    assert u.alarm                                                      # cause still present
    f.clear()
    u.bas_write("reset_diag", False)
    run(u, 2)
    u.bas_write("reset_diag", True)                                     # a new write of Reset
    run(u, 2)
    assert not u.alarm


def test_external_auto_stop_and_emergency_stop():
    u = started()
    u.set_hardwired("ext_auto_stop", False)
    run(u, 60)
    assert u.state in ("shutting", "stopped") and not u.running
    assert not u.points()["ext_auto_stop_bi"]
    run(u, 300)
    assert u.state == "stopped" and "External Auto-Stop" in u.status_text()
    u.set_hardwired("ext_auto_stop", True)
    u = started()
    u.set_hardwired("estop", False)
    run(u, 1)
    assert not u.running and "emergency_stop" in u.diags and u.last_diag == 71001
    u.panel("reset")
    assert "emergency_stop" in u.diags                                  # still open
    u.set_hardwired("estop", True)
    u.panel("reset")
    assert "emergency_stop" not in u.diags


def test_bas_stop_and_setpoint_arbitration():
    u = started()
    assert u.points()["sp_source"] == 2                                  # front panel (relinquish default)
    u.bas_write("chw_sp", 42.0)
    assert u.points()["sp_source"] == 0 and u.points()["active_sp"] == 42.0
    u.bas_write("demand_limit", 20.0)
    assert u.active_demand_limit == 40.0                                 # 40 % minimum
    u.bas_write("auto_cmd", False)
    run(u, 60)
    assert u.state in ("preparing", "shutting") and "Tracer" not in u.status_text() or not u.running
    run(u, 400)
    assert u.state == "stopped" and not u.evap_pump_req                  # evaporator pump off delay 1 min


def test_surge_low_oil_and_motor_faults_latch():
    for fault, diag in (("surge", "surge"), ("low_oil_pressure", "low_oil_dp"), ("motor_winding_hot", "motor_hot")):
        f = set()
        u = started(f)
        f.add(fault)
        run(u, 1500)
        assert diag in u.diags and u.alarm and not u.running, fault
    u = chiller({"low_oil_pressure"})
    run(u, 300)
    assert "oil_overdue" in u.diags or "low_oil_dp" in u.diags


def test_power_loss_and_restart():
    u = started()
    u.powered = False
    run(u, 30)
    assert not u.running and u.generic_state() == "no_power" and "power_loss" in u.diags
    u.powered = True
    run(u, 1500)                                                         # start-to-start 20 min
    assert u.running and "power_loss" not in u.diags


def test_refrigerant_and_integration_list():
    assert r514a_psia(84.2) == pytest.approx(14.696, rel=0.01)           # normal boiling point
    assert r514a_psia(40.0) < 7.0
    dev = bacnet_device(TraneCVHE, CFG, "chiller")
    objs = {o["name"]: o for o in dev["commands"] + dev["monitors"]}
    assert objs["Chiller Auto Stop Command BAS"]["object"] == "binary-value,10104"
    assert objs["Chiller Auto Stop Command BAS"]["relinquish_default"] is True
    assert objs["Chilled Water Setpoint"]["object"] == "analog-value,10100"
    assert objs["Evaporator Leaving Water Temperature"]["object"] == "analog-input,10107"
    assert objs["Unit Source ID (Last Diagnostic Code)"]["object"] == "analog-input,10118"
    assert objs["Running Mode"]["object"] == "multi-state-input,10100"
    assert objs["Running Mode"]["states"][2] == "Chiller In Run Mode"
    assert objs["Chiller Mode Command BAS"]["states"] == ["Cool", "Heat", "Ice Making", "Free Cooling"]
    assert objs["Diagnostic: Evaporator Water Flow Lost"]["object"] == "binary-input,11210"
    for key, (code, name, _t, inst, reg) in DIAGNOSTICS.items():
        assert objs[f"Diagnostic: {name}"]["object"] == f"binary-input,{inst}"


def test_modbus_variant_floats_and_multistate():
    from hvacsim.modbus_tcp import ModbusTcpServer
    from hvacsim.models.chw_plant import ChwPlant
    lay = {"chillers": [{"tag": "CH-1", "controller_device": 241001, "model": "trane_cvhe_modbus", "unit_id": 7}]}
    m = ChwPlant({"sensor_noise": 0.0}, layout=lay, seed=1)
    srv = ModbusTcpServer(m.modbus_units())
    hi, lo = struct.unpack(">HH", struct.pack(">f", 42.5))
    resp = srv.handle(7, struct.pack(">BHHBHH", 16, 40011 - 40001, 2, 4, hi, lo))   # Chilled Water Setpoint
    assert not resp[0] & 0x80
    m.step(1.0)
    assert m.inputs["ch1.chw_sp"] == pytest.approx(42.5)
    r = srv.handle(7, struct.pack(">BHH", 4, 30011 - 30001, 2))          # Active Chilled Water Setpoint
    assert struct.unpack(">f", r[2:6])[0] == pytest.approx(42.5)
    r = srv.handle(7, struct.pack(">BHH", 4, 32016 - 30001, 1))          # Refrigerant Type: 15 = R-514A
    assert struct.unpack(">H", r[2:4])[0] == 15
    r = srv.handle(7, struct.pack(">BHH", 3, 42011 - 40001, 1))          # Chiller Mode Command BAS: 1 = Cool
    assert struct.unpack(">H", r[2:4])[0] == 1


def test_mixed_plant_station_one():
    from hvacsim.config import load_host_config
    from hvacsim.models.chw_plant import ChwPlant
    cfg = load_host_config("configs/station-1.yaml")
    chp = next(i for i in cfg["instances"] if i["name"] == "CHP-1")
    m = ChwPlant(chp["equipment"].get("params") or {}, layout=chp["plant_layout"], seed=1)
    assert [c["model"] for c in m.chillers] == ["york_yk", "york_yk", "trane_cvhe"]
    assert {"ch3_ext_auto_stop", "ch3_estop"} <= set(m.io[241001]["bo"])
    v = __import__("json").loads((m.step(1.0) or m.signals)["ch3.view"])
    assert v["short"] == "Trane CVHE" and [k for k, _l in v["panel"]] == ["auto", "stop", "reset"]
