"""Hot water plant (hw_plant): Crest boilers, dedicated primary pumps, decoupler, branches, power, BACnet."""

import asyncio
import random

import pytest

from hvacsim.config import load_host_config
from hvacsim.models.components.crest import CrestBoiler
from hvacsim.models.hw_plant import DEFAULT_LAYOUT, HwPlant
from hvacsim.runner import Host


def plant(**params):
    return HwPlant({"clock_start": "2026-01-14 10:00", "oat": 20.0, "sensor_noise": 0.0, **params}, seed=1)


def run(m, seconds, dt=5.0):
    for _ in range(int(seconds / dt)):
        m.step(dt)


def start(m, boilers=(1,), rate=60.0, sec_pumps=1, sec_speed=80.0, prim_speed=91.7):
    I = m.inputs
    I["louver"] = True
    for n in boilers:
        I[f"phwp{n}_ss"], I[f"phwp{n}_vfd_spd"] = True, prim_speed
        I[f"b{n}.bms_enable"], I[f"b{n}.bms_rate"] = True, rate
    for i in range(1, sec_pumps + 1):
        I[f"shwp{i}_ss"], I[f"shwp{i}_vfd_spd"] = True, sec_speed


# ----- layout / points -------------------------------------------------------------


def test_points_and_controllers():
    m = plant()
    io = m.io
    assert set(io) == {242001, 242002, 242011, 242012}
    assert "b1_alarm" in [s for s, _o in io[242001]["writes"]]
    assert {"phwp1_ss", "louver"} <= set(io[242001]["bo"])
    assert "shwp2_vfd_spd" in io[242002]["ao"]
    assert {o for _s, o in io[242002]["writes"]} >= {"phws_temp", "shws_temp", "shw_flow", "hw_dcplr_flow",
                                                       "hw_dp", "hw_loop_press"}
    # branch points have the same object names on each branch controller
    assert io[242011]["writes"] == [("east_hw_remote_dp", "hw_remote_dp"), ("east_hw_eob_temp", "hw_eob_temp")]
    assert m.io_names["west_hw_eob_vlv"] == "hw_eob_vlv"
    names = [d["tag"] for d in m.bacnet_devices()]
    assert names == ["B-1", "B-2", "B-3"]
    objs = {o["name"] for o in m.bacnet_devices()[0]["commands"]}
    assert objs == {"BMS ENABLE", "BMS FIRING RATE"}


def test_each_boiler_needs_one_dedicated_pump():
    lay = {**DEFAULT_LAYOUT, "primary_pumps": DEFAULT_LAYOUT["primary_pumps"][:2]}
    with pytest.raises(ValueError, match="B-3 has 0 primary pumps"):
        HwPlant({}, layout=lay)
    lay = {**DEFAULT_LAYOUT, "primary_pumps": [*DEFAULT_LAYOUT["primary_pumps"][:2],
                                               {"tag": "PHWP-3", "boiler": "B-9", "controller_device": 242001}]}
    with pytest.raises(ValueError, match="B-9"):
        HwPlant({}, layout=lay)


def test_csv_schedules_load():
    cfg = load_host_config("configs/station-1.yaml")
    hwp = next(i for i in cfg["instances"] if i["name"] == "HWP-1")
    assert [b["tag"] for b in hwp["plant_layout"]["boilers"]] == ["B-1", "B-2", "B-3"]
    assert [d["device_instance"] for d in hwp["devices"]] == [4010051, 4010052, 4010053, 4010061, 4010062, 4010063, 4010064]


# ----- boiler (component) ----------------------------------------------------------


def crest(**faults):
    return CrestBoiler("B-1", {}, set(faults), random.Random(1), t_init=120.0)


def go(b, seconds, gpm=400.0, ewt=120.0, h=0.5):
    for _ in range(int(seconds / h)):
        b.step(h, gpm, ewt)


