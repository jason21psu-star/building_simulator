"""Generator plant (gen_plant): PowerCommand gensets, paralleling master, ATSs, Modbus TCP."""

import asyncio
import struct

import pytest

from hvacsim.config import load_host_config
from hvacsim.modbus_tcp import ModbusError, ModbusTcpClient, ModbusTcpServer
from hvacsim.models.gen_plant import DEFAULT_LAYOUT, GenPlant
from hvacsim.runner import Host


def plant(retransfer_min=1.0, **params):
    lay = {"ats": [{**a, "retransfer_delay_min": retransfer_min} for a in DEFAULT_LAYOUT["ats"]]}
    return GenPlant({"clock_start": "2026-01-14 10:00", **params}, layout=lay, seed=1)


def run(m, seconds, dt=0.5):
    for _ in range(int(seconds / dt)):
        m.step(dt)


def ats(m, tag):
    return m.ats_by_tag(tag)


def read(server, uid, reg, count=1):
    resp = server.handle(uid, struct.pack(">BHH", 3, reg - 40001, count))
    if resp[0] & 0x80:
        raise ModbusError(resp[1])
    return list(struct.unpack(f">{count}H", resp[2:]))


def write(server, uid, reg, value):
    resp = server.handle(uid, struct.pack(">BHH", 6, reg - 40001, value))
    if resp[0] & 0x80:
        raise ModbusError(resp[1])


# ----- generator set ----------------------------------------------------------------


def test_start_sequence_and_registers():
    m = plant()
    srv = ModbusTcpServer(m.modbus_units())
    assert read(srv, 1, 40010) == [1]                          # Auto
    write(srv, 1, 40300, 1)                                     # remote start over Modbus
    run(m, 3)
    assert read(srv, 1, 40011) == [1]                          # time delay to start / cranking
    run(m, 10)
    gs = m.gens[0]["gen"]
    assert gs.state == "rated" and gs.cb_closed                # first set closes to the dead bus
    r = read(srv, 1, 40010, 40)                                 # 40010-40049 in one request
    assert r[40044 - 40010] == 6000                            # Hz x 0.01
    assert r[40025 - 40010] == 480                             # L-L average volts
    nfpa = read(srv, 1, 40016)[0]
    assert nfpa & (1 << 13) and not nfpa & (1 << 12)          # running, in auto
    hi, lo = read(srv, 1, 40070, 2)
    assert (hi * 65536 + lo) * 0.1 == pytest.approx(gs.run_s, abs=1.0)
    assert read(srv, 1, 40069) == [1]
    # stop: cooldown 5 min, then stop
    write(srv, 1, 40300, 0)
    run(m, 5)
    assert read(srv, 1, 40011) == [4] and not gs.cb_closed
    run(m, 300)
    assert read(srv, 1, 40011) == [0]


def test_modbus_exceptions():
    m = plant()
    srv = ModbusTcpServer(m.modbus_units())
    with pytest.raises(ModbusError) as e:
        read(srv, 1, 40002)                                     # not implemented
    assert e.value.code == 0x02
    with pytest.raises(ModbusError) as e:
        read(srv, 1, 40010, 41)                                 # > 40 registers
    assert e.value.code == 0x03
    with pytest.raises(ModbusError) as e:
        write(srv, 1, 40044, 1)                                 # read only
    assert e.value.code == 0x02
    with pytest.raises(ModbusError) as e:
        read(srv, 9, 40010)                                     # no such unit behind the gateway
    assert e.value.code == 0x0B
    m.set_fault("gen2_comm_loss", True)
    with pytest.raises(ModbusError) as e:
        read(srv, 2, 40010)
    assert e.value.code == 0x0B
    assert srv.errors == 5 and srv.log[0]["result"].startswith("no response")


def test_fail_to_crank_and_fail_to_start_latch():
    m = plant()
    m.set_fault("gen1_fail_to_crank", True)
    m.set_fault("gen2_fail_to_start", True)
    g1, g2 = m.gens[0]["gen"], m.gens[1]["gen"]
    g1.remote_start = g2.remote_start = 1
    run(m, 20)
    assert g1.shutdown and g1.fault_code == 1438
    run(m, 80)
    assert g2.shutdown and g2.fault_code == 359 and g2.starts == 3
    srv = ModbusTcpServer(m.modbus_units())
    assert read(srv, 2, 40012) == [359] and read(srv, 2, 40013) == [4]
    assert read(srv, 2, 40403)[0] & (1 << 10)                  # fault bitmap (section 14)
    assert read(srv, 2, 40016)[0] & (1 << 8)                    # NFPA 110: fail to start
    m.set_fault("gen2_fail_to_start", False)
    g2.set_switch(2)
    assert not g2.fault_reset()                                 # not in Manual
    g2.set_switch(1)
    write(srv, 2, 40301, 1)                                     # fault reset over Modbus
    run(m, 15)
    assert not g2.shutdown and g2.state == "rated"


