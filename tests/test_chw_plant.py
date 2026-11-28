"""Chilled water plant (chw_plant): York YK chillers, headered pumps, TES decoupler, towers, power, BACnet."""

import asyncio
import copy

import pytest

from hvacsim.config import ConfigError, load_host_config
from hvacsim.models.chw_plant import ChwPlant
from hvacsim.models.components.hydronic import StratifiedTank, r134a_psig, r134a_tsat
from hvacsim.runner import Host


def plant(**params):
    return ChwPlant({"clock_start": "2026-07-15 10:00", "oat": 92.0, "oa_w": 0.015, "sensor_noise": 0.0, **params},
                    seed=1)


def run(m, seconds, dt=5.0):
    for _ in range(int(seconds / dt)):
        m.step(dt)


def base_valves(m):
    I = m.inputs
    for k in m.tanks:
        I[f"{k['pre']}_top_vlv"] = I[f"{k['pre']}_bot_vlv"] = True
    for t in m.towers:
        I[f"{t['pre']}_outlet_vlv"] = I[f"{t['pre']}_eq_vlv"] = True


def start_train(m, n=1, sec_pumps=2, sec_speed=95.0):
    """Chiller n with 'its' primary pump, condenser pump and tower cell, plus secondary pumps."""
    I = m.inputs
    base_valves(m)
    I[f"pchwp{n}_ss"], I[f"pchwp{n}_vfd_spd"] = True, 91.7
    I[f"cwp{n}_ss"], I[f"cwp{n}_vfd_spd"] = True, 91.7
    I[f"ch{n}_chw_vlv"] = I[f"ch{n}_cw_vlv"] = True
    I[f"ct{n}_inlet_vlv"], I[f"ct{n}_fan_ss"], I[f"ct{n}_fan_vfd_spd"] = True, True, 100.0
    for i in range(1, sec_pumps + 1):
        I[f"schwp{i}_ss"], I[f"schwp{i}_vfd_spd"] = True, sec_speed
    I[f"ch{n}.rem_run"] = True


# ----- building blocks -----------------------------------------------------------


def test_r134a_saturation():
    assert r134a_psig(40.0) == pytest.approx(35.0, abs=1.0)
    assert r134a_psig(100.0) == pytest.approx(124.1, abs=1.5)
    assert r134a_tsat(r134a_psig(77.0)) == pytest.approx(77.0, abs=0.01)


def test_tank_thermocline_moves_with_charge_flow():
    t = StratifiedTank(120000, 40, charge=0.0, t_init_cold=44, t_init_warm=56)
    assert t.charge_fraction() == pytest.approx(0.0, abs=0.01)
    for _ in range(300):                        # 2400 gpm for 50 min = 120,000 gal
        t.step(10.0, 2400.0, 44.0)
    assert t.charge_fraction() > 0.85 and t.top > 47.0     # thermocline just reaching the top
    assert t.sensor(9, 10) == pytest.approx(44.0, abs=0.3)   # bottom sensor cold
    for _ in range(150):
        t.step(10.0, -2400.0, 57.0)
    assert t.sensor(0, 10) > 55.0 and t.sensor(9, 10) < 46.0  # warm top, still cold bottom


# ----- generated points -------------------------------------------------------------


def test_default_layout_points_and_controllers():
    m = plant()
    io = m.io
    assert set(io) == {241001, 241002, 241003, 241004, 241011, 241012}
    assert io[241001]["bo"][:2] == ["ch1_chw_vlv", "ch1_cw_vlv"] and "pchwp3_ss" in io[241001]["bo"]
    assert "pchwp1_vfd_spd" in io[241001]["ao"] and "schwp2_vfd_spd" in io[241002]["ao"]
    assert {"ct1_inlet_vlv", "ct1_outlet_vlv", "ct1_bypass_vlv", "ct1_eq_vlv", "ct1_basin_htr"} <= set(io[241003]["bo"])
    w1 = [n for n, _o in io[241001]["writes"]]
    assert {"pchws_temp", "pchwr_temp", "pchw_flow", "cws_temp", "cwr_temp", "utility_power",
            "ch1_chw_vlv_open", "ch1_chw_vlv_closed", "ch1_chw_flow", "pchwp1_amps", "pchwp1_vfd_fb", "pchwp1_vfd_flt"} <= set(w1)
    w4 = [n for n, _o in io[241004]["writes"]]
    assert "tk1_temp_01" in w4 and "tk3_temp_10" in w4 and "dcplr_flow" in w4 and "tes_cold_temp" in w4
    assert io[241011]["writes"] == [("east_chw_remote_dp", "chw_remote_dp")]
    assert io[241012]["writes"] == [("west_chw_remote_dp", "chw_remote_dp")]


