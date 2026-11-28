"""
Vendor conformance (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 8).

Every registered chiller, boiler and genset must pass these checks; a new
vendor isn't accepted until it does.  Plus the plant-side plumbing: the CSV
model column, vendor settings, fault aliases, BMS modes and Modbus vendors in
a BACnet plant.
"""

import json
import struct

import pytest

from hvacsim.models.components.boilers import GENERIC_FAULTS as BOILER_FAULTS
from hvacsim.models.components.boilers import MODE_ROLE, boiler_class
from hvacsim.models.components.chillers import GENERIC_FAULTS as CHILLER_FAULTS
from hvacsim.models.components.chillers import chiller_class
from hvacsim.models.components.chillers.york_yk import YorkYK
from hvacsim.models.components.gensets import ENGINE_EVENTS, PLANT_EVENTS, genset_class
from hvacsim.models.components.vendor import (STATES, _REGISTRY, bacnet_device, family_models, register,
                                              signal_defs)

CHILLERS = family_models("chiller")
BOILERS = family_models("boiler")
GENSETS = family_models("genset")

CH_CFG = {"tag": "CH-T", "pre": "cht", "sim_device": 1, "mac": 1, "capacity_tons": 1200.0, "chw_design_gpm": 2400.0,
          "cw_design_gpm": 3600.0, "chws_design_temp": 44.0, "kw_per_ton": 0.57, "min_load_pct": 10.0}
B_CFG = {"tag": "B-T", "pre": "bt", "sim_device": 2, "mac": 2, "input_mbh": 4000.0, "design_gpm": 400.0,
         "min_flow_gpm": 120.0, "turndown": 25.0}
G_CFG = {"tag": "GEN-T", "pre": "gent", "kw_rating": 1000.0, "volts": 480.0}


def _write_role(u, role, value):
    u.bas_write(type(u).ROLES[role], value)


# ============================================================================ chillers

def chiller(model, faults=None):
    cls = chiller_class(model)
    return cls("CH-T", {**CH_CFG, **cls.settings_for(CH_CFG)}, faults if faults is not None else set())


def run_ch(u, seconds, ewt=52.0, evap=2400.0, cond=3600.0, ecwt=80.0, h=5.0):
    for _ in range(int(seconds / h)):
        u.step(h, evap, cond, ewt, ecwt, 75.0)


def started(model, **kw):
    u = chiller(model, **kw)
    _write_role(u, "chws_sp", 44.0)
    _write_role(u, "demand_limit", 100.0)
    _write_role(u, "run_cmd", True)
    run_ch(u, 1800)
    return u


@pytest.mark.parametrize("model", CHILLERS)
def test_chiller_needs_run_command(model):
    u = chiller(model)
    _write_role(u, "chws_sp", 44.0)
    run_ch(u, 600)
    assert not u.running and u.kw == 0.0
    assert u.generic_state() in STATES


@pytest.mark.parametrize("model", CHILLERS)
def test_chiller_holds_setpoint_and_limit(model):
    u = started(model)
    assert u.running and u.generic_state() == "running"
    assert abs(u.lchwt - 44.0) < 1.0
    assert 600 < u.q / 12000.0 < 900                      # 2400 gpm x 8 °F = 800 tons
    fla = u.role("pct_load")
    _write_role(u, "demand_limit", round(0.6 * fla))
    run_ch(u, 900)
    assert u.role("pct_load") <= 0.6 * fla + 2.0
    assert u.lchwt > 44.5                                  # limited: can't hold setpoint


@pytest.mark.parametrize("model", CHILLERS)
def test_chiller_stops_on_evaporator_flow_loss_and_restarts(model):
    u = started(model)
    run_ch(u, 30, evap=0.0)
    assert not u.running and u.codes()
    run_ch(u, 600, evap=0.0)
    assert not u.running                                   # no restart without flow
    run_ch(u, 3600)                                        # flow back: restarts by its own rules
    assert u.running


@pytest.mark.parametrize("model", CHILLERS)
def test_chiller_outage_and_restart(model):
    u = started(model)
    u.powered = False
    run_ch(u, 60)
    assert not u.running and u.kw == 0.0 and u.generic_state() == "no_power"
    u.powered = True
    run_ch(u, 3600)
    assert u.running