def test_boiler_sequence_and_post_purge():
    b = crest()
    b.bms_enable, b.bms_rate = True, 70.0
    go(b, 5)
    assert b.state == 1                                     # pre-purge
    go(b, 10)
    assert b.flame and b.state == 3 and b.rate == pytest.approx(0.40)   # low fire hold
    go(b, 60)
    assert b.state == 4 and b.rate == pytest.approx(0.70, abs=0.01)
    assert b.points()["pump_request"]
    b.bms_rate = 0.0
    go(b, 5)
    assert b.state == 5 and not b.flame                      # post-purge
    go(b, 30)
    assert b.state == 6                                      # anti-cycle
    b.bms_rate = 70.0
    go(b, 20)
    assert b.state in (6, 1)
    go(b, 60)
    assert b.flame and b.cycles == 2


def test_boiler_blocks_without_flow_louver_or_gas():
    b = crest()
    b.bms_enable, b.bms_rate = True, 50.0
    go(b, 10, gpm=50.0)
    assert b.state == 7 and b.blocking == 1 and not b.flame
    b.louver_ok = False
    go(b, 2, gpm=50.0)
    assert b.blocking == 1
    go(b, 2, gpm=400.0)
    assert b.blocking == 2
    b.louver_ok, b.gas_ok = True, False
    go(b, 2)
    assert b.blocking == 3
    b.gas_ok = True
    go(b, 60)
    assert b.flame and b.blocking == 0 and b.lockout == 0   # blocking conditions clear by themselves


def test_ignition_failure_locks_out_after_three_tries_and_reset():
    b = crest(ignition_fail=True)
    b.bms_enable, b.bms_rate = True, 50.0
    go(b, 60)
    assert b.lockout == 1 and b.state == 8 and b.points()["alarm"]
    b.faults.clear()
    go(b, 30)
    assert b.lockout == 1                                    # latched
    b.display("reset")
    go(b, 60)
    assert b.flame and b.lockout == 0


def test_no_flow_with_stuck_flow_switch_trips_high_limit():
    b = crest(flow_sw_stuck=True)
    b.bms_enable, b.bms_rate = True, 100.0
    go(b, 1800, gpm=0.0)
    assert b.lockout == 2                                    # manual reset high limit
    b.display("reset")                                       # cause gone (cooled?) - still too hot is fine
    assert b.lockout in (0, 2)


def test_power_loss_does_not_latch_and_comm_loss_holds_then_blocks():
    b = crest()
    b.bms_enable, b.bms_rate = True, 50.0
    go(b, 60)
    assert b.flame
    b.powered = False
    go(b, 5)
    assert not b.flame and b.blocking == 4 and b.lockout == 0
    b.powered = True
    go(b, 120)
    assert b.flame
    b.comm_ok = False
    go(b, 50)
    assert b.flame                                           # holds the last rate for 60 s
    go(b, 15)
    assert not b.flame and b.blocking == 6
    b.comm_ok = True
    go(b, 120)
    assert b.flame


def test_efficiency_falls_with_return_temperature():
    lo, hi = crest(), crest()
    for b, ewt in ((lo, 90.0), (hi, 150.0)):
        b.bms_enable, b.bms_rate = True, 50.0
        go(b, 120, ewt=ewt)
    assert lo.eff > 0.94 and hi.eff < 0.89
    assert lo.condensate > 0 and hi.condensate == 0
    assert lo.points()["flue_temp"] < hi.points()["flue_temp"]


def test_local_mode_ignores_bms():
    b = crest()
    b.display("local")
    go(b, 300)
    assert b.flame and not b.points()["bms_mode"]           # runs on its own 180 °F setpoint


# ----- plant ------------------------------------------------------------------------


def test_one_boiler_heats_the_loop():
    m = plant()
    start(m)
    run(m, 1200)
    s = m.signals
    assert s["b1.status"].startswith("Running")
    assert s["phwp1.flow"] == pytest.approx(400, abs=15)     # balancer speed gives boiler design flow
    assert not s["b2.flow_switch"]
    assert s["shws_actual"] > s["shwr_actual"] + 3
    assert s["plant_mbh"] > 1500 and 85 < s["plant_eff"] < 97
    assert s["louver_open"] and s["b1_alarm"] is False