def test_csv_schedule_and_devices():
    cfg = load_host_config("configs/station-1.yaml")
    p = next(i for i in cfg["instances"] if i["name"] == "CHP-1")
    assert [c["tag"] for c in p["plant_layout"]["chillers"]] == ["CH-1", "CH-2", "CH-3"]
    assert p["plant_layout"]["chillers"][0]["capacity_tons"] == 1200
    devs = {d["name"]: (d["device_instance"], d["mac"]) for d in p["devices"]}
    assert devs["CH-2"] == (4010032, 32) and devs["CHP-1-IO-241003"] == (4010043, 43)


def test_custom_plant_csv(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "chw_plant.yaml").write_text(open("configs/templates/chw_plant.yaml").read())
    (tmp_path / "ch.csv").write_text("tag,sim_device,mac,controller_device,capacity_tons,chw_design_gpm,cw_design_gpm\n"
                                     "CH-1,4010031,31,241001,800,1600,2400\n# spare row\n"
                                     "CH-2,4010032,32,241001,800,1600,2400\n"
                                     "CH-3,4010033,33,241001,,,\nCH-4,4010034,34,241001,500,1000,1500\n")
    (tmp_path / "pp.csv").write_text("tag,controller_device,design_gpm\nP-1,241001,1600\nP-2,241001,1600\n")
    (tmp_path / "h.yaml").write_text("""
host: {name: H, device_instance: 4010000, address: 127.0.0.1/24, ip_network: 1, virtual_network: 5001}
instances:
  - name: CHP-2
    template: templates/chw_plant.yaml
    plant:
      chillers: ch.csv
      primary_pumps: pp.csv
      io_devices: {241001: {device_instance: 4010041, mac: 41}, 241002: {device_instance: 4010042, mac: 42},
                   241003: {device_instance: 4010043, mac: 43}, 241004: {device_instance: 4010044, mac: 44}}
""")
    cfg = load_host_config(str(tmp_path / "h.yaml"))
    inst = cfg["instances"][0]
    m = ChwPlant(inst["equipment"]["params"], layout=inst["plant_layout"])
    assert [c["tag"] for c in m.chillers] == ["CH-1", "CH-2", "CH-3", "CH-4"]
    assert m.chillers[2]["capacity_tons"] == 1200.0               # blank cells keep the default
    assert m.chillers[3]["unit"].capacity == 500.0
    assert [x["tag"] for x in m.pumps["primary_pumps"]] == ["P-1", "P-2"]
    assert "p2_ss" in m.io[241001]["bo"] and "ch4_chw_vlv" in m.io[241001]["bo"]
    assert len(m.pumps["secondary_pumps"]) == 3                    # not listed: default 3
    bad = copy.deepcopy(inst)
    bad["plant"]["io_devices"][241002]["mac"] = 41
    cfg2 = copy.deepcopy(cfg)
    cfg2["instances"] = [bad]
    bad["devices"][-3]["mac"] = 41
    from hvacsim.config import validate
    with pytest.raises(ConfigError):
        validate(cfg2)


# ----- chiller and hydraulics --------------------------------------------------------


def test_chiller_starts_with_flow_and_holds_setpoint():
    m = plant()
    start_train(m)
    run(m, 30)
    s = m.signals
    assert s["ch1.op_code"] == 7                                   # 50 s oil pre-lube
    run(m, 900)
    assert s["ch1.motor_run"] and s["ch1.op_code"] == 8
    assert s["ch1.lchwt"] == pytest.approx(44.0, abs=0.6)
    assert s["ch1_chw_flow"] == pytest.approx(2400, rel=0.06)     # balanced at 55 Hz
    assert s["ch1_cw_flow"] == pytest.approx(3600, rel=0.06)
    assert 0.3 < s["ch1.kw"] / max(s["ch1.tons"], 1) < 0.7
    assert s["ch1.evap_fs"] and s["ch1.cond_fs"] and s["ch1.starts"] == 1