@pytest.mark.parametrize("model", CHILLERS)
def test_chiller_generic_faults(model):
    cls = chiller_class(model)
    for g in CHILLER_FAULTS:
        assert cls.fault_key(g) in cls.FAULTS, g
    base = started(model)
    # flow switches and the starter trip it
    for g in ("evap_fs_failed", "cond_fs_failed", "starter_fault"):
        f = set()
        u = started(model, faults=f)
        f.add(cls.fault_key(g))
        run_ch(u, 60)
        assert not u.running, g
    # the starter fault latches until the cause is gone and the panel is reset
    f = set()
    u = started(model, faults=f)
    f.add(cls.fault_key("starter_fault"))
    run_ch(u, 60)
    assert u.alarm
    f.clear()
    run_ch(u, 10)
    u.panel("reset")
    assert not u.alarm
    # performance faults show in the refrigerant temperatures
    for g, check in (("low_charge", lambda u: u.evap_sat < base.evap_sat - 1.0),
                     ("evap_fouled", lambda u: u.evap_sat < base.evap_sat - 0.5),
                     ("cond_fouled", lambda u: u.cond_sat > base.cond_sat + 0.5)):
        f = set()
        u = started(model, faults=f)
        f.add(cls.fault_key(g))
        run_ch(u, 600)
        assert check(u), g


@pytest.mark.parametrize("model", CHILLERS)
def test_chiller_integration_list(model):
    cls = chiller_class(model)
    u = started(model)
    pts = u.points()
    assert {o[3] for o in cls.OBJECTS} <= set(pts)
    for role in ("run_cmd", "chws_sp", "demand_limit"):
        assert cls.ROLES[role] in cls.cmd_keys()
    assert u.role("lchwt") == pytest.approx(u.lchwt, abs=0.1)
    ins, sigs = signal_defs(cls, "cht", CH_CFG)
    assert len(ins) + len(sigs) == len(cls.OBJECTS)
    if cls.PROTOCOL == "bacnet":
        dev = bacnet_device(cls, CH_CFG, "chiller")
        assert len(dev["commands"]) + len(dev["monitors"]) == len(cls.OBJECTS)
    v = u.view()
    json.dumps(v)
    assert v["state"] in STATES and v["text"] and all(k in u.PANEL for k, _l in v["panel"])


# ============================================================================ boilers

def boiler(model, faults=None):
    cls = boiler_class(model)
    return cls("B-T", {**B_CFG, **cls.settings_for(B_CFG)}, faults if faults is not None else set(), None,
               t_init=120.0)


def run_b(u, seconds, gpm=400.0, ewt=140.0, h=1.0):
    states = set()
    for _ in range(int(seconds / h)):
        u.step(h, gpm, ewt, 70.0)
        states.add(u.generic_state())
    return states


def firing_cmd(u, rate=60.0, sp=170.0):
    cls = type(u)
    _write_role(u, "enable", True)
    if "rate" in cls.BMS_MODES:
        _write_role(u, "rate_cmd", rate)
    else:
        _write_role(u, "sp_cmd", sp)


@pytest.mark.parametrize("model", BOILERS)
def test_boiler_needs_enable_and_permissives(model):
    u = boiler(model)
    run_b(u, 120)
    assert not u.flame
    firing_cmd(u)
    for attr in ("louver_ok", "gas_ok"):
        setattr(u, attr, False)
        run_b(u, 120)
        assert not u.flame, attr
        setattr(u, attr, True)
    run_b(u, 120, gpm=0.0)
    assert not u.flame
    u.powered = False
    run_b(u, 120)
    assert not u.flame and u.generic_state() == "no_power"


@pytest.mark.parametrize("model", BOILERS)
def test_boiler_sequence_and_rate(model):
    u = boiler(model)
    firing_cmd(u, rate=60.0)
    states = run_b(u, 300)
    assert "starting" in states and u.flame and u.generic_state() == "running"
    if "rate" in type(u).BMS_MODES:
        assert abs(u.role("firing") - 60.0) < 3.0
    assert u.q > 0.5 * 0.6 * u.input_mbh * 1000.0
    assert u.pump_request


@pytest.mark.parametrize("model", BOILERS)
def test_boiler_comm_loss_stops_firing(model):
    u = boiler(model)
    firing_cmd(u)
    run_b(u, 300)
    u.comm_ok = False
    run_b(u, 900, h=5.0)
    assert not u.flame


