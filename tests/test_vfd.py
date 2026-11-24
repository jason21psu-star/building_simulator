"""Tests for the ACH580-style VFD: offline drive behaviour and BACnet control in both modes."""

import asyncio

import pytest
from bacpypes3.basetypes import BinaryPV
from bacpypes3.primitivedata import Real

from hvacsim import bacnet_io
from hvacsim.config import load_host_config
from hvacsim.mockalc import MockController
from hvacsim.models import MODELS
from hvacsim.models.vfd_fan import VfdFan
from hvacsim.runner import DEMO_LAN, Host


def make(**params):
    return VfdFan(params, seed=1)


def run(m, seconds, dt=0.5):
    for _ in range(int(seconds / dt)):
        m.step(dt)


def hardwired_running(**params):
    m = make(**params)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 100)
    run(m, 60)
    return m


# ----- offline drive behaviour ----------------------------------------------


def test_registered():
    assert MODELS["vfd_fan"] is VfdFan


def test_hardwired_start_and_ai1_scaling():
    m = make()
    m.set_input("di1", True)
    m.set_input("ai1_pct", 80)                  # 8 V -> 48 Hz with 0-10 V = 0-60 Hz
    run(m, 10)
    assert m.signals["output_freq"] == pytest.approx(30.0, abs=1.0)   # 28.72 default 20 s: 60 Hz / 20 s = 3 Hz/s
    run(m, 40)
    assert m.signals["output_freq"] == pytest.approx(48.0, abs=0.2)
    assert m.signals["ro2"] and m.signals["ro3"]          # running, no fault (RO3 = fault inverted)
    assert m.signals["ao1_pct"] == pytest.approx(80.0, abs=0.5)
    # fan law: torque ~ 0.85 * 0.8^2
    assert m.signals["torque_pct"] == pytest.approx(54.4, abs=1.0)


def test_2_10v_live_zero():
    m = make(ai1_min_v=2.0)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 60)                  # 6 V -> (6-2)/8 = 50 % -> 30 Hz
    run(m, 60)
    assert m.signals["output_freq"] == pytest.approx(30.0, abs=0.2)


def test_min_freq_clamp():
    m = make(min_freq=12.0)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 5)
    run(m, 30)
    assert m.signals["output_freq"] == pytest.approx(12.0, abs=0.2)


def test_coast_stop_and_flying_start():
    m = hardwired_running()
    m.set_input("di1", False)
    run(m, 5)
    assert m.signals["output_freq"] == 0 and m.signals["fan_rotating"]   # coasting
    assert not m.signals["ro2"]
    m.set_input("di1", True)
    m.step(0.5)
    assert m.signals["output_freq"] > 20        # picked up the spinning fan


def test_short_decel_without_overvoltage_control_trips_3210():
    m = hardwired_running(stop_mode="ramp", decel_time_1=3.0, overvoltage_control=False)
    m.set_input("di1", False)
    run(m, 10)
    assert m.signals["fault_code"] == 3210
    assert not m.signals["ro3"]                 # fault relay opened


def test_overvoltage_control_extends_decel():
    m = hardwired_running(stop_mode="ramp", decel_time_1=3.0)
    m.set_input("di1", False)
    m.step(0.5)
    assert "Overvoltage control" in m.signals["warnings"]
    run(m, 60)
    assert m.signals["fault_code"] == 0 and m.signals["output_freq"] == 0


def test_run_permissive_from_safety_circuit():
    m = make(run_permissive="di4", safety_di=4)
    m.set_input("di4", True)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)
    run(m, 40)
    assert m.signals["running"]
    m.set_fault("safety_circuit_open", True)
    m.step(0.5)
    assert not m.signals["running"] and "Run permissive" in m.signals["warnings"]
    m.set_fault("safety_circuit_open", False)
    run(m, 5)
    assert m.signals["running"]                 # resumes without a reset


# ----- 28.76 Freq ramp in zero ------------------------------------------------


