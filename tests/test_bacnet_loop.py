"""
End-to-end BACnet tests of a simulator host: router + virtual network +
several equipment instances, talking to mock ALC controllers.

The "LAN" side is an in-process virtual network (the same path --demo uses),
so broadcasts (Who-Is, Who-Has, I-Am-Router-To-Network) work without real
network interfaces.  The only difference from the lab is the router's
upstream port type (virtual instead of BACnet/IP).
"""

import asyncio
import copy

import pytest
from bacpypes3.basetypes import BinaryPV
from bacpypes3.primitivedata import Null

from hvacsim import bacnet_io
from hvacsim.config import ConfigError, load_host_config, resolve_instance, validate
from hvacsim.mockalc import MockController
from hvacsim.runner import DEMO_LAN, Host

LAN = DEMO_LAN


def host_config(**over):
    tmpl = {
        "equipment": {"type": "hw_unit_heater", "params": {}},
        "commands": [{"object": "binary-output,1", "name": "HWV_CMD", "input": "valve_cmd"}],
        "controller": {
            "min_interval": 0.2,
            "writes": [
                {"signal": "fan_amps", "object_name": "fan_amps_{n}", "deadband": 0.05, "heartbeat": 5},
                {"signal": "space_temp", "object_name": "zone_temp_{n}", "deadband": 0.1, "heartbeat": 5},
            ],
        },
        "monitor": [{"object": "binary-value,101", "name": "AQUASTAT", "signal": "aquastat_closed"}],
    }
    insts = []
    for n, ctl in ((1, 240110), (2, 240110), (3, 240120)):
        inst = copy.deepcopy(tmpl)
        inst.update({"name": f"UH-{n}-SIM", "device_instance": 4010000 + n, "mac": n,
                     "vars": {"n": n}})
        inst["controller"]["device_instance"] = ctl
        insts.append(resolve_instance(inst, None, n - 1))
    cfg = {
        "host": {"name": "SIM-HOST-T", "device_instance": 4010000, "address": "unused",
                 "ip_network": 1, "virtual_network": 5001, "speed": 50.0, "step": 0.05},
        "instances": insts,
    }
    cfg.update(over)
    validate(cfg)
    return cfg


def make_controller(dev, mac, points):
    mock = MockController(dev, bacnet_io.virtual_port(1, LAN, 1, mac), name=f"ALC-{dev}")
    for name, obj in points:
        mock.add_point(name, obj, "noUnits")
    return mock