@pytest.mark.parametrize("model", BOILERS)
def test_boiler_generic_faults(model):
    cls = boiler_class(model)
    for g in BOILER_FAULTS:
        assert cls.fault_key(g) in cls.FAULTS, g
    # lockouts: latched until the cause is gone and the display is reset
    for g, secs in (("ignition_fail", 300), ("high_limit_trip", 30), ("lwco_trip", 30), ("gas_valve", 120),
                    ("flue_blocked", 900)):
        f = set()
        u = boiler(model, faults=f)
        firing_cmd(u)
        if g in ("ignition_fail", "gas_valve"):
            f.add(cls.fault_key(g))
            run_b(u, secs)
        else:
            run_b(u, 300)
            f.add(cls.fault_key(g))
            run_b(u, secs, h=5.0)
        assert u.alarm and not u.flame, g
        f.clear()
        run_b(u, 5)
        u.panel("reset")
        assert not u.alarm, g
    # conditions that stop firing without latching
    for g in ("flow_sw_open", "supply_sensor_open", "supply_sensor_short", "return_sensor_open", "return_sensor_short"):
        f = set()
        u = boiler(model, faults=f)
        firing_cmd(u)
        run_b(u, 300)
        f.add(cls.fault_key(g))
        run_b(u, 60)
        assert not u.flame, g
    # stuck flow switch: fires with no flow; scaled HX: lower efficiency
    f = set()
    u = boiler(model, faults=f)
    f.add(cls.fault_key("flow_sw_stuck"))
    firing_cmd(u)
    run_b(u, 200, gpm=0.0)
    assert u.role("flow_switch") is True
    base = boiler(model)
    firing_cmd(base)
    run_b(base, 300)
    f = {cls.fault_key("hx_scaled")}
    u = boiler(model, faults=f)
    firing_cmd(u)
    run_b(u, 300)
    assert u.eff < base.eff - 0.02 and u.flue > base.flue + 20.0


@pytest.mark.parametrize("model", BOILERS)
def test_boiler_integration_list(model):
    cls = boiler_class(model)
    assert cls.BMS_MODES
    for m in cls.BMS_MODES:
        assert cls.ROLES[MODE_ROLE[m]] in cls.cmd_keys()
    u = boiler(model)
    firing_cmd(u)
    run_b(u, 300)
    assert {o[3] for o in cls.OBJECTS} <= set(u.points())
    assert u.role("supply_t") == pytest.approx(u.t_hx, abs=0.1)
    if cls.PROTOCOL == "bacnet":
        dev = bacnet_device(cls, B_CFG, "boiler")
        assert len(dev["commands"]) + len(dev["monitors"]) == len(cls.OBJECTS)
    json.dumps(u.view())


# ============================================================================ gensets

def genset(model, faults=None):
    cls = genset_class(model)
    return cls("GEN-T", {**G_CFG, **cls.settings_for(G_CFG)}, faults if faults is not None else set(), None)


def run_g(u, seconds, h=0.5, close=False, kw=0.0):
    for _ in range(int(seconds / h)):
        u.step(h, 15.0, 60.0 if u.cb_closed else None)
        if close and u.at_rated and u.start_request:
            u.cb_closed = True
        if u.cb_closed:
            u.kw, u.kvar = kw, 0.4 * kw


@pytest.mark.parametrize("model", GENSETS)
def test_genset_remote_start_and_stop(model):
    u = genset(model)
    assert u.in_auto and not u.running
    _write_role(u, "remote_start", 1)
    run_g(u, 60)
    assert u.at_rated and u.running and u.role("at_rated") and u.role("running")
    assert u.role("hz") == pytest.approx(60.0, abs=0.1)
    _write_role(u, "remote_start", 0)
    run_g(u, 10)
    assert not u.at_rated
    run_g(u, 900, h=2.0)
    assert not u.running and u.generic_state() == "off"


@pytest.mark.parametrize("model", GENSETS)
def test_genset_master_call_and_load(model):
    u = genset(model)
    u.master_call = True
    run_g(u, 60, close=True, kw=600.0)
    assert u.cb_closed and u.role("kw") == pytest.approx(600.0, abs=1.0)
    assert u.capacity() == pytest.approx(1000.0)


@pytest.mark.parametrize("model", GENSETS)
def test_genset_plant_events(model):
    cls = genset_class(model)
    for e in ENGINE_EVENTS + PLANT_EVENTS:
        assert e in cls.EVENT_CODES
    for e in PLANT_EVENTS:
        u = genset(model)
        u.master_call = True
        run_g(u, 60, close=True, kw=500.0)
        u.plant_event(e)
        assert u.plant_event_active(e) or u.shutdown, e
        assert u.display_code() == cls.EVENT_CODES[e], e
    u = genset(model)
    u.plant_event("overload")
    u.plant_event_clear("overload")
    assert not u.plant_event_active("overload")


