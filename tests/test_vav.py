"""VAV box with reheat (vav_rh): box physics, zone, occupant controls, CSV schedule, AHU coupling, BACnet."""

import asyncio
import time

import pytest

from hvacsim import psychro
from hvacsim.config import ConfigError, load_host_config
from hvacsim.models import MODELS
from hvacsim.runner import Host

P = psychro.p_atm(0)


def box(**params):
    return MODELS["vav_rh"]({"clock_start": "2026-11-23 10:00", **params}, seed=1)


def run(m, seconds):
    for _ in range(int(seconds)):
        m.step(1.0)


# ----- box physics -------------------------------------------------------------


def test_airflow_follows_damper_and_inlet_static():
    m = box(box_max_cfm=1200, inlet_static=1.0)
    m.set_input("dmpr_1", 100)
    run(m, 200)                                       # 154 s stroke
    full = m.signals["airflow_1"]
    assert full == pytest.approx(1.6 * 1200 * (1 - 0.25 / 1.0 * 0.0) ** 0.5, rel=0.12)
    m.set_param("inlet_static", 0.25)
    run(m, 2)
    assert m.signals["airflow_1"] == pytest.approx(full / 2, rel=0.05)    # flow ~ sqrt(static)
    m.set_input("dmpr_1", 0)
    run(m, 200)
    assert m.signals["airflow_actual"] < 0.05 * full                      # 1 % leakage


def test_damper_stroke_time_154s():
    m = box()
    m.set_input("dmpr_1", 100)
    run(m, 77)
    assert m.signals["damper_pos"] == pytest.approx(50, abs=1)
    run(m, 80)
    assert m.signals["damper_pos"] == 100


def test_reheat_and_valve_signal():
    m = box(inlet_static=1.0, sa_temp=55)
    m.set_input("dmpr_1", 25)
    m.set_input("rh_vlv_1", 100)                      # 10 V
    run(m, 400)
    s = m.signals
    assert s["valve_pos"] == 100 and s["da_temp_1"] > 85 and s["reheat_mbh"] > 5
    m.set_input("rh_vlv_1", 20)                       # 2 V = closed; the coil's water cools off over ~10 min
    run(m, 900)
    assert m.signals["valve_pos"] == 0 and m.signals["da_temp_1"] < 60


def test_zone_heats_and_cools():
    m = box(inlet_static=1.0, sa_temp=55, zone_type="interior")
    m.set_input("dmpr_1", 100)
    run(m, 3600)
    cool = m.signals["zone_temp_actual"]
    assert cool < 70
    m.set_input("dmpr_1", 0)
    run(m, 3600)
    assert m.signals["zone_temp_actual"] > cool + 3   # internal gains warm it up again


# ----- occupant controls ----------------------------------------------------------


def test_override_button_adds_30_min_up_to_120():
    m = box()
    m.action("override_push")
    assert m.signals["override_remaining"] == 30 and m.signals["override_1"]
    for _ in range(4):
        m.action("override_push")
    assert m.signals["override_remaining"] == 120
    run(m, 600)
    assert m.signals["override_remaining"] == pytest.approx(110, abs=0.1)
    m._button_until = time.monotonic() - 1                # button released
    m.step(1)
    assert not m.signals["override_1"]


def test_override_puts_people_in_zone_after_hours():
    m = box(clock_start="2026-11-23 22:00")
    m.step(1)
    assert not m.signals["occupied"] and m.signals["occupants"] == 0
    m.action("override_push")
    m.step(1)
    assert m.signals["occupied"] and m.signals["occupants"] >= 1


def test_setpoint_adjust_range():
    m = box(stpt_adj_range=2.0)
    m.action("stpt_adj", 5)
    assert m.signals["stpt_adj_1"] == 2.0
    m.action("stpt_adj", -1.5)
    assert m.signals["stpt_adj_1"] == -1.5
    with pytest.raises(ValueError):
        m.set_param("stpt_adj_range", 6)


# ----- faults -----------------------------------------------------------------------


def test_airflow_sensor_faults():
    m = box(inlet_static=1.0)
    m.set_input("dmpr_1", 60)
    run(m, 200)
    true = m.signals["airflow_actual"]
    m.set_fault("flow_tube_pinched", True)
    m.step(1)
    assert m.signals["airflow_1"] == pytest.approx(0.3 * true, rel=0.05)
    m.set_fault("flow_tube_pinched", False)
    m.set_fault("flow_tubes_reversed", True)
    m.step(1)
    assert m.signals["airflow_1"] == 0


def test_actuator_failed_holds_and_air_bound_coil():
    m = box(inlet_static=1.0)
    m.set_input("dmpr_1", 40)
    run(m, 200)
    m.set_fault("actuator_failed", True)
    m.set_input("dmpr_1", 100)
    run(m, 200)
    assert m.signals["damper_pos"] == pytest.approx(40, abs=0.5)
    m.set_fault("coil_air_bound", True)
    m.set_input("rh_vlv_1", 100)
    run(m, 900)
    assert m.signals["valve_pos"] == 100 and m.signals["reheat_mbh"] < 2


# ----- CSV schedule ------------------------------------------------------------------


def test_default_schedule_expands_six_boxes():
    cfg = load_host_config("configs/station-1.yaml")
    vavs = [i for i in cfg["instances"] if i["equipment"]["type"] == "vav_rh"]
    assert [v["controller"]["device_instance"] for v in vavs] == list(range(240901, 240907))
    assert all(v["served_by"] == "AHU-1" for v in vavs)
    assert vavs[5]["equipment"]["params"]["zone_type"] == "conference"
    assert vavs[0]["controller"]["writes"][0]["object_name"] == "zone_temp_1"   # same names in every controller
    assert vavs[3]["controller"]["writes"][0]["object_name"] == "zone_temp_1"


