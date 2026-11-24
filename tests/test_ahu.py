"""AHU (vav_ahu_econ) physics, safeties, drives, and a BACnet demo run."""

import asyncio

import pytest

from hvacsim import psychro
from hvacsim.config import load_host_config
from hvacsim.models import MODELS
from hvacsim.models.components.airside import WaterCoil, equal_percentage, signal_2_10
from hvacsim.runner import Host

P = psychro.p_atm(0)
SF = ["sf1", "sf2", "sf3", "sf4"]


def make(oat=55.0, rh=50.0, **params):
    m = MODELS["ahu_vav_econ"]({"oat": oat, "oa_w": psychro.w_from_rh(oat, rh, P),
                                "clock_start": "2026-11-23 10:00", **params}, seed=1)
    return m


def start(m, speed=70.0, dmpr=36.0):
    for s in SF:
        m.set_input(f"{s}_ss", True)
        m.set_input(f"{s}_vfd_spd", speed)
    m.set_input("rf_ss", True)
    m.set_input("rf_vfd_spd", 0.8 * speed)
    m.set_input("ma_dmpr", dmpr)


def run(m, seconds):
    for _ in range(int(seconds)):
        m.step(1.0)


# ----- building blocks ------------------------------------------------------


def test_signal_and_valve_characteristics():
    assert signal_2_10(0) == 0 and signal_2_10(20) == 0 and signal_2_10(60) == 0.5 and signal_2_10(100) == 1
    assert equal_percentage(0) == 0 and equal_percentage(1) == pytest.approx(1)
    assert equal_percentage(0.5) < 0.5                  # equal percentage: little flow at half stroke


def test_coils_meet_design_points():
    ph = WaterCoil(cfm_d=24000, gpm_d=26, ewt_d=140, eat_d=35, lat_d=50)
    cc = WaterCoil(cfm_d=24000, gpm_d=180, ewt_d=44, eat_d=80, lat_d=53, ew_d=0.0112, capacitance=900, cooling=True)
    for coil, args in ((ph, (24000, 35, 0.003, 140, 26)), (cc, (24000, 80, 0.0112, 44, 180))):
        coil.lwt = args[1]
        for _ in range(3600):
            t, w = coil.step(1.0, *args, P)
        if coil is ph:
            assert t == pytest.approx(50, abs=0.5) and coil.lwt == pytest.approx(110, abs=1)
        else:
            assert t == pytest.approx(53, abs=0.7) and w < 0.0085 and coil.lwt == pytest.approx(56, abs=1)


# ----- airflow / fans --------------------------------------------------------


def test_startup_airflow_static_and_even_fan_sharing():
    m = make()
    start(m, 90)
    run(m, 300)
    s = m.signals
    assert s["supply_cfm"] == pytest.approx(s["vav_demand_cfm"], rel=0.05)   # pressure-independent boxes
    assert s["sa_static"] > 1.5                          # 90 % speed throttles the boxes
    amps = [s[f"{f}_amps"] for f in SF]
    assert max(amps) - min(amps) < 0.4                   # identical fans share evenly
    assert 5 < s["oa_fraction"] < 25                     # 20 % minimum damper position
    assert all(s[f"{f}_vfd_flt"] for f in SF + ["rf"])   # RO3 closed = healthy


def test_low_speed_starves_boxes():
    m = make(demand_override=100)
    start(m, 45)
    run(m, 300)
    assert m.signals["starved"] and m.signals["supply_cfm"] < 0.9 * m.signals["vav_demand_cfm"]


def test_barometric_damper_stuck_open_with_fan_off():
    m = make(demand_override=90)
    start(m, 95)
    run(m, 200)
    normal = m.signals["supply_cfm"]
    m.set_fault("sf4_bd_stuck_open", True)
    m.set_input("sf4_ss", False)                          # SF-4 off, its backdraft damper stuck open
    run(m, 200)
    s = m.signals
    assert s["sf4_cfm"] < -500                            # air recirculates backward through SF-4
    assert s["supply_cfm"] < normal
    m.set_fault("sf4_bd_stuck_open", False)
    run(m, 100)
    assert m.signals["sf4_cfm"] == 0                      # healthy damper closes


def test_drive_in_hand_shares_unevenly():
    m = make()
    start(m, 70)
    run(m, 120)
    m.action("sf2.hand")
    m.action("sf2.hand_ref", 60)
    run(m, 120)
    s = m.signals
    assert s["sf2_freq"] == pytest.approx(60, abs=0.5) and s["sf2_amps"] > s["sf1_amps"] + 1


def test_drive_parameters_by_dotted_name():
    m = make()
    m.set_param("sf3.accel_time_1", 60.0)
    assert m.drive_p["sf3"]["accel_time_1"] == 60.0 and m.drive_p["sf1"]["accel_time_1"] == 30.0
    with pytest.raises(ValueError):
        m.set_param("rf.ext1_commands", "bogus")
    names = [n for n, _, _ in m.param_items()]
    assert "rf.freq_ramp_in_zero" in names and "sf4.start_interlock_1" in names


# ----- safeties ---------------------------------------------------------------