@pytest.mark.parametrize("model", GENSETS)
def test_genset_generic_faults(model):
    cls = genset_class(model)
    for g in cls.GENERIC_FAULTS:
        assert cls.fault_key(g) in cls.FAULTS, g
    # faults found on a start
    for g in ("fail_to_crank", "fail_to_start", "overspeed", "low_gas"):
        u = genset(model, faults={cls.fault_key(g)})
        u.master_call = True
        run_g(u, 200)
        if g == "low_gas":
            assert u.shutdown or u.role("warning"), g
        else:
            assert u.shutdown and not u.running, g
    # faults found while running
    for g, secs in (("high_coolant", 1200), ("low_oil", 30), ("high_ac_volts", 30), ("low_ac_volts", 30),
                    ("high_current", 30)):
        f = set()
        u = genset(model, faults=f)
        u.master_call = True
        run_g(u, 60, close=True, kw=500.0)
        f.add(cls.fault_key(g))
        run_g(u, secs, h=1.0, close=True, kw=500.0)
        assert u.shutdown and not u.cb_closed, g
        assert u.role("shutdown") and u.role("fault_code"), g
        assert u.generic_state() == "lockout"
        # reset: cause gone, control in auto
        f.clear()
        u.master_call = False
        run_g(u, 5)
        assert u.fault_reset(), g
        assert not u.shutdown, g
    # warnings
    for g in ("low_battery", "charger_fail"):
        u = genset(model, faults={cls.fault_key(g)})
        run_g(u, 10)
        assert u.role("warning") and not u.shutdown, g


@pytest.mark.parametrize("model", GENSETS)
def test_genset_panel_and_reset_rules(model):
    u = genset(model, faults={"fail_to_crank"})
    u.master_call = True
    run_g(u, 200)
    assert u.shutdown
    for k in u.PANEL:
        assert isinstance(k, str)
    u.panel("off")
    assert not u.in_auto and not u.start_request
    u.faults.clear()
    u.panel("reset")
    u.panel("auto")
    assert not u.shutdown and u.in_auto


@pytest.mark.parametrize("model", GENSETS)
def test_genset_register_map(model):
    cls = genset_class(model)
    assert cls.PROTOCOL == "modbus_tcp"
    u = genset(model)
    u.master_call = True
    run_g(u, 60, close=True, kw=700.0)
    regs = u.modbus_registers(0.85)
    assert all(0 <= v <= 0xFFFF for v in regs.values())
    addrs = [o[1] for o in cls.OBJECTS]
    assert all(a in regs for a in addrs)
    # the gateway's block reads: no holes inside the parametric block
    block = [a for a in addrs if a < min(cls.role_register(r) for r in ("remote_start", "fault_reset"))]
    assert all(a in regs for a in range(min(block), max(block) + 1))
    # writes through the map
    u2 = genset(model)
    assert u2.modbus_write(cls.role_register("remote_start"), 1)
    run_g(u2, 60)
    assert u2.at_rated


# ============================================================================ plants: selection and plumbing

def test_unknown_model_and_settings_are_configuration_errors():
    with pytest.raises(ValueError, match="unknown chiller model"):
        chiller_class("acme_9000")
    with pytest.raises(ValueError, match="no setting"):
        YorkYK.settings_for(CH_CFG, {"anti_recycle_minutes": 10})
    st = YorkYK.settings_for(CH_CFG, {"york_yk": {"anti_recycle_min": 10}, "trane_cvhf": {"x": 1}})
    assert st["anti_recycle_min"] == 10.0
    assert YorkYK.settings_for({**CH_CFG, "anti_recycle_min": 5})["anti_recycle_min"] == 5.0


def test_plant_vendor_settings_and_fault_aliases():
    from hvacsim.models.chw_plant import ChwPlant
    m = ChwPlant({"chiller": {"york_yk": {"anti_recycle_min": 12}}, "sensor_noise": 0.0}, seed=1)
    assert m.chillers[0]["unit"].anti_recycle_s == 720.0
    assert m.chillers[0]["model"] == "york_yk"
    m.set_fault("ch1_starter_fault", True)                # generic name: alias of the York's vsd_fault
    assert m.fault("ch1_vsd_fault") and m.fault("ch1_starter_fault")
    m.set_fault("ch1_comm_loss", True)
    assert m.device_offline("ch1")
    m.step(1.0)
    v = json.loads(m.signals["ch1.view"])
    assert v["short"] == "YORK YK" and v["panel"][0][0] == "remote"