def test_custom_schedule(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "vav_rh.yaml").write_text(open("configs/templates/vav_rh.yaml").read())
    (tmp_path / "boxes.csv").write_text(
        "tag,sim_device,mac,controller_device,zone_type,box_max_cfm,duct_position\n"
        "VAV-101,4019101,51,241001,east,800,\n"
        "# spare,,,,,,\n"
        "VAV-102,4019102,52,241002,,2000,0.9\n")
    (tmp_path / "host.yaml").write_text(
        "host: {name: H, device_instance: 4019000, address: 10.0.0.5/24, ip_network: 1, virtual_network: 5009}\n"
        "instances:\n  - schedule: boxes.csv\n    template: templates/vav_rh.yaml\n")
    cfg = load_host_config(str(tmp_path / "host.yaml"))
    a, b = cfg["instances"]
    assert a["name"] == "VAV-101" and a["controller"]["device_instance"] == 241001
    assert a["equipment"]["params"]["box_max_cfm"] == 800 and "duct_position" not in a["equipment"]["params"]
    assert b["equipment"]["params"]["duct_position"] == 0.9
    (tmp_path / "bad.csv").write_text("tag,sim_device\nVAV-1,abc\n")
    (tmp_path / "host2.yaml").write_text(
        "host: {name: H, device_instance: 4019000, address: 10.0.0.5/24, ip_network: 1, virtual_network: 5009}\n"
        "instances:\n  - schedule: bad.csv\n    template: templates/vav_rh.yaml\n")
    with pytest.raises(ConfigError):
        load_host_config(str(tmp_path / "host2.yaml"))


# ----- AHU coupling -------------------------------------------------------------------


def coupled():
    ahu = MODELS["ahu_vav_econ"]({"oat": 80.0, "oa_w": psychro.w_from_rh(80, 50, P),
                                  "clock_start": "2026-11-23 10:00"}, seed=1)
    boxes = []
    for zt, x in (("north", 0.35), ("south", 0.5), ("west", 0.9)):
        b = MODELS["vav_rh"]({"zone_type": zt, "duct_position": x}, seed=1)
        b.supply = ahu
        boxes.append(b)
    ahu.boxes = boxes
    for i in range(1, 5):
        ahu.set_input(f"sf{i}_ss", True)
        ahu.set_input(f"sf{i}_vfd_spd", 75)
    ahu.set_input("rf_ss", True)
    ahu.set_input("rf_vfd_spd", 60)
    ahu.set_input("ma_dmpr", 36)
    for b in boxes:
        b.set_input("dmpr_1", 60)
    return ahu, boxes


def step_all(ahu, boxes, seconds):
    for _ in range(int(seconds)):
        ahu.step(1.0)
        for b in boxes:
            b.step(1.0)


def test_boxes_in_ahu_duct():
    ahu, boxes = coupled()
    step_all(ahu, boxes, 300)
    s = ahu.signals
    assert s["boxes"] == 3 and s["boxes_cfm"] == pytest.approx(sum(b.q for b in boxes), rel=0.01)
    near, far = boxes[0].signals["inlet_static"], boxes[2].signals["inlet_static"]
    assert near > s["sa_static"] > far                   # static falls along the duct
    sp_open = s["sa_static"]
    for b in boxes:
        b.set_input("dmpr_1", 0)
    step_all(ahu, boxes, 250)
    assert ahu.signals["sa_static"] > sp_open + 0.1      # closing boxes raises duct static
    assert ahu.aggregate_share() == pytest.approx((24000 - 3600) / 24000)


def test_zone_air_returns_to_ahu():
    ahu, boxes = coupled()
    step_all(ahu, boxes, 300)
    base = ahu.signals["ra_temp"]
    boxes[0].tz = 90.0                                    # one hot zone
    ahu.step(1.0)
    assert ahu.signals["ra_temp"] > base + 0.3


# ----- BACnet demo ----------------------------------------------------------------------


def test_demo_vav_controllers_over_bacnet():
    asyncio.run(_demo())


async def _demo():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"] in ("WX-1", "AHU-1") or i["name"].startswith("VAV-")]
    cfg["host"]["speed"] = 10.0
    host = Host(cfg, demo=True)
    vavs = [u for u in host.units if u.model.TYPE == "vav_rh"]
    assert len(vavs) == 6 and all(v.served_by is host.unit("AHU-1") for v in vavs)
    assert host.units[0].name == "WX-1" and host.units[1].name == "AHU-1"
    task = asyncio.create_task(host.run())
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + 40
        while loop.time() < end:
            await asyncio.sleep(0.5)
            if all(p.ok for v in vavs for p in v.link.points) and all(v.model.signals["airflow_actual"] > 100 for v in vavs):
                break
        bad = [(v.name, p.object_name, p.last_error) for v in vavs for p in v.link.points if not p.ok]
        assert not bad, bad
        assert all(v.model.signals["airflow_actual"] > 100 for v in vavs)
        mocks = {m.device_instance: m for m in host.mocks}
        assert set(range(240901, 240907)) <= set(mocks)
        assert mocks[240903].value("zone_temp_1") == pytest.approx(vavs[2].model.signals["zone_temp_1"], abs=0.5)
    finally:
        task.cancel()
        host.close()