def test_weak_battery_and_warnings():
    m = plant()
    m.set_fault("gen1_weak_battery", True)
    m.set_fault("gen1_charger_fail", True)
    g = m.gens[0]["gen"]
    g.remote_start = 1
    run(m, 4)
    assert g.batt_v < 18.0                                      # deep dip while cranking
    run(m, 45)
    assert g.state == "rated" and g.attempt == 2               # slow crank, second attempt
    assert {1442, 2678} <= set(g.active) and not g.shutdown
    srv = ModbusTcpServer(m.modbus_units())
    assert read(srv, 1, 40016)[0] & (1 << 9)                    # charger AC failure
    assert read(srv, 1, 40013) == [1]                          # warning


def test_high_coolant_shutdown():
    m = plant()
    g = m.gens[0]["gen"]
    g.remote_start = 1
    run(m, 30)
    m.set_fault("gen1_high_coolant", True)
    run(m, 900, dt=1.0)
    assert g.fault_code == 151 and 146 in g.active


# ----- paralleling, ATSs, load ----------------------------------------------------------


def test_outage_transfer_staggered_and_inhibit():
    m = plant()
    run(m, 5)
    m.set_utility(False)
    m.inputs["ats3_xfer_inhibit"] = True
    run(m, 9)
    assert all(a["pos"] == "N" for a in m.ats)                  # dark until the bus is ready
    run(m, 8)
    assert ats(m, "ATS-1")["pos"] == "E" and ats(m, "ATS-4")["pos"] == "N"   # priority stagger
    run(m, 30)
    assert [a["pos"] for a in m.ats] == ["E", "E", "N", "E"]   # ATS-3 held by the ALC
    assert m.signals["ats3_norm_pos"] and not m.signals["ats3_emer_pos"] and not m.signals["ats3.live"]
    assert m.signals["sets_online"] == 4 and m.signals["gen_running"] and not m.signals["utility_avail"]
    m.inputs["ats3_xfer_inhibit"] = False
    run(m, 2)
    assert ats(m, "ATS-3")["pos"] == "E"
    srv = ModbusTcpServer(m.modbus_units())
    assert read(srv, 5, 40259) == [2]                          # ATS-1 state: source 2 connected
    assert read(srv, 5, 40034)[0] == pytest.approx(600, abs=3)  # bus Hz x 0.1
    assert read(srv, 5, 40901) == [4]


def test_closed_transition_retransfer_and_cooldown():
    m = plant(retransfer_min=1.0)
    m.set_utility(False)
    run(m, 60)
    seen = set()
    m.set_utility(True)
    dark = False
    for _ in range(int(120 / 0.05)):
        m.step(0.05)
        seen.update(a["pos"] for a in m.ats)
        dark = dark or not all(m.ats_live(a) for a in m.ats)
    assert all(a["pos"] == "N" for a in m.ats)
    assert "B" in seen and not dark                             # make-before-break: no second outage
    run(m, 30)
    assert all(g["gen"].state == "cooldown" for g in m.gens)
    run(m, 300)
    assert all(g["gen"].state == "stop" for g in m.gens)


def test_load_step_dips_frequency():
    m = plant(building_kw_occ=0.0, building_kw_unocc=0.0)
    m.set_utility(False)
    run(m, 60)
    m.p["building_kw_occ"] = 1000.0                             # 25 % of 4 x 1000 kW in one step
    lo = 60.0
    for _ in range(100):
        m.step(0.1)
        lo = min(lo, m.bus_f)
    assert 55.5 < lo < 57.5
    assert m.bus_f > 59.5