def test_ramp_in_zero_active_holds_zero_speed_running():
    m = hardwired_running(min_freq=12.0)
    assert m.signals["output_freq"] == pytest.approx(60.0, abs=0.2)
    m.set_param("freq_ramp_in_zero", "active")
    m.step(0.5)
    assert m.signals["ramp_in_zero"] and m.signals["running"]
    run(m, 10)
    assert 0 < m.signals["output_freq"] < 60          # ramping down on 28.73, not coasting
    run(m, 30)
    s = m.signals
    assert s["output_freq"] == 0.0                     # held at zero even with 30.13 = 12 Hz
    assert s["running"] and s["ro2"]                   # still running: RO2 (running) stays closed
    assert s["zero_speed"] and s["airflow_cfm"] == 0
    assert s["motor_current"] == pytest.approx(0.35 * 14.0, abs=0.3)   # magnetizing current only
    assert "RAMP IN ZERO" in s["status_text"] and s["fault_code"] == 0
    run(m, 120)                                         # holding zero is not a stall
    assert m.signals["fault_code"] == 0


def test_ramp_in_zero_from_di_releases_without_new_start():
    m = make(freq_ramp_in_zero="di3")
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)                          # 30 Hz
    run(m, 20)
    assert m.signals["output_freq"] == 0.0 and m.signals["running"]   # DI3 open = hold zero
    m.set_input("di3", True)                            # source = 1: normal operation
    run(m, 20)
    assert m.signals["output_freq"] == pytest.approx(30.0, abs=0.2)
    m.set_input("di3", False)
    run(m, 20)
    assert m.signals["output_freq"] == 0.0 and m.signals["running"]


def test_ramp_in_zero_inactive_by_default_and_not_in_hand():
    m = make()
    assert m.p["freq_ramp_in_zero"] == "inactive"
    m.set_param("freq_ramp_in_zero", "active")
    m.action("hand")
    m.action("hand_ref", 20)
    run(m, 20)
    assert m.signals["output_freq"] == pytest.approx(20.0, abs=0.2)   # local (Hand) control is not affected


def test_ramp_in_zero_over_fieldbus():
    m = make(ext1_commands="fieldbus", ext1_freq_ref1="efb_ref1", freq_ramp_in_zero="active")
    m.set_input("efb_run", True)
    m.set_input("efb_ref1_pct", 80)
    run(m, 20)
    assert m.signals["running"] and m.signals["output_freq"] == 0.0 and m.signals["ref_hz"] == 0.0


# ----- start interlock / run permissive (manual behaviour) --------------------


def test_start_interlock_default_di4_blocks_start_and_stops_running_drive():
    m = make()
    assert m.p["start_interlock_1"] == "di4" and m.p["start_interlock_stop_mode"] == "not_used"
    m.set_input("di4", False)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)
    run(m, 5)
    assert not m.signals["running"] and "AFEE" in m.signals["warnings"]
    m.set_input("di4", True)
    run(m, 20)
    assert m.signals["running"]
    m.set_input("di4", False)                           # opening while running always stops the fan
    m.step(0.5)
    assert not m.signals["running"] and m.signals["output_freq"] == 0   # 20.45 Not used -> 21.03 Coast
    assert "AFEE" in m.signals["warnings"]


def test_start_interlock_coasts_even_with_ramp_stop_mode_and_restarts():
    m = make(start_interlock_stop_mode="ramp")
    m.set_input("di1", True)
    m.set_input("ai1_pct", 100)
    run(m, 30)
    m.set_input("di4", False)
    run(m, 5)
    assert m.signals["running"] and 30 < m.signals["output_freq"] < 60   # ramping down on 28.73
    run(m, 30)
    assert not m.signals["running"]
    m = make(stop_mode="ramp")                          # 20.45 Not used: coasts even though 21.03 = Ramp
    m.set_input("di1", True)
    m.set_input("ai1_pct", 100)
    run(m, 30)
    m.set_input("di4", False)                           # e.g. high-static switch trips
    m.step(0.5)
    assert not m.signals["running"] and m.signals["output_freq"] == 0 and m.signals["fan_rotating"]
    m.set_input("di4", True)                            # interlock closes, DI1 start still made
    m.step(0.5)
    assert m.signals["running"] and m.signals["output_freq"] > 20   # restarts (flying start), no new start


def test_start_interlock_stop_mode_coast():
    m = make(start_interlock_stop_mode="coast")
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)
    run(m, 20)
    m.set_input("di4", False)
    m.step(0.5)
    assert not m.signals["running"] and m.signals["output_freq"] == 0
    m.set_input("di4", True)                            # interlock back: restarts (start still on)
    run(m, 5)
    assert m.signals["running"]


