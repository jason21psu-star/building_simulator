"""
Natural gas generator plant (docs/GENERATOR_DESIGN.md).

  UTILITY ── ATS-1..n (normal side)
  GEN-1..n ─CB─ paralleling bus (DMC 1500 master) ── ATS-1..n (emergency side)
  ATS-n feeds the instances whose config says `ats: ATS-n` (power on/off each step)

The generators and the paralleling master are Modbus TCP units (Mode B,
hvacsim/modbus_tcp.py, register maps from Cummins A029X159); the ATS position
contacts, transfer inhibits, utility-available, generator-running and load-shed
points are hardwired to an ALC controller (Mode A, simulator I/O device).
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any, Dict, List, Optional

from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef
from .chw_plant import ChwPlant, prefix
from .components.pcc import FAULT_CODES, PCC_FAULTS, SEQ_TEXT, SWITCH_TEXT, Genset

_A = "analog"

EQUIPMENT_DEFAULTS = {
    "generators": {"unit_id": 1, "kw_rating": 1000.0, "volts": 480.0, "start_priority": 1},
    "ats": {"controller_device": 243001, "priority": 1, "shed_order": 0, "retransfer_delay_min": 30.0},
}
DEFAULT_LAYOUT: Dict[str, Any] = {
    "generators": [{"tag": f"GEN-{i}", "unit_id": i, "start_priority": i} for i in (1, 2, 3, 4)],
    "ats": [{"tag": "ATS-1", "priority": 1, "shed_order": 0, "description": "Critical / UPS"},
            {"tag": "ATS-2", "priority": 2, "shed_order": 1, "description": "CHW plant"},
            {"tag": "ATS-3", "priority": 3, "shed_order": 2, "description": "HW plant"},
            {"tag": "ATS-4", "priority": 4, "shed_order": 3, "description": "AHUs / building"}],
}
PLANT_SENSORS = {
    "utility_avail": "Utility available (closed = available)",
    "gen_running": "Generator(s) running (closed = running)",
    "load_shed": "Load shed signal from the paralleling master (closed = shedding)",
}
MASTER_UNIT = 5


def _rows(layout: Dict[str, Any], group: str) -> List[Dict[str, Any]]:
    out = []
    for row in layout.get(group) or []:
        r = dict(EQUIPMENT_DEFAULTS[group])
        r.update({k: v for k, v in row.items() if v not in (None, "")})
        r["tag"] = str(r["tag"])
        r["pre"] = prefix(r["tag"])
        out.append(r)
    return out


# ----------------------------------------------------------------------------- load kW per model

def unit_kw(model, split: bool = False):
    """(kW, kVAR) drawn by an equipment model; with split=True a chw_plant returns its UPS share separately."""
    t = model.TYPE
    kw = kvar = ups = 0.0

    def drive(v):
        return max(0.0, v.power_kw) if getattr(v, "powered", True) else 0.0

    if t == "chw_plant":
        for c in model.chillers:
            kw += max(0.0, c["yk"].kw)
        for g, items in model.pumps.items():
            for x in items:
                p = drive(model.drives[x["pre"]])
                if g == "secondary_pumps":
                    ups += p
                else:
                    kw += p
        for tw in model.towers:
            kw += drive(model.drives[tw["pre"]])
        kvar = 0.33 * kw
    elif t == "hw_plant":
        kw = sum(drive(v) for v in model.drives.values())
        kw += sum(1.5 for b in model.boilers if b["crest"].flame)
        kvar = 0.33 * kw
    elif t == "ahu_vav_econ":
        kw = sum(drive(v) for v in model.drives.values())
        kvar = 0.33 * kw
    elif t == "vfd_fan":
        kw = drive(model.vfd)
        kvar = 0.33 * kw
    elif t == "hw_unit_heater":
        kw = 0.17 * getattr(model, "motor_amps", 0.0)
        kvar = 0.75 * kw
    elif t == "vav_rh":
        kw = 0.05
    if split:
        return kw, kvar, ups
    return kw + ups, kvar + 0.33 * ups


# ----------------------------------------------------------------------------- Modbus units

class GenUnit:
    """Modbus unit for one generator (PowerCommand 3.x register map)."""

    def __init__(self, plant: "GenPlant", g: Dict[str, Any]):
        self.plant, self.g = plant, g

    def online(self) -> bool:
        return not self.plant.fault(f"{self.g['pre']}_comm_loss")

    def read(self, reg: int) -> Optional[int]:
        return self.g["regs"].get(reg)

    def write(self, reg: int, value: int) -> bool:
        gs: Genset = self.g["gen"]
        if reg == 40300:
            gs.remote_start = 1 if value else 0
            return True
        if reg == 40301:
            if value:
                gs.fault_reset()
            return True
        return False


class MasterUnit:
    def __init__(self, plant: "GenPlant"):
        self.plant = plant

    def online(self) -> bool:
        return True

    def read(self, reg: int) -> Optional[int]:
        return self.plant.master_regs.get(reg)

    def write(self, reg: int, value: int) -> bool:
        return False


# ----------------------------------------------------------------------------- model

class GenPlant(EquipmentModel):
    TYPE = "gen_plant"
    DESCRIPTION = "Natural gas generator plant: paralleled gensets (Modbus TCP), ATSs, load shedding"
    MAX_SUBSTEP = 0.1

    DEFAULTS: Dict[str, Any] = {
        "building_kw_occ": 600.0,        # non-HVAC load (lighting, plugs, elevators)
        "building_kw_unocc": 200.0,
        "building_ats": "ATS-4",
        "ups_ats": "ATS-1",              # where the CHW plant's UPS loads draw from
        "transfer_stagger_s": 5.0,
        "load_demand_delay_min": 10.0,
        "occ_days": "mon-fri",
        "occ_start": 6.0,
        "occ_end": 18.0,
        "clock_start": "",
        "oat": 60.0,
    }
    WEATHER_PARAMS = {"oat": "oa_temp_actual"}
    CONDITIONS = ["oat", "building_kw_occ", "building_kw_unocc"]

    def __init__(self, params: Optional[Dict[str, Any]] = None, layout: Optional[Dict[str, Any]] = None,
                 seed: Optional[int] = None):
        lay = dict(DEFAULT_LAYOUT)
        for k, v in (layout or {}).items():
            if v:
                lay[k] = v
        self.layout = lay
        self.rows = {g: _rows(lay, g) for g in EQUIPMENT_DEFAULTS}
        ids = [int(g["unit_id"]) for g in self.rows["generators"]]
        if len(set(ids)) != len(ids) or MASTER_UNIT in ids:
            raise ValueError(f"generators.csv: unit_id values must be unique and not {MASTER_UNIT} (the master): {ids}")
        self.sensor_ctl = int((lay.get("plant_controller") or (self.rows["ats"][0]["controller_device"]
                                                                if self.rows["ats"] else 243001)))
        self.loads: List[Dict[str, Any]] = []     # {"model", "ats", "name"}
        self.utility_ok = True
        self._build_meta()
        super().__init__(params, seed)

    # ------------------------------------------------------------------ meta

    def _build_meta(self) -> None:
        inputs: List[SignalDef] = []
        signals: List[SignalDef] = []
        faults: List[FaultDef] = []
        actions: List[str] = []
        io: Dict[int, Dict[str, list]] = {}
        info = {
            "oat": ParamInfo("", "OA temp", "Conditions", "°F", minimum=-40, maximum=130),
            "building_kw_occ": ParamInfo("", "Building load occupied", "Conditions", "kW", minimum=0, maximum=3000),
            "building_kw_unocc": ParamInfo("", "Building load unoccupied", "Conditions", "kW", minimum=0, maximum=3000),
        }

        def ctl(dev):
            return io.setdefault(int(dev), {"bo": [], "ao": [], "writes": []})

        def truth(name, units="noUnits", desc="", kind=_A, default=0.0):
            signals.append(SignalDef(name, kind, units, desc, default))

        for g in self.rows["generators"]:
            pre, tag = g["pre"], g["tag"]
            for k, u, d in (("kw", "kilowatts", "kW"), ("kvar", "noUnits", "kVAR"), ("freq", "hertz", "frequency"),
                            ("volts", "volts", "L-L volts"), ("amps", "amperes", "amps"), ("rpm", "revolutionsPerMinute", "speed"),
                            ("coolant", "degreesFahrenheit", "coolant temp"), ("oil", "poundsForcePerSquareInch", "oil pressure"),
                            ("battery", "volts", "battery"), ("hours", "hours", "run hours"), ("starts", "noUnits", "start attempts"),
                            ("pct", "percent", "% of rating"), ("fault_code", "noUnits", "active fault code")):
                truth(f"{pre}.{k}", u, f"{tag} {d}")
            truth(f"{pre}.status", kind="text", desc=f"{tag} status", default="")
            truth(f"{pre}.cb", kind="binary", desc=f"{tag} breaker closed", default=False)
            truth(f"{pre}.switch", kind="text", desc=f"{tag} control switch", default="Auto")
            for k, txt in PCC_FAULTS.items():
                faults.append(FaultDef(f"{pre}_{k}", f"{tag}: {txt}", tag))
            actions += [f"{pre}.{k}" for k in ("off", "auto", "manual", "reset")]

        for a in self.rows["ats"]:
            pre, tag, dev = a["pre"], a["tag"], a["controller_device"]
            inputs.append(SignalDef(f"{pre}_xfer_inhibit", "binary", description=f"{tag} transfer inhibit (ALC output)",
                                    default=False))
            ctl(dev)["bo"].append(f"{pre}_xfer_inhibit")
            for k, d in (("norm_pos", "normal position contact (closed = on normal)"),
                         ("emer_pos", "emergency position contact (closed = on emergency)")):
                signals.append(SignalDef(f"{pre}_{k}", "binary", description=f"{tag} {d}", default=(k == "norm_pos")))
                ctl(dev)["writes"].append((f"{pre}_{k}", f"{pre}_{k}"))
            for k, u, d in (("kw", "kilowatts", "load kW"), ("amps", "amperes", "load amps")):
                truth(f"{pre}.{k}", u, f"{tag} {d}")
            truth(f"{pre}.pos", kind="text", desc=f"{tag} position (N, E, O = open / neutral, B = both)", default="N")
            truth(f"{pre}.live", kind="binary", desc=f"{tag} load energized", default=True)
            truth(f"{pre}.status", kind="text", desc=f"{tag} status", default="")
            faults.append(FaultDef(f"{pre}_fail_to_transfer", f"{tag}: fails to transfer (operator mechanism)", "ATS"))

        for name, desc in PLANT_SENSORS.items():
            signals.append(SignalDef(name, "binary", description=desc, default=(name == "utility_avail")))
            ctl(self.sensor_ctl)["writes"].append((name, name))

        for k, u, d in (("bus_freq", "hertz", "bus frequency"), ("bus_volts", "volts", "bus volts"),
                        ("bus_kw", "kilowatts", "bus load"), ("online_kw", "kilowatts", "online capacity"),
                        ("sets_online", "noUnits", "sets on the bus"), ("shed_level", "noUnits", "load shed level"),
                        ("building_kw", "kilowatts", "building (non-HVAC) load"), ("site_kw", "kilowatts", "site load")):
            truth(k, u, d)
        truth("utility_ok", kind="binary", desc="Utility power present", default=True)
        truth("freq_trend", kind="text", desc="Bus frequency, last minutes", default="")
        truth("sim_clock", kind="text", desc="Simulated date/time", default="")
        truth("layout", kind="text", desc="Plant layout (json)", default="")

        self.INPUTS, self.SIGNALS, self.FAULTS = inputs, signals, faults
        self.PARAM_INFO, self.ACTIONS = info, actions
        self.io = io
        self.io_names: Dict[str, str] = {}
        self.drive_keys: List[str] = []

    def bacnet_devices(self) -> List[Dict[str, Any]]:
        return []                      # the generators are Modbus TCP devices (modbus_units)

    def device_offline(self, pre: str) -> bool:
        return False

    def watch_states(self) -> Dict[str, str]:
        out = {g["tag"]: g["gen"].status_text().split(" · ")[0].split(" (")[0] for g in self.gens}
        out.update({f"{a['tag']} position": {"N": "NORMAL", "E": "EMERGENCY", "O": "OPEN", "B": "BOTH (closed transition)"}[a["pos"]]
                    for a in self.ats})
        out["load shed level"] = str(self.shed_level)
        return out

    def modbus_units(self) -> Dict[int, Any]:
        units: Dict[int, Any] = {int(g["unit_id"]): GenUnit(self, g) for g in self.gens}
        units[MASTER_UNIT] = MasterUnit(self)
        return units

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        self.gens: List[Dict[str, Any]] = []
        self.faults_by: Dict[str, set] = {}
        for g in self.rows["generators"]:
            f: set = set()
            self.faults_by[g["pre"]] = f
            self.gens.append({**g, "gen": Genset(g["tag"], g, f, self.rng), "sync_s": 0.0, "regs": {}, "fts_s": 0.0})
        self.ats: List[Dict[str, Any]] = []
        for a in self.rows["ats"]:
            self.ats.append({**a, "pos": "N", "timer": 0.0, "retx": 0.0, "sync": 0.0, "shed": False,
                             "kw": 0.0, "kvar": 0.0, "kw_shed": 0.0, "status": "On normal"})
        self.bus_f = 0.0
        self.bus_v = 0.0
        self.bus_kw = 0.0
        self.bus_kvar = 0.0
        self._last_bus_kw = 0.0
        self.shed_level = 0
        self._over_s = 0.0
        self._under_s = 0.0
        self._restore_s = 0.0
        self._bus_up_s = 0.0
        self._ld_s = 0.0
        self.emergency = False
        self.trend: List[float] = []
        self._trend_s = 0.0
        self.building_kw = 0.0
        self.kwh = 0.0
        self.master_regs: Dict[int, int] = {}
        start = self.p["clock_start"]
        self.clock0 = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M") if start else _dt.datetime.now().replace(microsecond=0)
        self._occ = False
        self._publish_regs()

    clock = ChwPlant.clock
    _occupied = ChwPlant._occupied

    def set_utility(self, ok: bool) -> None:
        self.utility_ok = bool(ok)

    def action(self, name: str, value: Any = None) -> None:
        pre, key = name.split(".", 1) if "." in name else ("", name)
        g = next((x for x in self.gens if x["pre"] == pre), None)
        if g is None:
            raise KeyError(name)
        gs: Genset = g["gen"]
        if key in ("off", "auto", "manual"):
            gs.set_switch({"off": 0, "auto": 1, "manual": 2}[key])
        elif key == "reset":
            if not gs.fault_reset():
                raise ValueError(f"{g['tag']}: fault reset needs the control switch in Off or Auto")
        else:
            raise KeyError(name)
        self._publish()

    def _sync_inject(self) -> None:
        for g in self.gens:
            f = self.faults_by[g["pre"]]
            f.clear()
            for k in PCC_FAULTS:
                if self.fault(f"{g['pre']}_{k}"):
                    f.add(k)

    def ats_by_tag(self, tag: str) -> Optional[Dict[str, Any]]:
        return next((a for a in self.ats if a["tag"].lower() == str(tag).lower()), None)

    def ats_live(self, a: Dict[str, Any]) -> bool:
        if a["pos"] == "B":
            return True
        if a["pos"] == "N":
            return self.utility_ok
        if a["pos"] == "E":
            return self.bus_live()
        return False

    def bus_live(self) -> bool:
        """Energized (a set's breaker closed) - a sagging bus still feeds its loads."""
        return any(g["gen"].cb_closed for g in self.gens) and self.bus_f > 0.0

    # ------------------------------------------------------------------ step

    def _step(self, h: float) -> None:
        p = self.p
        self._sync_inject()
        now = self.clock()
        self._occ = self._occupied(now)
        u = self.utility_ok

        # ---------------- loads per ATS (from the models as they stand) ----------------
        self.building_kw = p["building_kw_occ"] if self._occ else p["building_kw_unocc"]
        for a in self.ats:
            a["kw"] = a["kvar"] = 0.0
        ups = self.ats_by_tag(p["ups_ats"])
        for x in self.loads:
            a = x["ats"]
            kw, kvar, ups_kw = unit_kw(x["model"], split=True)
            if self.ats_live(a):
                a["kw"] += kw
                a["kvar"] += kvar
            if ups is not None and self.ats_live(ups):
                ups["kw"] += ups_kw                     # the UPS carries these through any gap
                ups["kvar"] += 0.33 * ups_kw
        bld = self.ats_by_tag(p["building_ats"])
        if bld is not None and self.ats_live(bld):
            bld["kw"] += self.building_kw
            bld["kvar"] += 0.48 * self.building_kw

        # ---------------- master: start / stop calls ----------------
        on_emer = any(a["pos"] in ("E", "B") or a["shed"] for a in self.ats)
        if not u:
            self.emergency = True
        elif not on_emer:
            self.emergency = False
        ready = [g for g in self.gens if g["gen"].switch == 1 and not g["gen"].shutdown]
        if self.emergency:
            online = [g for g in self.gens if g["gen"].cb_closed]
            cap_one = max([g["gen"].capacity() for g in self.gens] + [1.0])
            needed = min(len(ready), int(math.ceil(self.bus_kw / (0.9 * cap_one))) + 1) if self.bus_kw > 0 else 2
            self._ld_s = self._ld_s + h if (len(online) > needed and self.shed_level == 0 and u is False) else 0.0
            for g in sorted(self.gens, key=lambda x: int(x["start_priority"])):
                gs = g["gen"]
                if gs.load_demand_stopped:
                    gs.master_call = len([x for x in online if x is not g]) < needed or self.shed_level > 0
                    if gs.master_call:
                        gs.load_demand_stopped = False
                else:
                    gs.master_call = True
            if self._ld_s >= p["load_demand_delay_min"] * 60.0:
                # load demand: stop the highest-priority-number surplus set (keeps N+1)
                spare = sorted(online, key=lambda x: -int(x["start_priority"]))
                if spare:
                    g = spare[0]
                    g["gen"].master_call = False
                    g["gen"].load_demand_stopped = True
                    g["gen"].cb_closed = False
                self._ld_s = 0.0
        else:
            for g in self.gens:
                g["gen"].master_call = False
                g["gen"].load_demand_stopped = False

        # ---------------- gensets ----------------
        for g in self.gens:
            g["gen"].step(h, (p["oat"] - 32.0) * 5.0 / 9.0, self.bus_f if self.bus_live() else None)

        # ---------------- breakers: dead-bus closure, synchronizing ----------------
        dead = not any(g["gen"].cb_closed for g in self.gens)
        for g in sorted(self.gens, key=lambda x: int(x["start_priority"])):
            gs = g["gen"]
            want = gs.at_rated and gs.start_request
            if not want:
                g["sync_s"] = 0.0
                if not gs.at_rated:
                    gs.cb_closed = False
                continue
            if gs.cb_closed:
                continue
            if "cb_fail_close" in gs.faults:
                g["sync_s"] += h
                if g["sync_s"] > 5.0 and 1452 not in gs.active:
                    gs._raise(1452)
                continue
            if dead:
                gs.cb_closed = True                     # first up closes to the dead bus
                dead = False
                self.bus_f, self.bus_v = 60.0, gs.volts_rated
                continue
            if "fail_to_sync" in gs.faults:
                g["sync_s"] += h
                if g["sync_s"] > 60.0 and 1457 not in gs.active:
                    gs._raise(1457)
                continue
            g["sync_s"] += h
            if g["sync_s"] >= g.setdefault("sync_need", 2.0 + 6.0 * self.rng.random()) and self.bus_f > 58.5:
                gs.cb_closed = True
                g["sync_s"] = 0.0
                g.pop("sync_need", None)

        # ---------------- bus: load sharing, frequency / voltage response ----------------
        online = [g for g in self.gens if g["gen"].cb_closed]
        cap = sum(g["gen"].capacity() for g in online)
        e_kw = sum(a["kw"] for a in self.ats if a["pos"] in ("E",) and online)
        e_kvar = sum(a["kvar"] for a in self.ats if a["pos"] in ("E",) and online)
        if online and cap > 0:
            d_kw = e_kw - self._last_bus_kw
            if d_kw > 0:
                self.bus_f -= 14.0 * d_kw / cap             # gas engine: ~3.5 Hz for a 25 % step
            ratio = e_kw / cap
            f_target = 60.0 if ratio <= 1.05 else max(40.0, 60.0 - 25.0 * (ratio - 1.05))
            self.bus_f += (f_target - self.bus_f) * min(1.0, h / 2.5)
            self.bus_f = max(0.0, min(62.0, self.bus_f))
            self.bus_v = 480.0 * max(0.0, 1.0 - 0.012 * max(0.0, 60.0 - self.bus_f))
            for g in online:
                share = g["gen"].capacity() / cap
                g["gen"].kw = e_kw * share
                g["gen"].kvar = e_kvar * share
            self._bus_up_s += h
        else:
            self.bus_f, self.bus_v = 0.0, 0.0
            self._bus_up_s = 0.0
        for g in self.gens:
            if not g["gen"].cb_closed:
                g["gen"].kw = g["gen"].kvar = 0.0
            if g["gen"].kw > 1.0 * g["gen"].capacity():
                if 1444 not in g["gen"].active:
                    g["gen"]._raise(1444)
            else:
                g["gen"]._clear(1444)
        self._last_bus_kw = e_kw
        self.bus_kw, self.bus_kvar = e_kw, e_kvar

        # overload: shed the lowest priority ATS; sustained underfrequency trips the sets
        if online and e_kw > 1.05 * cap:
            self._over_s += h
            if self._over_s >= 3.0:
                victims = [a for a in self.ats if a["pos"] == "E" and int(a["shed_order"]) > 0]
                if victims:
                    a = max(victims, key=lambda x: int(x["shed_order"]))
                    a["pos"], a["shed"], a["kw_shed"] = "O", True, a["kw"]
                    self.shed_level += 1
                self._over_s = 0.0
        else:
            self._over_s = 0.0
        if online and self.bus_f < 55.0:
            self._under_s += h
            if self._under_s >= 10.0:
                for g in online:
                    g["gen"]._raise(1448)
                self._under_s = 0.0
        else:
            self._under_s = 0.0
        # restore shed loads one at a time when there is room
        shed = [a for a in self.ats if a["shed"]]
        if shed and online:
            a = min(shed, key=lambda x: int(x["shed_order"]))
            if cap - e_kw > 1.2 * max(a["kw_shed"], 50.0):
                self._restore_s += h
                if self._restore_s >= 30.0:
                    a["shed"] = False
                    a["pos"] = "E" if not u else "N"
                    self.shed_level = max(0, self.shed_level - 1)
                    self._restore_s = 0.0
            else:
                self._restore_s = 0.0
        elif shed and u:
            for a in shed:
                a["shed"], a["pos"] = False, "N"
            self.shed_level = 0

        # ---------------- ATSs ----------------
        bus_ok = bool(online) and self.bus_f > 57.0 and self.bus_v > 430.0
        for a in sorted(self.ats, key=lambda x: int(x["priority"])):
            pre = a["pre"]
            inhibit = bool(self.inputs[f"{pre}_xfer_inhibit"])
            stuck = self.fault(f"{pre}_fail_to_transfer")
            pos = a["pos"]
            if a["shed"]:
                a["status"] = "Shed by the master (open)"
                continue
            if pos == "N" and not u:
                a["retx"] = 0.0
                if bus_ok and not inhibit and not stuck:
                    a["timer"] += h
                    delay = (int(a["priority"]) - 1) * p["transfer_stagger_s"]
                    if self._bus_up_s >= delay + 0.5 and a["timer"] >= 0.5:
                        a["pos"], a["timer"] = "E", 0.0          # open transition (load was already dark)
                        a["status"] = "Transferred to emergency (open transition)"
                else:
                    a["timer"] = 0.0
                    a["status"] = ("Transfer inhibited by ALC" if inhibit else
                                   "Fails to transfer" if stuck else "Waiting for generator bus")
            elif pos == "E":
                if u and not bus_ok:
                    a["timer"] += h
                    if a["timer"] >= 1.0 and not stuck:
                        a["pos"], a["timer"] = "N", 0.0          # bus lost: back to the live utility (open)
                        a["status"] = "Bus lost - open transfer to normal"
                elif u:
                    if inhibit or stuck:
                        a["status"] = "Retransfer inhibited by ALC" if inhibit else "Fails to transfer"
                    else:
                        a["retx"] += h
                        left = float(a["retransfer_delay_min"]) * 60.0 - a["retx"]
                        if left > 0:
                            a["status"] = f"Utility back - retransfer in {left / 60.0:.1f} min"
                        else:
                            a["sync"] += h
                            a["status"] = "Synchronizing bus to utility (closed transition)"
                            if a["sync"] >= 3.0 and self.bus_f > 59.5:
                                a["pos"], a["timer"] = "B", 0.0
                            elif a["sync"] >= 60.0:
                                a["pos"], a["timer"] = "O", 0.0     # fail to sync: fall back to open transition
                                a["status"] = "Sync failed - open transition"
                else:
                    a["retx"] = a["sync"] = 0.0
                    a["status"] = "On emergency" if bus_ok else "On emergency - bus dead"
            elif pos == "B":
                a["timer"] += h
                if a["timer"] >= 0.1:                           # ≤ 100 ms overlap
                    a["pos"], a["timer"], a["retx"], a["sync"] = "N", 0.0, 0.0, 0.0
                    a["status"] = "Retransferred to normal (closed transition)"
            elif pos == "O":
                a["timer"] += h
                if a["timer"] >= 1.0:
                    a["pos"] = "N" if u else ("E" if bus_ok else "N")
                    a["timer"] = a["retx"] = a["sync"] = 0.0
            if a["pos"] == "N" and u and not a["status"].startswith("Retransferred"):
                a["status"] = "On normal"

        # ---------------- power to the equipment ----------------
        for x in self.loads:
            live = self.ats_live(x["ats"])
            m = x["model"]
            m.set_power(live)
            m.utility_avail = u                         # the plants' phase monitors see the real utility

        # trend
        self._trend_s += h
        if self._trend_s >= 2.0:
            self._trend_s = 0.0
            self.trend.append(round(self.bus_f, 2))
            self.trend = self.trend[-150:]
        self.kwh += e_kw * h / 3600.0

    # ------------------------------------------------------------------ publish

    def _publish(self) -> None:
        if not hasattr(self, "gens"):
            return
        s = self.signals
        for g in self.gens:
            pre, gs = g["pre"], g["gen"]
            pf = gs.kw / math.hypot(gs.kw, gs.kvar) if gs.kw > 1 else 1.0
            g["regs"] = gs.registers(pf)
            s[f"{pre}.kw"] = round(gs.kw if gs.cb_closed else 0.0, 0)
            s[f"{pre}.kvar"] = round(gs.kvar if gs.cb_closed else 0.0, 0)
            s[f"{pre}.freq"] = round(gs.freq, 2)
            s[f"{pre}.volts"] = round(gs.volts, 0)
            s[f"{pre}.amps"] = round(gs.amps, 0)
            s[f"{pre}.rpm"] = round(gs.rpm, 0)
            s[f"{pre}.coolant"] = round(gs.coolant_c * 9 / 5 + 32, 1)
            s[f"{pre}.oil"] = round(gs.oil_kpa * 0.145038, 1)
            s[f"{pre}.battery"] = round(gs.batt_v, 1)
            s[f"{pre}.hours"] = round(gs.run_s / 3600.0, 2)
            s[f"{pre}.starts"] = float(gs.starts)
            s[f"{pre}.pct"] = round(100.0 * gs.kw / gs.kw_rating, 0) if gs.cb_closed else 0.0
            s[f"{pre}.fault_code"] = float(gs.fault_code or (max(gs.active) if gs.active else 0))
            s[f"{pre}.status"] = gs.status_text()
            s[f"{pre}.cb"] = bool(gs.cb_closed)
            s[f"{pre}.switch"] = SWITCH_TEXT[gs.switch]
        for a in self.ats:
            pre = a["pre"]
            s[f"{pre}_norm_pos"] = a["pos"] in ("N", "B")
            s[f"{pre}_emer_pos"] = a["pos"] in ("E", "B")
            s[f"{pre}.kw"] = round(a["kw"], 0)
            s[f"{pre}.amps"] = round(math.hypot(a["kw"], a["kvar"]) * 1000.0 / (math.sqrt(3) * 480.0), 0)
            s[f"{pre}.pos"] = a["pos"]
            s[f"{pre}.live"] = self.ats_live(a)
            s[f"{pre}.status"] = a["status"]
        s["utility_avail"] = bool(self.utility_ok)
        s["gen_running"] = any(g["gen"].running for g in self.gens)
        s["load_shed"] = self.shed_level > 0
        online = [g for g in self.gens if g["gen"].cb_closed]
        s["bus_freq"] = round(self.bus_f, 2)
        s["bus_volts"] = round(self.bus_v, 0)
        s["bus_kw"] = round(self.bus_kw, 0)
        s["online_kw"] = round(sum(g["gen"].capacity() for g in online), 0)
        s["sets_online"] = float(len(online))
        s["shed_level"] = float(self.shed_level)
        s["building_kw"] = round(self.building_kw, 0)
        s["site_kw"] = round(sum(a["kw"] for a in self.ats), 0)
        s["utility_ok"] = bool(self.utility_ok)
        s["freq_trend"] = ",".join(f"{x:.2f}" for x in self.trend)
        s["sim_clock"] = self.clock().strftime("%a %Y-%m-%d %H:%M")
        s["layout"] = json.dumps({
            "generators": [g["pre"] for g in self.gens],
            "ats": [a["pre"] for a in self.ats],
            "tags": {**{g["pre"]: g["tag"] for g in self.gens}, **{a["pre"]: a["tag"] for a in self.ats}},
            "units": {g["pre"]: int(g["unit_id"]) for g in self.gens},
            "ats_desc": {a["pre"]: a.get("description", "") for a in self.ats},
            "feeds": {a["pre"]: [x["name"] for x in self.loads if x["ats"] is a] for a in self.ats},
            "master_unit": MASTER_UNIT,
            "seq_text": SEQ_TEXT,
        })
        self._publish_regs()

    def _publish_regs(self) -> None:
        """DMC 1500 style master register image (A029X159 section 7.1)."""
        r: Dict[int, int] = {}
        online = [g for g in self.gens if g["gen"].cb_closed]
        cap = sum(g["gen"].capacity() for g in online)
        u = self.utility_ok
        bit = lambda pairs: sum(1 << b for b, on in pairs if on)
        r[40001] = bit([(6, not u), (8, not u)])
        r[40002] = bit([(7, bool(online) and self.bus_v < 430.0), (9, bool(online) and not 58.0 <= self.bus_f <= 62.0),
                        (12, any(g["gen"].master_call and g["gen"].shutdown for g in self.gens)),
                        (14, bool(online) and self.bus_kw > cap)])
        r[40003] = bit([(3, any(1457 in g["gen"].active for g in self.gens))])
        r[40004] = 0
        bus_avail = bool(online) and self.bus_f > 57.0
        r[40006] = bit([(0, True), (4, True), (6, bus_avail), (7, u),
                        (8, any(a["pos"] in ("E", "B") for a in self.ats)),
                        (9, u and any(a["pos"] in ("N", "B") for a in self.ats)),
                        (10, any(a["sync"] > 0 for a in self.ats)), (14, self.bus_kw > 1.0)])
        r[40007] = bit([(6, True)])
        v_ll, v_ln = self.bus_v, self.bus_v / math.sqrt(3.0)
        kva = math.hypot(self.bus_kw, self.bus_kvar)
        amps = kva * 1000.0 / (math.sqrt(3.0) * v_ll) if v_ll > 50 else 0.0
        for i in range(3):
            r[40009 + i] = int(round(v_ln))
            r[40013 + i] = int(round(v_ll))
            r[40017 + i] = int(round(amps))
            r[40021 + i] = int(round(self.bus_kw / 3)) & 0xFFFF
            r[40025 + i] = int(round(self.bus_kvar / 3)) & 0xFFFF
            r[40030 + i] = int(round(kva / 3))
        r[40012], r[40016], r[40020] = int(round(v_ln)), int(round(v_ll)), int(round(amps))
        r[40024] = int(round(self.bus_kw)) & 0xFFFF
        r[40028] = int(round(self.bus_kvar)) & 0xFFFF
        r[40029] = int(round(100 * (self.bus_kw / kva if kva > 1 else 1.0)))
        r[40033] = int(round(kva))
        r[40034] = int(round(10 * self.bus_f))
        kwh = int(getattr(self, "kwh", 0.0))
        for reg in range(40035, 40049, 2):
            r[reg], r[reg + 1] = 0, 0
        r[40037], r[40038] = kwh >> 16 & 0xFFFF, kwh & 0xFFFF
        r[40039], r[40040] = kwh >> 16 & 0xFFFF, kwh & 0xFFFF
        avail_a = cap * 1000.0 / (math.sqrt(3.0) * 480.0)
        r[40049] = int(round(avail_a))
        pct_a = 10.0 * 100.0 * amps / avail_a if avail_a > 1 else 0.0
        for i in range(3):
            r[40050 + i] = int(round(pct_a))
            r[40055 + i] = int(round(10.0 * 100.0 * v_ll / 480.0))
        r[40053] = int(round(1000.0 * self.bus_kw / cap)) if cap > 0 else 0
        r[40054] = int(round(1000.0 * self.bus_f / 60.0)) & 0xFFFF
        uv = 480.0 if u else 0.0
        site_kva = sum(math.hypot(a["kw"], a["kvar"]) for a in self.ats if a["pos"] in ("N", "B") and u)
        ua = site_kva * 1000.0 / (math.sqrt(3.0) * 480.0) if u else 0.0
        for i in range(3):
            r[40059 + i] = int(round(uv / math.sqrt(3)))
            r[40063 + i] = int(round(uv))
            r[40067 + i] = int(round(ua))
        r[40062], r[40066], r[40070] = int(round(uv / math.sqrt(3))), int(round(uv)), int(round(ua))
        for n, a in enumerate(self.ats[:10]):
            b = 40257 + 60 * n                                    # ATS 1-5 and 6-10 blocks are contiguous
            pos = a["pos"]
            state = {"N": 1, "E": 2, "B": 3, "O": 0}[pos]
            live = self.ats_live(a)
            kva_l = math.hypot(a["kw"], a["kvar"]) if live else 0.0
            amps_l = kva_l * 1000.0 / (math.sqrt(3.0) * 480.0)
            f_l = 60.0 if (pos in ("N", "B") and u) else (self.bus_f if pos == "E" and live else 0.0)
            v_l = 480.0 if live and pos in ("N", "B") else (self.bus_v if live else 0.0)
            pf_l = a["kw"] / kva_l if kva_l > 1 else 1.0
            regs = [1, 1, state, 0, 0, 0, int(round(a["kw"] if live else 0)),
                    bit([(15, pos in ("N", "B")), (14, pos in ("E", "B")),
                         (12, bool(self.inputs[f"{a['pre']}_xfer_inhibit"]))]), 0]
            regs[5] = int(round(100.0 * amps_l / 1600.0 / 0.5))            # % of a 1600 A switch, ×0.5
            r.update({b + i: v & 0xFFFF for i, v in enumerate(regs)})
            load = [int(round(10 * f_l)), int(round(pf_l / 0.00005)) & 0xFFFF, int(round(kva_l)), int(round(a["kw"] if live else 0)),
                    int(round(a["kvar"] if live else 0)), *[int(round(v_l))] * 3, *[int(round(v_l / math.sqrt(3)))] * 3,
                    *[int(round(amps_l))] * 3, *[regs[5]] * 3]
            r.update({b + 9 + i: v & 0xFFFF for i, v in enumerate(load)})
            src1 = [int(round(600 if u else 0)), int(round(pf_l / 0.00005)) & 0xFFFF if pos in ("N", "B") else 0] + [0] * 15
            src2_live = bool(online)
            src2 = [int(round(10 * self.bus_f)) if src2_live else 0, 0] + [0] * 15
            for i, v in enumerate(src1):
                r[b + 26 + i] = v & 0xFFFF
            for i, v in enumerate(src2):
                r[b + 43 + i] = v & 0xFFFF
            if pos in ("N", "B") and live:
                r[b + 28], r[b + 29] = int(round(kva_l)), int(round(a["kw"]))
                for i in range(3):
                    r[b + 31 + i], r[b + 34 + i], r[b + 37 + i] = 480, int(round(480 / math.sqrt(3))), int(round(amps_l))
            elif pos == "E" and live:
                r[b + 45], r[b + 46] = int(round(kva_l)), int(round(a["kw"]))
                for i in range(3):
                    r[b + 48 + i], r[b + 51 + i], r[b + 54 + i] = int(round(self.bus_v)), int(round(self.bus_v / math.sqrt(3))), int(round(amps_l))
        # spare block (A): load shed level, sets online, capacity, bus load, sets running
        r[40900] = self.shed_level
        r[40901] = len(online)
        r[40902] = int(round(cap))
        r[40903] = int(round(self.bus_kw))
        r[40904] = sum(1 for g in self.gens if g["gen"].running)
        for reg in range(40905, 40910):
            r[reg] = 0
        for reg in range(40001, 40071):
            r.setdefault(reg, 0)                 # 40005, 40008, 40058: not implemented on the DMC, read as 0
        self.master_regs = r


__all__ = ["GenPlant", "unit_kw", "FAULT_CODES"]
