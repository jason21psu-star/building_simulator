"""
Golden-trace scenarios for the drive refactor (docs/DRIVE_INTERFACE_DESIGN.md, section 4)
and the vendor interfaces (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 8).

Each scenario drives a model through a fixed script and returns snapshots of
its signals.  tests/golden/*.json.gz hold the snapshots recorded from the code
before the refactor; tests/test_golden.py replays the scenarios and compares.

Record (only when a change is *meant* to alter behaviour):
    python -m tests.golden.scenarios --record [scenario ...]
"""

from __future__ import annotations

import gzip
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


# ----------------------------------------------------------------------------- vendor equipment
# (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 8: recorded before the vendor interfaces)

def chiller_vendor() -> List[Dict[str, Any]]:
    """YORK YK: start, panel local/remote, loss of evaporator flow, reset, outage."""
    from hvacsim.models.chw_plant import ChwPlant
    m = ChwPlant({"clock_start": "2026-07-15 10:00", "oat": 92.0, "oa_w": 0.015, "sensor_noise": 0.0}, seed=2)
    I = m.inputs
    for k in m.tanks:
        I[f"{k['pre']}_top_vlv"] = I[f"{k['pre']}_bot_vlv"] = True
    for t in m.towers:
        I[f"{t['pre']}_outlet_vlv"] = I[f"{t['pre']}_eq_vlv"] = True
    I["pchwp1_ss"], I["pchwp1_vfd_spd"] = True, 91.7
    I["cwp1_ss"], I["cwp1_vfd_spd"] = True, 91.7
    I["ch1_chw_vlv"] = I["ch1_cw_vlv"] = True
    I["ct1_inlet_vlv"], I["ct1_fan_ss"], I["ct1_fan_vfd_spd"] = True, True, 100.0
    I["schwp1_ss"], I["schwp1_vfd_spd"] = True, 95.0
    I["ch1.rem_run"], I["ch1.rem_sp"], I["ch1.rem_cur_limit"] = True, 44.0, 100.0
    snaps: List[Dict[str, Any]] = []
    keys = None
    _run(m, 900, 5.0, 30, snaps, keys, tag="start")
    I["ch1.rem_sp"], I["ch1.rem_cur_limit"] = 42.0, 80.0
    _run(m, 300, 5.0, 30, snaps, keys, tag="sp + limit")
    m.action("ch1.local")
    m.set_param("ch1.local_sp", 46.0)
    _run(m, 120, 5.0, 30, snaps, keys, tag="local")
    m.action("ch1.remote")
    m.set_fault("ch1_evap_fs_failed", True)
    _run(m, 60, 5.0, 10, snaps, keys, tag="evap flow")
    m.set_fault("ch1_evap_fs_failed", False)
    _run(m, 20, 5.0, 10, snaps, keys, tag="flow back")
    m.action("ch1.reset")
    _run(m, 600, 5.0, 30, snaps, keys, tag="reset")
    m.set_fault("ch1_vsd_fault", True)
    _run(m, 60, 5.0, 10, snaps, keys, tag="vsd")
    m.set_fault("ch1_vsd_fault", False)
    _run(m, 10, 5.0, 10, snaps, keys, tag="vsd cleared")
    m.action("ch1.reset")
    m.set_utility(False)
    _run(m, 60, 5.0, 15, snaps, keys, tag="outage")
    m.set_utility(True)
    _run(m, 900, 5.0, 30, snaps, keys, tag="power back")
    return snaps


def boiler_vendor() -> List[Dict[str, Any]]:
    """Lochinvar Crest: sequence, rate following, ignition failure lockout, reset, local, comm loss."""
    from hvacsim.models.hw_plant import HwPlant
    m = HwPlant({"clock_start": "2026-01-14 10:00", "oat": 20.0, "sensor_noise": 0.0}, seed=2)
    I = m.inputs
    I["louver"] = True
    for n in (1, 2):
        I[f"phwp{n}_ss"], I[f"phwp{n}_vfd_spd"] = True, 91.7
    I["shwp1_ss"], I["shwp1_vfd_spd"] = True, 80.0
    I["b1.bms_enable"], I["b1.bms_rate"] = True, 60.0
    snaps: List[Dict[str, Any]] = []
    _run(m, 30, 5.0, 5, snaps, tag="warm pumps")
    I["b2.bms_enable"], I["b2.bms_rate"] = True, 60.0
    m.set_fault("b2_ignition_fail", True)
    _run(m, 180, 1.0, 5, snaps, tag="b2 ignition")
    m.set_fault("b2_ignition_fail", False)
    _run(m, 5, 1.0, 5, snaps, tag="b2 cleared")
    m.action("b2.reset")
    _run(m, 120, 1.0, 5, snaps, tag="b2 reset")
    I["b1.bms_rate"] = 90.0
    _run(m, 300, 5.0, 15, snaps, tag="rate 90")
    m.action("b1.local")
    _run(m, 120, 5.0, 15, snaps, tag="b1 local")
    m.action("b1.bms")
    m.set_fault("b1_gateway_offline", True)
    _run(m, 120, 5.0, 15, snaps, tag="b1 comm")
    m.set_fault("b1_gateway_offline", False)
    m.set_fault("b2_flue_blocked", True)
    _run(m, 600, 5.0, 30, snaps, tag="b2 flue")
    m.set_fault("b2_flue_blocked", False)
    _run(m, 5, 5.0, 5, snaps, tag="b2 flue cleared")
    m.action("b2.reset")
    I["louver"] = False
    _run(m, 60, 5.0, 15, snaps, tag="louver")
    I["louver"] = True
    m.set_utility(False)
    _run(m, 60, 5.0, 15, snaps, tag="outage")
    m.set_utility(True)
    _run(m, 300, 5.0, 30, snaps, tag="power back")
    return snaps