def test_overload_sheds_lowest_priority_then_underfrequency_trips():
    m = plant(building_kw_occ=2500.0)
    for g in m.gens[2:]:
        g["gen"].set_switch(0)                                 # only 2 sets available
    m.set_utility(False)
    run(m, 60)
    assert ats(m, "ATS-4")["shed"] and ats(m, "ATS-4")["pos"] == "O"
    assert m.signals["load_shed"] and m.signals["shed_level"] == 1
    srv = ModbusTcpServer(m.modbus_units())
    assert read(srv, 5, 40900) == [1]
    # nothing left to shed: sustained underfrequency trips the sets (1448)
    m2 = plant(building_kw_occ=2500.0)
    for a in m2.ats:
        a["shed_order"] = 0
    for g in m2.gens[2:]:
        g["gen"].set_switch(0)
    m2.set_utility(False)
    run(m2, 60)
    assert all(g["gen"].fault_code == 1448 for g in m2.gens[:2])
    assert not m2.bus_live()


def test_load_demand_keeps_n_plus_one():
    m = plant(building_kw_occ=300.0, load_demand_delay_min=1.0)
    m.set_utility(False)
    run(m, 200, dt=1.0)
    assert m.signals["sets_online"] == 2                        # 300 kW: one needed + one spare
    stopped = [g for g in m.gens if g["gen"].load_demand_stopped]
    assert len(stopped) == 2
    srv = ModbusTcpServer(m.modbus_units())
    assert read(srv, int(stopped[0]["unit_id"]), 40017)[0] & (1 << 3)   # extended NFPA: load demand


# ----- host ----------------------------------------------------------------------------


def test_host_power_follows_ats():
    asyncio.run(_host_power())


async def _host_power():
    cfg = load_host_config("configs/station-1.yaml")
    host = Host(cfg, demo=True, demo_controllers=False)
    try:
        gen = host.unit("GEN-PLANT-1").model
        names = {x["name"]: x["ats"]["tag"] for x in gen.loads}
        assert names["CHP-1"] == "ATS-2" and names["HWP-1"] == "ATS-3" and names["AHU-1"] == "ATS-4"
        assert [u.name for u in host.units][:2] == ["WX-1", "GEN-PLANT-1"]
        host.step(1.0)
        host.power(False)
        host.step(1.0)
        chp, ahu = host.unit("CHP-1").model, host.unit("AHU-1").model
        assert not chp.utility_ok and not ahu.power_ok
        assert host.unit("SF-1-VFD").app.offline                     # drive fieldbus dark with the drive
        for _ in range(40):
            host.step(1.0)
        assert ahu.power_ok and chp.utility_ok                       # on the generator bus
        assert chp.signals["utility_power"] is False                 # phase monitor still sees no utility
        assert not host.unit("SF-1-VFD").app.offline
    finally:
        host.close()


def test_modbus_tcp_over_the_network_and_demo():
    asyncio.run(_demo())


async def _demo():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "GEN-PLANT-1", "AHU-1")]
    cfg["instances"][-1 if cfg["instances"][-1]["name"] == "GEN-PLANT-1" else 0]["plant"]["modbus"] = {"port": 0}
    cfg["host"]["speed"] = 10.0
    host = Host(cfg, demo=True)
    task = asyncio.create_task(host.run())
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            if host.modbus.server is not None:
                break
        cli = ModbusTcpClient("127.0.0.1", host.modbus.port)
        assert await cli.read(1, 40010) == [1]
        regs = await cli.read(5, 40257, 9)
        assert regs[2] == 1                                           # ATS-1 on source 1
        host.power(False)
        loop = asyncio.get_running_loop()
        end = loop.time() + 30
        gen = host.unit("GEN-PLANT-1")
        while loop.time() < end:
            await asyncio.sleep(0.5)
            if all(a["pos"] == "E" for a in gen.model.ats):
                break
        assert all(a["pos"] == "E" for a in gen.model.ats), [a["status"] for a in gen.model.ats]
        assert (await cli.read(5, 40259))[0] == 2
        bad = [(c, p.object_name, p.last_error) for c, l in gen.links.items() for p in l.points if not p.ok]
        assert not bad, bad[:3]
        logic = [lg for m in host.mocks for lg in m.logics if type(lg).__name__ == "GenPlantDemoLogic"][0]
        assert "utility FAILED" in logic.status
        cli.close()
    finally:
        task.cancel()
        host.close()


def test_shed_load_returns_to_normal_when_utility_returns():
    # a shed load bigger than the spare generator capacity must still come back on utility
    m = plant(building_kw_occ=3800.0)
    m.gens[3]["gen"].set_switch(0)
    m.set_utility(False)
    run(m, 60)
    assert ats(m, "ATS-4")["shed"]
    m.set_utility(True)
    run(m, 5)
    assert ats(m, "ATS-4")["pos"] == "N" and not ats(m, "ATS-4")["shed"]
    assert m.shed_level == 0