async def wait_for(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


def test_host_with_router_and_object_names():
    asyncio.run(_host_test())


async def _host_test():
    # demo=True -> router upstream on the in-process LAN; our own controllers instead of the demo ones
    host = Host(host_config(), demo=True, demo_controllers=False)
    # two units share controller 240110 (names _1 and _2 at non-obvious object ids)
    alc_a = make_controller(240110, 0x20, [("fan_amps_1", "analog-input,7"), ("zone_temp_1", "analog-value,12"),
                                           ("fan_amps_2", "analog-input,8"), ("zone_temp_2", "analog-value,13")])
    # UH-3's controller is missing zone_temp_3 (trainee hasn't created it yet)
    alc_b = make_controller(240120, 0x21, [("fan_amps_3", "analog-input,1")])
    run = asyncio.create_task(host.run())
    try:
        await asyncio.sleep(0.3)
        # the ALC controller discovers the router and every instance behind it
        i_ams = await alc_a.app.who_is(4010000, 4010099, timeout=1)
        found = {i.iAmDeviceIdentifier[1]: str(i.pduSource) for i in i_ams}
        assert found.get(4010001) == "5001:1"
        assert found.get(4010002) == "5001:2"
        assert found.get(4010003) == "5001:3"
        assert 4010000 in found  # the router itself

        # network output: controller commands only UH-2's valve
        await alc_a.app.write_property(found[4010002], "binary-output,1", "present-value",
                                       BinaryPV.active, priority=10)
        uh1, uh2, uh3 = host.units
        assert await wait_for(lambda: uh2.model.inputs["valve_cmd"] is True, 3)
        assert uh1.model.inputs["valve_cmd"] is False

        # values arrive at the objects found by NAME
        assert await wait_for(lambda: alc_a.value("fan_amps_2") > 1.5, 15), "fan_amps_2 never written"
        assert alc_a.value("fan_amps_1") < 0.2           # UH-1 fan is off
        assert 55 < alc_a.value("zone_temp_1") < 80
        assert 55 < alc_a.value("zone_temp_2") < 80
        p = uh2.link.points[0]
        assert p.objid[1] == 8 and str(p.objid[0]) == "analog-input" and p.found_by == "object-list"

        # missing object name is reported, the present one still works
        assert await wait_for(lambda: uh3.link.points[1].last_error != "", 5)
        assert "zone_temp_3" in uh3.link.points[1].last_error
        assert await wait_for(lambda: uh3.link.points[0].ok is True, 5)

        # input taken back in service -> write-access-denied reported
        alc_a.app.objectName["fan_amps_2"].outOfService = False
        uh2.link.points[0].last_value = None
        assert await wait_for(lambda: uh2.link.points[0].ok is False, 5)
        assert "Out_Of_Service" in uh2.link.points[0].last_error

        # retarget UH-1 to the other controller: name lookup starts over there
        uh1.link.retarget(240120)
        assert await wait_for(lambda: uh1.link.points[0].last_error != "", 5)
        assert "fan_amps_1" in uh1.link.points[0].last_error and "240120" in uh1.link.points[0].last_error

        # relinquish: back to relinquish default (closed)
        await alc_a.app.write_property(found[4010002], "binary-output,1", "present-value", Null(()), priority=10)
        assert await wait_for(lambda: uh2.model.inputs["valve_cmd"] is False, 3)
    finally:
        run.cancel()
        for m in (alc_a, alc_b):
            m.app.close()
        host.close()


def test_who_has_fallback(monkeypatch):
    """If the object-list cannot be read, names are found with Who-Has."""
    async def broken_scan(self, app, address, device):
        raise RuntimeError("no object list")
    monkeypatch.setattr(bacnet_io.NameDirectory, "_scan", broken_scan)
    asyncio.run(_who_has())


async def _who_has():
    cfg = host_config()
    cfg["instances"] = cfg["instances"][:1]
    host = Host(cfg, demo=True, demo_controllers=False)
    alc = make_controller(240110, 0x20, [("fan_amps_1", "analog-input,3"), ("zone_temp_1", "analog-value,4")])
    run = asyncio.create_task(host.run())
    try:
        assert await wait_for(lambda: all(p.ok for p in host.units[0].link.points), 15)
        assert host.units[0].link.points[0].found_by == "who-has"
        assert host.units[0].link.points[0].objid[1] == 3
    finally:
        run.cancel()
        alc.app.close()
        host.close()


def test_demo_mode_runs_whole_loop():
    asyncio.run(_demo())


async def _demo():
    host = Host(load_host_config("configs/station-1.yaml"), demo=True)
    host.speed = 50
    run = asyncio.create_task(host.run())
    try:
        mock = host.mocks[0]
        assert await wait_for(lambda: (mock.value("fan_amps_1") or 0) > 1.5 and (mock.value("fan_amps_2") or 0) > 1.5, 20)
        assert all(t.call for t in mock.thermostats)
    finally:
        run.cancel()
        host.close()


def test_config_validation():
    cfg = host_config()
    bad = copy.deepcopy(cfg)
    bad["instances"][1]["mac"] = 1
    with pytest.raises(ConfigError):
        validate(bad)
    bad = copy.deepcopy(cfg)
    bad["host"]["virtual_network"] = 1
    with pytest.raises(ConfigError):
        validate(bad)
    with pytest.raises(ConfigError):
        resolve_instance({"name": "X", "device_instance": 5, "controller": {"writes": [{"object_name": "a_{zz}"}]}},
                         None, 0)


def test_station_config_template_vars():
    cfg = load_host_config("configs/station-1.yaml")
    names = [[w["object_name"] for w in i["controller"]["writes"]] for i in cfg["instances"]]
    assert names == [["fan_amps_1", "zone_temp_1"], ["fan_amps_2", "zone_temp_2"]]
    assert cfg["instances"][1]["equipment"]["params"]["oat"] == 25.0
    assert cfg["instances"][1]["equipment"]["params"]["hw_supply_temp"] == 180.0


def test_controller_address_qualification():
    assert bacnet_io.controller_address("192.168.1.20", 1) == "1:192.168.1.20"
    assert bacnet_io.controller_address("192.168.1.20:47808", 7) == "7:192.168.1.20:47808"
    assert bacnet_io.controller_address("1:192.168.1.20", 7) == "1:192.168.1.20"
    assert bacnet_io.controller_address(None, 1) is None