def test_external_event_as_warning():
    m = make(external_event_1="di5", external_event_1_type="warning")
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)
    run(m, 10)                                          # DI5 open = event active
    assert m.signals["fault_code"] == 0 and "A981" in m.signals["warnings"] and m.signals["running"]


def test_damper_control_relay_default():
    m = make(start_interlock_stop_mode="coast")
    assert m.p["ro1_source"] == "damper_control"
    m.set_input("di4", False)                           # damper end switch open
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)
    m.step(0.5)
    assert m.signals["ro1"] and not m.signals["running"]   # relay opens the damper, drive waits
    m.set_input("di4", True)                            # end switch made
    run(m, 2)
    assert m.signals["running"]


def test_sto_trips_5091_and_reset():
    m = hardwired_running()
    m.set_fault("sto_open", True)
    m.step(0.5)
    assert m.signals["fault_code"] == 5091
    m.action("reset")
    assert m.signals["fault_code"] == 5091      # cause still present
    m.set_fault("sto_open", False)
    m.action("reset")
    assert m.signals["fault_code"] == 0
    assert m.signals["last_fault"] == 5091


def test_external_event_fault_9081():
    m = make(external_event_1="di5")
    m.set_input("di5", True)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 50)
    run(m, 10)
    m.set_input("di5", False)
    m.step(0.5)
    assert m.signals["fault_code"] == 9081


def test_seized_fan_stalls_7121():
    m = make()
    m.set_fault("motor_locked", True)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 100)
    run(m, 40)
    assert m.signals["fault_code"] == 7121


def test_coupling_failure_low_current_no_air():
    m = hardwired_running()
    normal = m.signals["motor_current"]
    m.set_fault("coupling_failed", True)
    run(m, 5)
    assert m.signals["airflow_cfm"] == 0
    assert m.signals["motor_current"] < 0.6 * normal
    assert m.signals["running"]                 # the drive itself sees nothing wrong


def test_cooling_fan_failure_overheats():
    m = hardwired_running()
    m.set_fault("cooling_fan_failed", True)
    run(m, 3600, dt=1.0)
    assert m.signals["fault_code"] == 4310


def test_keypad_hand_off_auto():
    m = make()
    m.action("hand")
    m.action("hand_ref", 20)
    run(m, 30)
    assert m.signals["output_freq"] == pytest.approx(20, abs=0.2) and not m.signals["in_auto"]
    m.action("off")
    run(m, 2)
    assert not m.signals["running"]
    m.action("auto")
    assert m.signals["in_auto"]


def test_fieldbus_control_and_comm_loss():
    m = make(ext1_commands="fieldbus", ext1_freq_ref1="efb_ref1", comm_loss_action="fault", comm_loss_time=30.0)
    m.set_input("efb_run", True)
    m.set_input("efb_ref1_pct", 50)
    m.set_input("efb_idle_s", 1.0)
    run(m, 40)
    assert m.signals["output_freq"] == pytest.approx(30.0, abs=0.2)
    m.set_input("efb_idle_s", 31.0)
    m.step(0.5)
    assert m.signals["fault_code"] == 6681
    m.set_input("efb_idle_s", 1.0)
    m.set_input("efb_fault_reset", True)        # rising edge resets
    m.step(0.5)
    assert m.signals["fault_code"] == 0


def test_comm_loss_safe_speed():
    m = make(ext1_commands="fieldbus", ext1_freq_ref1="efb_ref1", comm_loss_action="speed_ref_safe",
             freq_ref_safe=25.0)
    m.set_input("efb_idle_s", 100.0)
    run(m, 40)
    assert m.signals["output_freq"] == pytest.approx(25.0, abs=0.2)


def test_override_ignores_permissive_and_faults():
    m = make(override_enable=True, override_source="di6", run_permissive="di4", override_freq=50.0)
    m.set_fault("earth_leakage", False)
    m.set_input("di6", True)                    # fire mode active, permissive DI4 open, no start
    run(m, 60)
    assert m.signals["override_active"] and m.signals["output_freq"] == pytest.approx(50.0, abs=0.2)


