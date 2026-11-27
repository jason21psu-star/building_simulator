"""
Drive interface conformance (docs/DRIVE_INTERFACE_DESIGN.md, section 4): every
registered drive must pass these.  Then Yaskawa Z1000 specifics from its manuals.
"""

import asyncio

import pytest

from hvacsim.config import load_host_config
from hvacsim.models import MODELS
from hvacsim.models.components.drives import GENERIC_FAULTS, ROLES_OUT, build_drive, drive_class, drive_models, motor_for_hp

FAN = lambda s: 0.85 * s * s                     # noqa: E731  fan load, pu torque


def drive(model, **overrides):
    common = {"min_hz": 12.0, "accel_s": 20.0, "decel_s": 20.0, **motor_for_hp(10, 2.0)}
    return build_drive(model, common, "hardwired", overrides, set())


def run(v, seconds, h=0.1, load=FAN, **roles):
    for _ in range(int(round(seconds / h))):
        if roles:
            v.wire(**roles)
        v.step(h, load)


# ----- conformance: every drive -------------------------------------------------------------


@pytest.fixture(params=drive_models())
def model(request):
    return request.param


def test_registry_and_metadata(model):
    cls = drive_class(model)
    assert cls.MODEL == model and cls.DISPLAY and cls.SHORT
    assert set(GENERIC_FAULTS) <= set(cls.INJECTABLE)
    assert set(cls.PARAM_INFO) <= set(cls.DEFAULTS)
    assert {"hand", "off", "auto", "reset", "hand_ref"} <= set(cls.KEYPAD)
    assert "hardwired" in cls.PRESETS
    with pytest.raises(ValueError):
        build_drive(model, overrides={"no_such_parameter": 1})


def test_start_follows_reference_and_stops(model):
    v = drive(model)
    run(v, 40, start=True, speed_ref=75.0)
    assert v.freq == pytest.approx(45.0, abs=0.3) and v.modulating
    assert v.role_out("running_out") and v.role_out("fault_out")
    assert v.role_out("speed_fb") == pytest.approx(75.0, abs=1.0)
    assert 0 < v.power_kw < 7.5 and 1 < v.current < 14
    run(v, 5, start=True, speed_ref=10.0)
    assert v.freq >= 11.9                                       # minimum frequency (common min_hz)
    run(v, 30, start=False, speed_ref=75.0)
    assert not v.role_out("running_out") and v.freq == 0.0
    for role in ROLES_OUT:
        v.role_out(role)


def test_safeties_stop_and_restart(model):
    v = drive(model)
    run(v, 30, start=True, speed_ref=80.0)
    run(v, 0.5, start=True, safeties=False, speed_ref=80.0)
    assert v.freq == 0.0                                        # coast: output off at once
    assert v.warnings
    run(v, 30, start=True, speed_ref=80.0)
    assert v.freq > 40                                          # restarts by itself (start still on)


def test_generic_faults_trip_and_reset(model):
    for name in ("overcurrent_trip", "earth_leakage", "mains_undervoltage"):
        v = drive(model)
        run(v, 20, start=True, speed_ref=80.0)
        v.inject.add(name)
        run(v, 1, start=True, speed_ref=80.0)
        assert v.fault_code, name
        assert not v.role_out("fault_out") and v.freq == 0.0
        assert type(v).FAULT_TEXT.get(v.fault_code), (name, v.fault_code)
        v.inject.discard(name)
        run(v, 1, start=False)                                  # remove the run command, then reset
        v.keypad("reset")
        assert not v.fault_code and v.role_out("fault_out")


def test_cooling_fan_failure_overheats(model):
    v = drive(model)
    v.inject.add("cooling_fan_failed")
    run(v, 900, h=0.5, load=lambda s: 1.15 * s * s, start=True, speed_ref=100.0)
    assert v.fault_code                                         # excess temperature trip


def test_no_power_then_level_restart(model):
    v = drive(model)
    run(v, 30, start=True, speed_ref=80.0)
    v.powered = False
    run(v, 10, start=True, speed_ref=80.0)
    assert v.freq == 0.0 and not v.role_out("fault_out") and v.role_out("speed_fb") == 0.0
    v.powered = True
    run(v, 40, start=True, speed_ref=80.0)
    assert v.freq > 40 and not v.fault_code                     # level start, no latched fault


def test_keypad_hand_off_auto(model):
    v = drive(model)
    v.keypad("hand")
    v.keypad("hand_ref", 30.0)
    run(v, 30, start=False, speed_ref=0.0)
    assert v.freq == pytest.approx(30.0, abs=0.5)
    v.keypad("off")
    run(v, 30, start=True, speed_ref=80.0)
    assert v.freq == 0.0
    v.keypad("auto")
    run(v, 40, start=True, speed_ref=80.0)
    assert v.freq > 40