def _gen_snap(m, snaps, tag):
    regs = {}
    for g in m.gens:
        for r, v in sorted(g["regs"].items()):
            if v != 65535:                     # unlisted registers: constant, covered by test_gen_plant
                regs[f"{g['pre']}:{r}"] = v
    for r, v in sorted(m.master_regs.items()):
        regs[f"master:{r}"] = v
    snaps.append({"t": round(m.sim_time, 3), "tag": tag, **_snap(m), **regs})


def _gen_run(m, seconds, dt, every, snaps, tag):
    n = int(round(seconds / dt))
    for i in range(n):
        m.step(dt)
        if (i + 1) % int(round(every / dt)) == 0:
            _gen_snap(m, snaps, tag)


def gen_plant() -> List[Dict[str, Any]]:
    """Cummins PCC 3.3 plant: outage, start, sync, transfer, load step, shed, fault, closed transition back."""
    from hvacsim.models.gen_plant import DEFAULT_LAYOUT, GenPlant
    lay = {"ats": [{**a, "retransfer_delay_min": 2.0} for a in DEFAULT_LAYOUT["ats"]]}
    m = GenPlant({"clock_start": "2026-01-14 10:00", "building_kw_occ": 1200.0, "load_demand_delay_min": 2.0},
                 layout=lay, seed=2)
    snaps: List[Dict[str, Any]] = []
    _gen_run(m, 10, 0.5, 5, snaps, "normal")
    m.set_utility(False)
    _gen_run(m, 60, 0.5, 1, snaps, "outage")
    m.inputs["ats3_xfer_inhibit"] = True
    _gen_run(m, 20, 0.5, 2, snaps, "inhibit")
    m.inputs["ats3_xfer_inhibit"] = False
    _gen_run(m, 30, 0.5, 2, snaps, "release")
    m.p["building_kw_occ"] = 2200.0
    _gen_run(m, 20, 0.1, 0.5, snaps, "load step")
    m.set_fault("gen2_low_oil", True)
    _gen_run(m, 60, 0.5, 2, snaps, "gen2 low oil")
    m.set_fault("gen2_low_oil", False)
    _gen_run(m, 2, 0.5, 2, snaps, "gen2 cleared")
    m.action("gen2.reset")
    m.p["building_kw_occ"] = 3800.0
    m.gens[3]["gen"].set_switch(0)
    _gen_run(m, 60, 0.5, 2, snaps, "overload")
    m.gens[3]["gen"].set_switch(1)
    m.p["building_kw_occ"] = 1200.0
    m.set_fault("gen1_high_coolant", True)
    _gen_run(m, 600, 2.0, 20, snaps, "gen1 coolant")
    m.set_fault("gen1_high_coolant", False)
    _gen_run(m, 2, 1.0, 2, snaps, "gen1 cleared")
    m.action("gen1.off")
    m.action("gen1.reset")
    m.action("gen1.auto")
    m.set_utility(True)
    _gen_run(m, 200, 1.0, 5, snaps, "retransfer")
    _gen_run(m, 600, 2.0, 20, snaps, "cooldown")
    return snaps


SCENARIOS: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
    "vfd_hardwired": vfd_hardwired,
    "vfd_fieldbus": vfd_fieldbus,
    "ahu": ahu,
    "chw_plant": chw_plant,
    "hw_plant": hw_plant,
    "chiller_vendor": chiller_vendor,
    "boiler_vendor": boiler_vendor,
    "gen_plant": gen_plant,
}


def load(name: str) -> List[Dict[str, Any]]:
    with gzip.open(HERE / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def record(names=None) -> None:
    for name, fn in SCENARIOS.items():
        if names and name not in names:
            continue
        snaps = fn()
        with gzip.open(HERE / f"{name}.json.gz", "wt", encoding="utf-8") as f:
            f.write(json.dumps(snaps, separators=(",", ":")))
        print(f"{name}: {len(snaps)} snapshots, {len(snaps[0])} signals each")


if __name__ == "__main__":
    if "--record" in sys.argv:
        record([a for a in sys.argv[1:] if not a.startswith("--")])