def test_no_flow_no_start_and_anti_recycle():
    m = plant()
    base_valves(m)
    m.inputs["ch1.rem_run"] = True
    run(m, 120)
    s = m.signals
    assert not s["ch1.motor_run"] and s["ch1.cycling_code"] == 7 and s["ch1.op_code"] == 4
    start_train(m)
    run(m, 300)
    assert s["ch1.motor_run"]
    m.inputs["ch1.rem_run"] = False                                # soft stop, coastdown
    run(m, 120)
    assert not s["ch1.motor_run"] and s["ch1.op_code"] in (11, 2)
    m.inputs["ch1.rem_run"] = True
    run(m, 300)
    assert not s["ch1.motor_run"] and s["ch1.inhibit_code"] == 1   # 30 min start-to-start
    assert s["ch1.anti_recycle"] > 15
    run(m, 1500)
    assert s["ch1.motor_run"] and s["ch1.starts"] == 2


def test_panel_local_ignores_bas():
    m = plant()
    start_train(m)
    m.action("ch1.local")
    m.inputs["ch1.rem_sp"] = 50.0
    run(m, 600)
    s = m.signals
    assert s["ch1.control_source"] == 0 and s["ch1.active_sp"] == 44.0
    m.action("ch1.stop")                                           # panel switch to STOP/RESET
    run(m, 60)
    assert not s["ch1.motor_run"] and not s["ch1.panel_switch_run"]
    m.action("ch1.run")
    m.action("ch1.remote")
    run(m, 10)
    assert s["ch1.control_source"] == 1 and s["ch1.active_sp"] == 50.0


def test_safety_latches_until_panel_reset():
    m = plant()
    start_train(m)
    run(m, 400)
    m.set_fault("ch1_vsd_fault", True)
    run(m, 10)
    s = m.signals
    assert s["ch1.safety_code"] == 77 and not s["ch1.motor_run"]
    m.set_fault("ch1_vsd_fault", False)
    m.inputs["ch1.rem_run"] = False
    run(m, 20)
    m.inputs["ch1.rem_run"] = True
    run(m, 300)
    assert s["ch1.safety_code"] == 77 and s["ch1.op_code"] == 5   # BAS stop/run does not clear it
    m.action("ch1.reset")
    run(m, 10)
    assert s["ch1.safety_code"] == 0


def test_overflow_then_second_chiller():
    m = plant()
    start_train(m)
    run(m, 400)
    one = m.signals["ch1_chw_flow"]
    m.inputs["pchwp2_ss"], m.inputs["pchwp2_vfd_spd"] = True, 91.7
    run(m, 60)
    over = m.signals["ch1_chw_flow"]
    assert over > 1.1 * one and m.signals["ch1.evap_dp"] > 1.3 * 20.0 / 2.307   # overflowed, higher evaporator dP
    m.inputs["ch2_chw_vlv"] = True
    run(m, 90)
    assert m.signals["ch1_chw_flow"] == pytest.approx(m.signals["ch2_chw_flow"], rel=0.05)
    assert m.signals["ch2_chw_vlv_open"] and not m.signals["ch2_chw_vlv_closed"]


def test_decoupler_charges_and_discharges():
    m = plant(clock_start="2026-07-15 23:00", oat=65.0, oa_w=0.008)       # night: low load
    start_train(m, sec_pumps=1, sec_speed=45.0)
    run(m, 900)
    s = m.signals
    assert s["dcplr_flow"] > 500 and s["tes_flow"] == pytest.approx(s["primary_flow"] - s["secondary_flow"], abs=5)
    assert s["pchw_flow"] == pytest.approx(s["schw_flow"] + s["dcplr_flow"], abs=10)
    charge = s["tes_charge"]
    m.set_param("clock_start", "")                                  # (no effect after start)
    m.inputs["pchwp1_vfd_spd"] = 0.0
    m.inputs["pchwp1_ss"] = False
    for i in (1, 2):
        m.inputs[f"schwp{i}_ss"], m.inputs[f"schwp{i}_vfd_spd"] = True, 90.0
    run(m, 900)
    assert s["dcplr_flow"] < -500 and s["tes_charge"] < charge - 3
    m.set_fault("dcplr_flow_reversed", True)
    m.step(1.0)
    assert s["dcplr_flow"] > 500