def test_param_validation():
    m = make()
    with pytest.raises(ValueError):
        m.set_param("ext1_commands", "bogus")
    with pytest.raises(ValueError):
        m.set_param("accel_time_1", -1)
    m.set_param("overvoltage_control", "false")
    assert m.p["overvoltage_control"] is False


# ----- BACnet: both modes through the router --------------------------------


def wait_for(pred, timeout=10.0):
    async def _w():
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return True
            await asyncio.sleep(0.1)
        return pred()
    return _w()


def test_bacnet_fieldbus_and_hardwired():
    asyncio.run(_bacnet())


async def _bacnet():
    cfg = load_host_config("configs/station-1.yaml")
    cfg["instances"] = [i for i in cfg["instances"] if i["name"].startswith("SF-")]
    cfg["host"].pop("weather", None)
    cfg["host"]["speed"] = 20.0
    cfg["instances"][0]["equipment"]["params"]["comm_loss_time"] = 2.0   # SF-1 fieldbus
    host = Host(cfg, demo=True, demo_controllers=False)
    alc = MockController(240110, bacnet_io.virtual_port(1, DEMO_LAN, host.ip_network, 0x20), name="ALC")
    for name, obj in (("sf2_status", "binary-input,1"), ("sf2_fault", "binary-input,2"), ("sf2_speed_fb", "analog-input,1")):
        alc.add_point(name, obj)
    sf1, sf2 = host.units
    run_task = asyncio.create_task(host.run())
    try:
        await asyncio.sleep(0.3)
        a1, a2 = f"5001:{sf1.cfg['mac']}", f"5001:{sf2.cfg['mac']}"

        # --- fieldbus: RUN/STOP CMD + INPUT REF 1, read OUTPUT FREQ back
        await alc.app.write_property(a1, "binary-value,10", "present-value", BinaryPV.active, priority=10)
        await alc.app.write_property(a1, "analog-value,16", "present-value", Real(50.0), priority=10)
        # keep the fieldbus alive with polls, like WebCTRL does
        async def poll():
            while True:
                await alc.app.read_property(a1, "analog-value,1", "present-value")
                await asyncio.sleep(0.5)
        poller = asyncio.create_task(poll())
        assert await wait_for(lambda: abs(sf1.model.signals["output_freq"] - 30.0) < 0.3, 10)
        freq = await alc.app.read_property(a1, "analog-value,1", "present-value")
        assert float(freq) == pytest.approx(30.0, abs=0.3)
        name = await alc.app.read_property(a1, "analog-value,16", "object-name")
        assert str(name) == "INPUT REF 1"

        # parameter object: write ACCEL1 TIME over BACnet
        await alc.app.write_property(a1, "analog-value,23", "present-value", Real(45.0))
        assert await wait_for(lambda: sf1.model.p["accel_time_1"] == 45.0, 3)

        # comm loss: stop polling -> fault 6681 after 58.16 (2 s), visible as LAST FLT
        poller.cancel()
        assert await wait_for(lambda: sf1.model.signals["fault_code"] == 6681, 8)
        last = await alc.app.read_property(a1, "analog-value,18", "present-value")
        assert float(last) == 6681
        # reset over BACnet (rising edge on FAULT RESET)
        await alc.app.write_property(a1, "binary-value,14", "present-value", BinaryPV.active, priority=10)
        assert await wait_for(lambda: sf1.model.signals["fault_code"] == 0, 3)

        # --- hardwired: ALC network outputs write the DI1 / AI1 terminals
        await alc.app.write_property(a2, "binary-value,1001", "present-value", BinaryPV.active, priority=10)
        await alc.app.write_property(a2, "analog-value,1001", "present-value", Real(70.0), priority=10)
        assert await wait_for(lambda: alc.value("sf2_status") is True, 10), "RO2 status never reached sf2_status"
        assert await wait_for(lambda: (alc.value("sf2_speed_fb") or 0) > 69, 10)
        assert alc.value("sf2_fault") is True     # RO3 closed = healthy
        sf2.model.set_fault("sto_open", True)
        assert await wait_for(lambda: alc.value("sf2_fault") is False, 5)
        assert await wait_for(lambda: alc.value("sf2_status") is False, 5)
    finally:
        run_task.cancel()
        alc.app.close()
        host.close()