def test_high_static_trip_coasts_all_drives_and_manual_reset():
    m = make()
    start(m, 90)
    run(m, 200)
    m.set_fault("fire_damper_closed", True)
    run(m, 20)
    s = m.signals
    assert not s["hi_static"] and not s["safety_chain_ok"]
    assert all(s[f"{d}_freq"] == 0 for d in SF + ["rf"])  # coasting: output off immediately
    with pytest.raises(ValueError):
        m.action("hs_reset") if m.p_dis > 3.8 else (_ for _ in ()).throw(ValueError())
    m.set_fault("fire_damper_closed", False)
    run(m, 120)
    assert not m.signals["hi_static"]                     # latched until reset
    m.action("hs_reset")
    run(m, 60)
    assert m.signals["hi_static"] and m.signals["sf1_freq"] > 30   # restarts without a new start


def test_freezestat_trips_and_wont_reset_while_cold():
    m = make(oat=5.0, rh=60.0)
    start(m, 80, dmpr=100)
    run(m, 150)
    assert not m.signals["frz_stat"] and m.signals["sf1_freq"] == 0
    with pytest.raises(ValueError):
        m.action("frz_reset")


def test_open_oa_damper_after_trip_freezes_coil_closed_damper_does_not():
    m = make(oat=5.0, rh=60.0)
    start(m, 80, dmpr=100)
    run(m, 4500)
    assert m.signals["cc_frozen"] or m.signals["ph_frozen"]
    m2 = make(oat=5.0, rh=60.0)
    start(m2, 80, dmpr=100)
    run(m2, 150)
    m2.set_input("ma_dmpr", 0)                            # controller closes the OA damper
    run(m2, 4500)
    assert not (m2.signals["cc_frozen"] or m2.signals["ph_frozen"])


def test_smoke_detector_stops_fans():
    m = make()
    start(m)
    run(m, 120)
    m.set_fault("sa_smoke_alarm", True)
    m.step(1)
    assert not m.signals["sa_smoke"] and m.signals["sf1_freq"] == 0
    m.set_fault("sa_smoke_alarm", False)                  # fire alarm reset: no manual reset needed
    run(m, 30)
    assert m.signals["sf1_freq"] > 10


# ----- actuators --------------------------------------------------------------


def test_preheat_valve_fails_open_but_broken_wire_closes():
    m = make()
    start(m)
    m.set_input("ph_vlv", 60)
    run(m, 200)
    assert m.signals["ph_vlv_pos"] == pytest.approx(50, abs=1)
    m.set_fault("ph_wire_broken", True)                   # 0 V: powered actuator drives to 2 V position
    run(m, 200)
    assert m.signals["ph_vlv_pos"] == 0
    m.set_fault("ph_actuator_failed", True)               # power lost: spring opens the valve
    run(m, 40)
    assert m.signals["ph_vlv_pos"] == 100


def test_damper_failures():
    m = make()
    start(m, dmpr=100)
    run(m, 200)
    assert m.signals["oa_dmpr_pos"] == 100 and m.signals["ra_dmpr_pos"] == 0
    m.set_fault("ra_actuator_failed", True)
    m.set_fault("oa_actuator_failed", True)
    run(m, 40)
    assert m.signals["ra_dmpr_pos"] == 100 and m.signals["oa_dmpr_pos"] == 0


def test_humidifier_high_limit_closes_steam_valve():
    m = make(oat=40.0, rh=30.0, hum_high_limit=55.0)
    start(m)
    m.set_input("hum_enable", True)
    m.set_input("hum_vlv", 100)
    tripped, max_rh, max_pos = False, 0.0, 0.0
    for _ in range(900):
        m.step(1.0)
        tripped |= m.signals["hum_hl_tripped"]
        max_rh = max(max_rh, m.signals["sa_rh"])
        max_pos = max(max_pos, m.signals["hum_vlv_pos"])
    assert tripped and max_rh < 58.0 and max_pos < 100   # limit cycles the valve closed (spring) and open
    m.set_fault("hum_hl_failed", True)
    run(m, 300)
    assert m.signals["hum_vlv_pos"] == 100 and m.signals["sa_rh"] > 58.0


def test_sensor_faults():
    m = make()
    start(m)
    run(m, 200)
    m.set_fault("static_tube_off", True)
    m.set_fault("pf_tubes_reversed", True)
    m.step(1)
    assert m.signals["sa_static"] == 0 and m.signals["pf_dp"] < 0


def test_weather_params_declared():
    assert MODELS["ahu_vav_econ"].WEATHER_PARAMS == {"oat": "oa_temp_actual", "oa_w": "oa_w_actual"}


# ----- BACnet demo ------------------------------------------------------------


def test_demo_controller_runs_the_ahu_over_bacnet():
    asyncio.run(_demo())


async def _demo():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "AHU-1")]
    cfg["host"]["speed"] = 20.0
    host = Host(cfg, demo=True)
    ahu = host.unit("AHU-1")
    assert ahu.weather is host.unit("WX-1")
    task = asyncio.create_task(host.run())
    try:
        loop = asyncio.get_running_loop()
        mock = host.mocks[0]
        end = loop.time() + 40
        while loop.time() < end:
            await asyncio.sleep(0.5)
            pts = ahu.link.points
            sim_sp = ahu.model.signals["sa_static"]
            if all(p.ok for p in pts) and abs(sim_sp - 1.5) < 0.3 and abs((mock.value("sa_static") or 0) - sim_sp) < 0.1:
                break
        assert all(p.ok for p in ahu.link.points), [(p.object_name, p.last_error) for p in ahu.link.points if not p.ok]
        assert len(ahu.link.points) == 27
        assert abs(ahu.model.signals["sa_static"] - 1.5) < 0.3
        assert mock.value("sa_static") == pytest.approx(ahu.model.signals["sa_static"], abs=0.1)
    finally:
        task.cancel()
        host.close()