def test_utility_outage_ups_and_restart():
    m = plant()
    start_train(m)
    run(m, 600)
    s = m.signals
    assert s["ch1.motor_run"]
    m.set_utility(False)
    run(m, 60)
    assert not s["utility_power"] and not s["ch1.motor_run"]
    assert s["pchwp1_amps"] == 0 and not s["pchwp1_vfd_flt"]           # RO3 dropped out (no control power)
    assert s["schwp1_amps"] > 10 and s["secondary_flow"] > 2000      # secondary pumps on the UPS
    assert s["primary_flow"] < 50 and s["dcplr_flow"] < -2000          # building runs on the tanks
    assert s["schws_actual"] < 46.0
    m.set_utility(True)
    run(m, 30)
    assert s["ch1.cycling_code"] == 5 and s["ch1.safety_code"] == 0    # power failure does not latch
    assert s["pchwp1_amps"] > 10                                       # level start: drive restarts on its own
    run(m, 1500)
    assert s["ch1.motor_run"] and s["ch1.cycling_code"] == 0


def test_condenser_flow_loss_cycles_chiller():
    m = plant()
    start_train(m)
    run(m, 600)
    m.inputs["cwp1_vfd_spd"] = 35.0
    run(m, 120)
    s = m.signals
    assert not s["ch1.motor_run"] and s["ch1.cycling_code"] == 8
    m.inputs["cwp1_vfd_spd"] = 91.7
    run(m, 600)
    assert s["ch1.cycling_code"] == 0


def test_low_load_cycles_on_low_leaving_temp():
    # chiller on a small closed loop with almost no load: it cannot unload below its minimum,
    # the loop cools, and it cycles off at setpoint - 4 °F; it restarts at setpoint
    from hvacsim.models.components.york_yk import YorkYK
    yk = YorkYK("CH-1", {}, set())
    yk.rem_run, yk.rem_sp = True, 44.0
    ewt = 50.0
    seen = []
    for _ in range(4000):
        yk.step(1.0, 2400.0, 3600.0, ewt, 70.0)
        ewt += (yk.lchwt + 0.3 - ewt) * 0.01           # loop water returns slightly warmer than it left
        if not seen or seen[-1] != yk.cycling:
            seen.append(yk.cycling)
        if 6 in seen:
            break
    assert 6 in seen and not yk.running and yk.op_code() == 11     # coastdown after the cycling stop
    assert yk.lchwt < 40.5


# ----- towers ---------------------------------------------------------------------


def test_tower_basin_drains_with_equalizer_closed_and_heater_lockout():
    m = plant(oat=30.0, oa_w=0.002)
    start_train(m)
    m.inputs["ct2_outlet_vlv"] = True
    m.inputs["ct2_eq_vlv"] = False
    m.inputs["ct2_basin_htr"] = True
    m.inputs["ch1.rem_run"] = False
    run(m, 900)
    s = m.signals
    assert s["ct2.level"] < 8.0 and not s["ct2_level_lo"]       # switch open = low level alarm
    assert not s["ct2.heater_on"]                                # low level locks the heater out
    m.inputs["ct2_eq_vlv"] = True
    run(m, 900)
    assert s["ct2_level_lo"]


def test_tower_vibration_switch_coasts_fan_until_reset():
    m = plant()
    start_train(m)
    run(m, 300)
    m.set_fault("ct1_vibration", True)
    run(m, 30)
    s = m.signals
    assert s["ct1.vib_tripped"] and s["ct1.freq"] == 0
    with pytest.raises(ValueError):
        m.action("ct1.vib_reset")
    m.set_fault("ct1_vibration", False)
    m.action("ct1.vib_reset")
    run(m, 60)
    assert not s["ct1.vib_tripped"] and s["ct1.freq"] > 40