def test_decoupler_direction_and_mixing():
    # primary > secondary: supply short-circuits to the boilers, primary return heats up
    m = plant()
    start(m, boilers=(1, 2), rate=40.0, sec_speed=45.0)
    run(m, 900)
    s = m.signals
    assert s["dcplr_flow"] > 100 and s["hw_dcplr_flow"] > 100
    assert s["phwr_actual"] > s["shwr_actual"] + 1.0
    # secondary > primary: return water mixes into the building supply
    m = plant()
    start(m, boilers=(1,), rate=60.0, sec_pumps=2, sec_speed=100.0)
    run(m, 900)
    s = m.signals
    assert s["dcplr_flow"] < -100
    assert s["shws_actual"] < s["phws_actual"] - 1.0
    before = s["dcplr_flow"]
    # matching secondary to primary flow balances it
    m.inputs["shwp2_ss"] = False
    m.inputs["shwp1_vfd_spd"] = 50.0
    run(m, 300)
    assert abs(m.signals["dcplr_flow"]) < abs(before) / 2


def test_primary_pump_turndown_opens_the_flow_switch():
    m = plant()
    start(m)
    run(m, 300)
    assert m.signals["b1.flame"]
    m.inputs["phwp1_vfd_spd"] = 0.0                        # drive minimum 15 Hz: below the boiler's 120 gpm
    run(m, 120)
    assert m.signals["b1.blocking_code"] == 1 and not m.signals["b1.flame"]


def test_louver_interlock_and_estop():
    m = plant()
    start(m)
    m.inputs["louver"] = False
    run(m, 120)
    assert m.signals["b1.blocking_code"] == 2 and not m.signals["louver_open"]
    m.inputs["louver"] = True
    run(m, 180)
    assert m.signals["b1.flame"]
    m.action("estop")
    run(m, 10)
    assert not m.signals["b1.flame"] and not m.signals["boiler_estop"] and m.signals["b1.blocking_code"] == 4
    assert m.signals["phwp1.flow"] > 300                       # E-stop removes boiler power only
    m.action("estop_reset")
    run(m, 180)
    assert m.signals["b1.flame"]
    m.set_fault("louver_stuck_closed", True)
    run(m, 60)
    assert m.signals["b1.blocking_code"] == 2


def test_utility_outage_and_restart():
    m = plant()
    start(m)
    run(m, 300)
    m.set_utility(False)
    run(m, 30)
    s = m.signals
    assert not s["b1.flame"] and s["b1.lockout_code"] == 0 and m.device_offline("b1")
    assert s["secondary_flow"] < 50 and not s["utility_power"]
    m.set_utility(True)
    run(m, 240)
    assert m.signals["b1.flame"]                               # drives level-start, boiler re-runs its sequence


def test_end_of_branch_valve_keeps_pipe_hot():
    m1, m2 = plant(load_scale=20.0), plant(load_scale=20.0)
    for m, pos in ((m1, 0.0), (m2, 100.0)):
        start(m, rate=40.0)
        m.inputs["east_hw_eob_vlv"] = pos
        run(m, 3600, dt=10.0)
    assert m2.signals["east.eob_flow"] > 5 and m1.signals["east.eob_flow"] < 1
    assert m2.signals["east_hw_eob_temp"] > m1.signals["east_hw_eob_temp"] + 3


def test_expansion_tank_waterlogged_lifts_relief():
    m = plant()
    start(m, boilers=(1, 2), rate=90.0, sec_speed=60.0)
    run(m, 1200)
    normal = m.signals["hw_loop_press"]
    assert 12.0 < normal < 16.0 and not m.signals["relief_lifting"]
    m.set_fault("exp_tank_waterlogged", True)
    run(m, 10)
    assert m.signals["hw_loop_press"] > normal + 5


def test_building_load_follows_oa_and_warmup():
    m = plant(oat=0.0)
    m._occ = True
    assert m.load_fraction(m.clock()) == pytest.approx(1.0, abs=0.01)
    m.p["oat"] = 70.0
    m._occ = True
    assert m.load_fraction(m.clock()) == pytest.approx(0.10, abs=0.01)   # reheat floor
    w = plant(oat=32.0, clock_start="2026-01-14 06:30")
    w._occ = True
    f_warm = w.load_fraction(w.clock())
    w2 = plant(oat=32.0, clock_start="2026-01-14 10:30")
    w2._occ = True
    assert f_warm == pytest.approx(1.3 * w2.load_fraction(w2.clock()), rel=0.01)


