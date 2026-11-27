"""
Golden-trace scenarios for the drive refactor (docs/DRIVE_INTERFACE_DESIGN.md, section 4).

Each scenario drives a model through a fixed script and returns snapshots of
its signals.  tests/golden/*.json hold the snapshots recorded from the code
before the refactor; tests/test_golden.py replays the scenarios and compares.

Record (only when a change is *meant* to alter behaviour):
    python -m tests.golden.scenarios --record
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

HERE = Path(__file__).parent


def _snap(m, keys=None) -> Dict[str, Any]:
    s = m.signals
    out = {}
    for k in sorted(keys or s):
        v = s[k]
        if isinstance(v, float):
            v = round(v, 6)
        out[k] = v
    return out


def _run(m, seconds, dt, every, snaps, keys=None, tag=""):
    n = int(round(seconds / dt))
    for i in range(n):
        m.step(dt)
        if (i + 1) % int(round(every / dt)) == 0:
            snaps.append({"t": round(m.sim_time, 3), "tag": tag, **_snap(m, keys)})


# ----------------------------------------------------------------------------- standalone drive

def vfd_hardwired() -> List[Dict[str, Any]]:
    from hvacsim.models import MODELS
    m = MODELS["vfd_fan"]({"accel_time_1": 30.0, "decel_time_1": 30.0, "min_freq": 12.0, "safety_di": 4,
                           "ro1_source": "damper_control"}, seed=3)
    snaps: List[Dict[str, Any]] = []
    m.set_input("di1", True)
    m.set_input("ai1_pct", 75.0)
    _run(m, 60, 0.2, 2, snaps, tag="start")
    m.set_input("ai1_pct", 40.0)
    _run(m, 40, 0.2, 2, snaps, tag="slow")
    m.set_fault("safety_circuit_open", True)
    _run(m, 10, 0.2, 1, snaps, tag="safety")
    m.set_fault("safety_circuit_open", False)
    _run(m, 40, 0.2, 2, snaps, tag="restart")
    m.action("hand")
    m.action("hand_ref", 50.0)
    _run(m, 30, 0.2, 2, snaps, tag="hand")
    m.action("off")
    _run(m, 20, 0.2, 2, snaps, tag="off")
    m.action("auto")
    _run(m, 40, 0.2, 2, snaps, tag="auto")
    m.set_fault("overcurrent_trip", True)
    _run(m, 6, 0.2, 1, snaps, tag="oc")
    m.set_fault("overcurrent_trip", False)
    m.action("reset")
    _run(m, 40, 0.2, 2, snaps, tag="reset")
    m.set_power(False)
    _run(m, 20, 0.2, 2, snaps, tag="outage")
    m.set_power(True)
    _run(m, 40, 0.2, 2, snaps, tag="power back")
    m.set_input("di1", False)
    _run(m, 40, 0.2, 2, snaps, tag="stop")
    m.set_fault("cooling_fan_failed", True)
    m.set_input("di1", True)
    m.set_input("ai1_pct", 100.0)
    m.p["fan_load_pu"] = 1.1
    _run(m, 600, 0.5, 20, snaps, tag="hot")
    return snaps


def vfd_fieldbus() -> List[Dict[str, Any]]:
    from hvacsim.models import MODELS
    m = MODELS["vfd_fan"]({"ext1_commands": "fieldbus", "ext1_freq_ref1": "efb_ref1", "start_interlock_1": "selected",
                           "comm_loss_mode": "any_message", "comm_loss_time": 30.0, "min_freq": 12.0,
                           "accel_time_1": 30.0, "decel_time_1": 30.0}, seed=4)
    snaps: List[Dict[str, Any]] = []
    m.set_input("efb_run", True)
    m.set_input("efb_ref1_pct", 80.0)
    _run(m, 50, 0.2, 2, snaps, tag="run")
    m.set_input("efb_run_enable", False)
    _run(m, 10, 0.2, 1, snaps, tag="permissive")
    m.set_input("efb_run_enable", True)
    _run(m, 30, 0.2, 2, snaps, tag="enable")
    for i in range(40):                        # the ALC goes quiet
        m.set_input("efb_idle_s", float(i))
        m.step(1.0)
    snaps.append({"t": m.sim_time, "tag": "comm loss", **_snap(m)})
    m.set_input("efb_idle_s", 0.0)
    m.set_input("efb_fault_reset", True)
    _run(m, 2, 0.2, 1, snaps, tag="reset")
    m.set_input("efb_fault_reset", False)
    _run(m, 40, 0.2, 2, snaps, tag="after")
    return snaps


# ----------------------------------------------------------------------------- AHU fan array

def ahu() -> List[Dict[str, Any]]:
    from hvacsim import psychro
    from hvacsim.models import MODELS
    m = MODELS["ahu_vav_econ"]({"oat": 55.0, "oa_w": psychro.w_from_rh(55.0, 50.0, psychro.p_atm(0)),
                                "clock_start": "2026-11-23 10:00"}, seed=1)
    snaps: List[Dict[str, Any]] = []
    for s in ("sf1", "sf2", "sf3", "sf4"):
        m.set_input(f"{s}_ss", True)
        m.set_input(f"{s}_vfd_spd", 80.0)
    m.set_input("rf_ss", True)
    m.set_input("rf_vfd_spd", 64.0)
    m.set_input("ma_dmpr", 36.0)
    _run(m, 200, 1.0, 5, snaps, tag="start")
    m.action("sf2.hand")
    m.action("sf2.hand_ref", 40.0)
    _run(m, 60, 1.0, 5, snaps, tag="hand")
    m.action("sf2.auto")
    m.set_fault("fire_damper_closed", True)
    _run(m, 20, 1.0, 1, snaps, tag="high static")
    m.set_fault("fire_damper_closed", False)
    _run(m, 60, 1.0, 5, snaps, tag="tripped")
    m.action("hs_reset")
    _run(m, 120, 1.0, 5, snaps, tag="reset")
    m.set_fault("sf3_overcurrent_trip", True)
    _run(m, 20, 1.0, 2, snaps, tag="sf3 oc")
    m.set_fault("sf3_overcurrent_trip", False)
    m.action("sf3.reset")
    _run(m, 60, 1.0, 5, snaps, tag="sf3 reset")
    m.set_power(False)
    _run(m, 30, 1.0, 2, snaps, tag="outage")
    m.set_power(True)
    _run(m, 90, 1.0, 5, snaps, tag="power back")
    return snaps


# ----------------------------------------------------------------------------- plant pumps

def chw_plant() -> List[Dict[str, Any]]:
    from hvacsim.models.chw_plant import ChwPlant
    m = ChwPlant({"clock_start": "2026-07-15 10:00", "oat": 92.0, "oa_w": 0.015, "sensor_noise": 0.0}, seed=1)
    I = m.inputs
    for k in m.tanks:
        I[f"{k['pre']}_top_vlv"] = I[f"{k['pre']}_bot_vlv"] = True
    for t in m.towers:
        I[f"{t['pre']}_outlet_vlv"] = I[f"{t['pre']}_eq_vlv"] = True
    I["pchwp1_ss"], I["pchwp1_vfd_spd"] = True, 91.7
    I["cwp1_ss"], I["cwp1_vfd_spd"] = True, 91.7
    I["ch1_chw_vlv"] = I["ch1_cw_vlv"] = True
    I["ct1_inlet_vlv"], I["ct1_fan_ss"], I["ct1_fan_vfd_spd"] = True, True, 100.0
    for i in (1, 2):
        I[f"schwp{i}_ss"], I[f"schwp{i}_vfd_spd"] = True, 95.0
    I["ch1.rem_run"] = True
    snaps: List[Dict[str, Any]] = []
    _run(m, 600, 5.0, 30, snaps, tag="start")
    m.action("pchwp1.hand")
    m.action("pchwp1.hand_ref", 40.0)
    _run(m, 120, 5.0, 30, snaps, tag="hand")
    m.action("pchwp1.auto")
    m.set_fault("schwp1_overcurrent_trip", True)
    _run(m, 60, 5.0, 15, snaps, tag="oc")
    m.set_fault("schwp1_overcurrent_trip", False)
    m.action("schwp1.reset")
    m.set_utility(False)
    _run(m, 60, 5.0, 15, snaps, tag="outage")
    m.set_utility(True)
    _run(m, 300, 5.0, 30, snaps, tag="power back")
    return snaps


def hw_plant() -> List[Dict[str, Any]]:
    from hvacsim.models.hw_plant import HwPlant
    m = HwPlant({"clock_start": "2026-01-14 10:00", "oat": 20.0, "sensor_noise": 0.0}, seed=1)
    I = m.inputs
    I["louver"] = True
    for n in (1, 2):
        I[f"phwp{n}_ss"], I[f"phwp{n}_vfd_spd"] = True, 91.7
        I[f"b{n}.bms_enable"], I[f"b{n}.bms_rate"] = True, 50.0
    I["shwp1_ss"], I["shwp1_vfd_spd"] = True, 80.0
    snaps: List[Dict[str, Any]] = []
    _run(m, 600, 5.0, 30, snaps, tag="start")
    m.set_fault("phwp2_seized", True)
    _run(m, 120, 5.0, 15, snaps, tag="seized")
    m.set_fault("phwp2_seized", False)
    m.action("phwp2.reset")
    _run(m, 120, 5.0, 30, snaps, tag="reset")
    return snaps


SCENARIOS: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
    "vfd_hardwired": vfd_hardwired,
    "vfd_fieldbus": vfd_fieldbus,
    "ahu": ahu,
    "chw_plant": chw_plant,
    "hw_plant": hw_plant,
}


def record() -> None:
    for name, fn in SCENARIOS.items():
        snaps = fn()
        (HERE / f"{name}.json").write_text(json.dumps(snaps, separators=(",", ":")))
        print(f"{name}: {len(snaps)} snapshots, {len(snaps[0])} signals each")


if __name__ == "__main__":
    if "--record" in sys.argv:
        record()