def test_bypass_valve_spring_closes_on_power_loss():
    m = plant()
    base_valves(m)
    m.inputs["ct1_bypass_vlv"] = True
    run(m, 90)
    assert m.signals["ct1_bypass_vlv_open"]
    m.set_utility(False)
    run(m, 40)
    assert m.signals["ct1_bypass_vlv_closed"] and m.signals["ct1_outlet_vlv_open"]   # bypass fails closed, outlet in place


# ----- building / remote DP ----------------------------------------------------------


def test_remote_dp_below_plant_dp_and_tube_leak():
    m = plant()
    start_train(m)
    run(m, 600)
    s = m.signals
    assert s["schw_dp"] > s["east_chw_remote_dp"] > 0
    rdp = s["west_chw_remote_dp"]
    m.set_fault("west_dp_tube_leak", True)
    m.step(1.0)
    assert s["west_chw_remote_dp"] == pytest.approx(rdp * 0.5, rel=0.1)


def test_low_dt_fault_lowers_delta_t():
    def dt(fault):
        m = plant()
        start_train(m)
        if fault:
            m.set_fault("low_dt", True)
        run(m, 1200)
        return m.signals["schwr_temp"] - m.signals["schws_temp"]
    assert dt(True) < dt(False) - 1.0


def test_ahu_fed_from_plant_and_aggregate_shrinks():
    asyncio.run(_ahu_fed())


async def _ahu_fed():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "AHU-1", "CHP-1")]
    host = Host(cfg, demo=True, demo_controllers=False)
    try:
        p = host.unit("CHP-1").model
        east = p.branches[0]
        assert p.ahus and p.ahus[0]["branch"] is east
        assert p.ahu_tons(east) == pytest.approx(90.0)                 # 180 gpm coil at 12 °F
        host.step(10.0)
        assert host.unit("AHU-1").model.p["chw_supply_temp"] == pytest.approx(p.signals["schws_actual"], abs=0.05)
        assert [u.name for u in host.units][-1] == "CHP-1"               # plant steps after the AHU
    finally:
        host.close()


# ----- BACnet -------------------------------------------------------------------------


def test_plant_over_bacnet_demo():
    asyncio.run(_demo())


async def _demo():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "CHP-1")]
    cfg["host"]["speed"] = 20.0
    host = Host(cfg, demo=True)
    plant_u = host.unit("CHP-1")
    assert len(plant_u.facets) == 7
    ch1 = plant_u.facets[0]
    names = {str(o.objectName): o for o in ch1.app.objectIdentifier.values()}
    assert "REM RUN STOP CMD" in names and "UNIT OPERATION CODE" in names
    op = names["UNIT OPERATION CODE"]
    assert str(op.objectIdentifier) in ("multi-state-value,1", "('multi-state-value', 1)") or op.objectIdentifier[1] == 1
    assert op.stateText[0] == "Unit Stopped - Ready To Start" and len(op.stateText) == 160
    task = asyncio.create_task(host.run())
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + 60
        while loop.time() < end:
            await asyncio.sleep(0.5)
            links_ok = all(p.ok for l in plant_u.links.values() for p in l.points)
            if links_ok and plant_u.model.signals["ch1.motor_run"]:
                break
        bad = [(c, p.object_name, p.last_error) for c, l in plant_u.links.items() for p in l.points if not p.ok]
        assert not bad, bad[:5]
        s = plant_u.model.signals
        assert s["ch1.motor_run"], s["ch1.status"]                       # demo logic started CH-1 over BACnet
        assert int(op.presentValue) == s["ch1.op_code"] + 1                # MSV value = code + 1
        mocks = {m.device_instance: m for m in host.mocks}
        assert {241001, 241002, 241003, 241004, 241011, 241012} <= set(mocks)
        assert mocks[241011].value("chw_remote_dp") == pytest.approx(s["east_chw_remote_dp"], abs=0.5)
        # utility outage: chiller gateways stop answering
        host.power(False, minutes=1)
        await asyncio.sleep(1.0)
        assert ch1.app.offline and not s["utility_power"]
        await asyncio.sleep(4.0)                                           # 20x: one simulated minute
        assert host.utility_ok and not ch1.app.offline
    finally:
        task.cancel()
        host.close()