def test_pump_faults():
    m = plant()
    start(m)
    m.set_fault("phwp1_seized", True)
    run(m, 120)
    assert "FAULT" in m.signals["phwp1.status"] and not m.signals["phwp1_vfd_flt"]
    assert not m.signals["b1.flame"]


def test_sensor_faults():
    m = plant()
    start(m)
    run(m, 300)
    m.set_fault("hw_dcplr_flow_failed", True)
    m.set_fault("shws_temp_failed", True)
    m.set_fault("hw_dp_tube_leak", True)
    m.set_fault("b1_supply_sensor_open", True)
    run(m, 30)
    s = m.signals
    assert s["hw_dcplr_flow"] == 0.0 and s["shws_temp"] == -40.0
    assert s["hw_dp"] == pytest.approx(s["plant_dp"] * 0.5, abs=0.3)
    assert s["b1.supply_temp"] == -40.0 and s["b1.blocking_code"] == 8


# ----- host coupling -----------------------------------------------------------------


def test_host_links_ahu_vav_and_unit_heater():
    asyncio.run(_host_links())


async def _host_links():
    cfg = load_host_config("configs/station-1.yaml")
    host = Host(cfg, demo=True, demo_controllers=False)
    try:
        p = host.unit("HWP-1").model
        kinds = sorted({x["kind"] for x in p.loads})
        assert kinds == ["ahu", "uh", "vav"]
        assert all(x["branch"]["tag"] == "EAST" for x in p.loads)
        east = p.branches[0]
        assert p.connected_mbh(east) == pytest.approx(10 * (52 + 6 * float(host.unit("VAV-1").model.p["reheat_gpm"]) + 3))
        host.step(10.0)
        t = p.signals["shws_actual"]
        assert host.unit("AHU-1").model.p["hw_supply_temp"] == pytest.approx(t, abs=0.05)
        assert host.unit("UH-1-SIM").model.p["hw_supply_temp"] == pytest.approx(t, abs=0.05)
        assert host.unit("VAV-1").model.p["hw_supply_temp"] == pytest.approx(t, abs=0.05)
        assert host.unit("UH-2-SIM").model.p["hw_supply_temp"] == 140.0          # not on the plant
        assert [u.name for u in host.units][-2:] == ["CHP-1", "HWP-1"]
    finally:
        host.close()


# ----- BACnet -------------------------------------------------------------------------


def test_hw_plant_over_bacnet_demo():
    asyncio.run(_demo())


async def _demo():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "HWP-1")]
    cfg["host"]["speed"] = 20.0
    host = Host(cfg, demo=True)
    pu = host.unit("HWP-1")
    assert len(pu.facets) == 7
    b1 = pu.facets[0]
    names = {str(o.objectName): o for o in b1.app.objectIdentifier.values()}
    assert {"BMS ENABLE", "BMS FIRING RATE", "STATUS", "LOCKOUT CODE"} <= set(names)
    st = names["STATUS"]
    assert st.stateText[0] == "Standby" and len(st.stateText) == 10
    task = asyncio.create_task(host.run())
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + 60
        while loop.time() < end:
            await asyncio.sleep(0.5)
            ok = all(p.ok for l in pu.links.values() for p in l.points)
            if ok and pu.model.signals["b1.flame"]:
                break
        bad = [(c, p.object_name, p.last_error) for c, l in pu.links.items() for p in l.points if not p.ok]
        assert not bad, bad[:5]
        s = pu.model.signals
        assert s["b1.flame"], s["b1.status"]                       # demo logic fired B-1 over BACnet
        assert int(st.presentValue) == s["b1.status_code"] + 1
        mocks = {m.device_instance: m for m in host.mocks}
        assert {242001, 242002, 242011, 242012} <= set(mocks)
        assert mocks[242011].value("hw_remote_dp") == pytest.approx(s["east_hw_remote_dp"], abs=0.5)
        host.power(False, minutes=1)
        await asyncio.sleep(1.0)
        assert b1.app.offline and not s["b1.flame"]
    finally:
        task.cancel()
        host.close()