def test_standalone_fan_model(model):
    m = MODELS["vfd_fan"]({"drive_model": model}, seed=1)
    names = {d.name for d in m.INPUTS}
    assert "efb_idle_s" in names
    assert m.signals["drive_model"] == drive_class(model).DISPLAY
    for f in drive_class(model).INJECTABLE:
        assert f in m.faults


# ----- Yaskawa Z1000 specifics ------------------------------------------------------------------


def z(**ov):
    return drive("z1000", **ov)


def test_z1000_terminal_layout_and_roles():
    v = z()
    p = v.p
    assert [p[f"h1_0{i}"] for i in range(1, 8)] == ["40", "41", "B1", "B2", "AF", "4", "6"]   # HVAC Initialization
    assert (p["h2_01"], p["h2_02"], p["h2_03"]) == ("0", "1", "B2")
    assert (p["b1_01"], p["b1_02"], p["b1_03"], p["h5_04"], p["h5_09"]) == (1, 1, 1, 3, 2.0)
    assert p["d2_02"] == pytest.approx(20.0)                  # 12 Hz of E1-04 60 Hz
    v.wire(start=True, safeties=True, damper_interlock=True, speed_ref=50.0)
    assert v.di["s1"] and v.di["s3"] and v.di["s4"] and not v.di["s5"]


def test_z1000_bas_interlock_with_damper_relay():
    v = z()
    run(v, 2, start=True, damper_interlock=False, speed_ref=80.0)
    assert v.freq == 0.0 and v.baseblock and any("inTLK" in w for w in v.warnings)
    assert v.role_out("damper_out")                             # M5-M6 closes on the run command: open the damper
    run(v, 30, start=True, damper_interlock=True, speed_ref=80.0)   # end switch made
    assert v.freq > 40 and not v.baseblock


def test_z1000_customer_safeties_coast_and_safe_alarm():
    v = z(b1_03=0)                                              # even with ramp-to-stop selected ...
    run(v, 30, start=True, speed_ref=80.0)
    run(v, 0.2, start=True, safeties=False, speed_ref=80.0)
    assert v.freq == 0.0 and any("SAFE" in w for w in v.warnings)   # ... safeties force a coast stop


def test_z1000_fault_reset_ignored_with_run_command():
    v = z()
    run(v, 20, start=True, speed_ref=80.0)
    v.inject.add("overcurrent_trip")
    run(v, 0.5, start=True, speed_ref=80.0)
    v.inject.clear()
    assert v.fault_code == "oC" and v.relay("ma") and not v.relay("mb")
    v.keypad("reset")
    assert v.fault_code == "oC"                                 # run command still present
    run(v, 0.2, start=False)
    v.keypad("reset")
    assert not v.fault_code


def test_z1000_emergency_override_ignores_bas_interlock():
    v = z(s6_01=45.0)
    v.wire(start=False, damper_interlock=False)
    v.di["s5"] = True                                           # AF Emergency Override Forward Run
    for _ in range(400):
        v.step(0.1, FAN)
    assert v.override and v.freq == pytest.approx(45.0, abs=0.3)
    assert v.status_text() == "EMERGENCY OVERRIDE"


def test_z1000_external_fault_and_drive_enable():
    v = z(h1_06="24")                                           # S6: external fault N.O., always, coast
    run(v, 20, start=True, speed_ref=80.0)
    v.di["s6"] = True
    v.step(0.1, FAN)
    assert v.fault_code == "EF6"
    v2 = z(h1_07="6A")                                          # S7: drive enable
    for _ in range(50):
        v2.wire(start=True, speed_ref=80.0)
        v2.di["s7"] = False
        v2.step(0.1, FAN)
    assert v2.freq == 0.0 and any("dnE" in w for w in v2.warnings)
    for _ in range(50):
        v2.wire(start=True, speed_ref=80.0)                   # enable closes, run still on ...
        v2.step(0.1, FAN)
    assert v2.freq == 0.0                                       # ... 6A needs a new run command
    run(v2, 0.2, start=False)
    run(v2, 20, start=True, speed_ref=80.0)
    assert v2.freq > 30


def test_z1000_comm_error_alarm_by_default_fault_when_set():
    for h5_04, faults in ((3, False), (1, True)):
        v = z(b1_01=2, b1_02=2, h5_04=h5_04)
        v.fb.update(run=True, freq_hz=50.0)
        run(v, 30)
        assert v.freq == pytest.approx(50.0, abs=0.3)
        v.fb_idle_s = 3.0                                       # > H5-09 2.0 s
        run(v, 1)
        assert bool(v.fault_code == "CE") is faults
        assert any("CE" in w for w in v.warnings) or faults
        assert (v.freq > 40) is (not faults)