def test_boiler_bms_mode_is_checked():
    from hvacsim.models.hw_plant import HwPlant
    with pytest.raises(ValueError, match="does not support BMS mode"):
        HwPlant({"boiler_bms_mode": "setpoint"}, seed=1)
    m = HwPlant({"sensor_noise": 0.0}, seed=1)
    m.set_fault("b1_comm_loss", True)
    assert m.fault("b1_gateway_offline") and m.device_offline("b1")


def test_unknown_model_in_csv_row():
    from hvacsim.models.gen_plant import GenPlant
    with pytest.raises(ValueError, match="unknown genset model"):
        GenPlant({}, layout={"generators": [{"tag": "GEN-1", "unit_id": 1, "model": "acme"}]}, seed=1)


# ----- a Modbus chiller in the (BACnet) chilled water plant

@pytest.fixture
def modbus_chiller():
    """A test-only vendor: the York's behaviour behind a Modbus register map."""
    hr = "holding-register"

    class ModbusYK(YorkYK):
        MODEL = "test_modbus_yk"
        DISPLAY = "Test Modbus chiller"
        SHORT = "Test Modbus"
        PROTOCOL = "modbus_tcp"
        OBJECTS = [(hr, 40001, "Run command", "rem_run", None, "cmd"),
                   (hr, 40002, "CHW setpoint (x10)", "rem_sp", None, "cmd", 10.0),
                   (hr, 40003, "Demand limit", "rem_cur_limit", None, "cmd"),
                   (hr, 40010, "Leaving CHW (x10)", "lchwt", None, None, 10.0),
                   (hr, 40011, "Entering CHW (x10)", "echwt", None, None, 10.0),
                   (hr, 40012, "Leaving CW (x10)", "lcwt", None, None, 10.0),
                   (hr, 40013, "Entering CW (x10)", "ecwt", None, None, 10.0),
                   (hr, 40014, "% FLA", "pct_fla", None, None),
                   (hr, 40015, "Input kW", "kw", None, None),
                   (hr, 40016, "Running", "motor_run", None, None),
                   (hr, 40020, "Operation code", "op_code", None, None)]

    register(ModbusYK)
    yield ModbusYK
    del _REGISTRY["chiller"]["test_modbus_yk"]


def test_modbus_vendor_in_bacnet_plant(modbus_chiller):
    from hvacsim.modbus_tcp import ModbusTcpServer
    from hvacsim.models.chw_plant import ChwPlant
    lay = {"chillers": [{"tag": "CH-1", "controller_device": 241001, "sim_device": 4010031, "mac": 31},
                        {"tag": "CH-2", "controller_device": 241001, "model": "test_modbus_yk", "unit_id": 2}]}
    m = ChwPlant({"sensor_noise": 0.0}, layout=lay, seed=1)
    assert [d["tag"] for d in m.bacnet_devices()] == ["CH-1"]             # only the BACnet one
    units = m.modbus_units()
    assert list(units) == [2]
    srv = ModbusTcpServer(units)

    def read(reg, n=1):
        resp = srv.handle(2, struct.pack(">BHH", 3, reg - 40001, n))
        assert not resp[0] & 0x80
        return list(struct.unpack(f">{n}H", resp[2:]))

    def write(reg, v):
        resp = srv.handle(2, struct.pack(">BHH", 6, reg - 40001, v))
        assert not resp[0] & 0x80

    write(40002, 420)                                    # 42.0 °F
    write(40001, 1)
    m.step(1.0)
    assert m.inputs["ch2.rem_sp"] == 42.0 and m.inputs["ch2.rem_run"] in (1, 1.0, True)
    assert m.chillers[1]["unit"].rem_run and m.chillers[1]["unit"].setpoint == 42.0
    r = read(40001, 20)                                   # one block, holes read 0
    assert r[40010 - 40001] == round(m.chillers[1]["unit"].lchwt * 10)
    m.set_fault("ch2_comm_loss", True)
    resp = srv.handle(2, struct.pack(">BHH", 3, 40009, 1))
    assert resp[0] & 0x80                                 # no response from the gateway -> exception
    with pytest.raises(ValueError, match="unit_id"):
        ChwPlant({}, layout={"chillers": [{"tag": "CH-9", "controller_device": 241001,
                                           "model": "test_modbus_yk"}]}, seed=1).modbus_units()