def test_z1000_run_at_power_up_b1_17():
    v = z(b1_17=0)
    run(v, 20, start=True, speed_ref=80.0)
    v.powered = False
    run(v, 10, start=True, speed_ref=80.0)
    v.powered = True
    run(v, 20, start=True, speed_ref=80.0)
    assert v.freq == 0.0                                        # needs the run command cycled
    run(v, 0.2, start=False)
    run(v, 30, start=True, speed_ref=80.0)
    assert v.freq > 40


def test_z1000_locked_rotor_trips_motor_overload():
    v = z()
    run(v, 120, h=0.5, load=lambda s: 3.0, start=True, speed_ref=80.0)
    assert v.fault_code in ("oL1", "oL2")


def test_z1000_overvoltage_without_decel_stall_prevention():
    v = z(l3_04=0, c1_02=2.0)
    v.p["inertia_h"] = 8.0
    run(v, 60, start=True, speed_ref=100.0)
    v.p["b1_03"] = 0                                            # ramp to stop
    run(v, 3, start=False)
    assert v.fault_code == "ov"


# ----- equipment with a Z1000 ---------------------------------------------------------------------


def test_ahu_fan_array_on_z1000():
    from hvacsim import psychro
    m = MODELS["ahu_vav_econ"]({"fan_drive_model": "z1000", "oat": 55.0, "oa_w": psychro.w_from_rh(55.0, 50.0, psychro.p_atm(0)),
                                "clock_start": "2026-11-23 10:00"}, seed=1)
    for s in ("sf1", "sf2", "sf3", "sf4"):
        m.set_input(f"{s}_ss", True)
        m.set_input(f"{s}_vfd_spd", 80.0)
    m.set_input("rf_ss", True)
    m.set_input("rf_vfd_spd", 64.0)
    m.set_input("ma_dmpr", 36.0)
    for _ in range(120):
        m.step(1.0)
    assert m.signals["sf1_freq"] == pytest.approx(48.0, abs=0.5) and m.signals["sf1_vfd_flt"]
    assert "(Z1000)" in [i for _n, i, _v in m.param_items() if _n.startswith("sf1.")][0].group
    m.set_param("sf1.c1_01", 45.0)
    with pytest.raises(KeyError):
        m.set_param("sf1.accel_time_1", 45.0)                   # an ACH580 parameter name


def test_plant_csv_drive_model_column():
    from hvacsim.models.chw_plant import ChwPlant, DEFAULT_LAYOUT
    lay = {"primary_pumps": [{**r, "drive_model": "z1000"} if r["tag"] == "PCHWP-2" else r
                             for r in DEFAULT_LAYOUT["primary_pumps"]]}
    m = ChwPlant({"clock_start": "2026-07-15 10:00", "pchwp_drive": {"z1000": {"c1_01": 45.0}}}, layout=lay, seed=1)
    assert type(m.drives["pchwp2"]).MODEL == "z1000" and type(m.drives["pchwp1"]).MODEL == "ach580"
    assert m.drives["pchwp2"].p["c1_01"] == 45.0
    assert "pchwp2.b1_03" in m.PARAM_INFO and "pchwp1.stop_mode" in m.PARAM_INFO
    m.inputs["pchwp2_ss"], m.inputs["pchwp2_vfd_spd"] = True, 91.7
    for _ in range(30):
        m.step(5.0)
    assert m.signals["pchwp2.freq"] == pytest.approx(55.0, abs=0.3) and m.signals["pchwp2_vfd_flt"]


def test_station_sf3_z1000_over_bacnet():
    asyncio.run(_sf3())


async def _sf3():
    from hvacsim.runner import Host
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "SF-3-VFD", "SF-4-VFD")]
    cfg["host"]["speed"] = 5.0
    host = Host(cfg, demo=True)
    task = asyncio.create_task(host.run())
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + 30
        while loop.time() < end:
            await asyncio.sleep(0.5)
            if all(host.unit(n).model.signals["running"] for n in ("SF-3-VFD", "SF-4-VFD")):
                break
        for n in ("SF-3-VFD", "SF-4-VFD"):
            u = host.unit(n)
            assert u.model.signals["running"], (n, u.model.signals["status_text"], u.model.signals["warnings"])
            assert type(u.model.vfd).MODEL == "z1000"
        bad = [(p.object_name, p.last_error) for p in host.unit("SF-3-VFD").link.points if not p.ok]
        assert not bad, bad
    finally:
        task.cancel()
        host.close()
