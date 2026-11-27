"""
Chilled water plant (docs/CHILLER_PLANT_DESIGN.md).

  CT-1..n cells ── CW supply hdr ── CWP-1..n (VFD, headered) ── condensers of CH-1..n (iso valve each)
  PCHWP-1..n (VFD, headered) ── evaporators of CH-1..n (iso valve each) ── primary supply hdr
  primary supply hdr ── TES cold hdr / TK-1..n (decoupler) / TES warm hdr ── primary return hdr
  SCHWP-1..n (VFD, on UPS) ── main ── risers (chw_branches) ── coil banks + AHUs ── return

The equipment comes from CSV schedules (one per equipment type), so the
model's inputs, signals and faults are built per instance.  Point names
follow the design review (AHU style): pchwp1_ss, pchwp1_vfd_spd,
pchwp1_amps, ch1_chw_vlv_open, ct2_basin_temp, tk3_temp_04 ...

Hardwired points belong to the ALC controller in each CSV row's
controller_device; the chillers are Mode B devices serving the YORK
OptiView BAS object list (components/york_yk.py).

Hydraulics are solved every step: parallel pumps against the system curve,
the tanks as a low-resistance decoupler (flow = primary - secondary), an
open condenser circuit with static lift to the tower distribution basins.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import psychro
from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef, coerce
from .components.airside import equal_percentage
from .components.hydronic import (
    FT_PER_PSI, IsoValve, Pump, StratifiedTank, bisect, parallel_r, pumps_flow, root, solve_pumps, valve_r,
)
from .components.tower import TowerCell
from .components.drives import build_drive, drive_class, motor_for_hp
from .components.york_yk import YK_FAULTS, YK_OBJECTS, YorkYK

_A = "analog"
_F = "degreesFahrenheit"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# ----- default equipment (the 3-of-each initial build) and CSV column defaults -----

EQUIPMENT_DEFAULTS = {
    "chillers": {"capacity_tons": 1200.0, "chw_design_gpm": 2400.0, "cw_design_gpm": 3600.0,
                 "chws_design_temp": 44.0, "kw_per_ton": 0.57, "min_load_pct": 10.0,
                 "evap_dp_ft": 20.0, "cond_dp_ft": 20.0},
    "primary_pumps": {"design_gpm": 2400.0, "design_head_ft": 60.0, "motor_hp": 50.0, "ct_range_amps": 100.0,
                      "drive_model": "ach580"},
    "secondary_pumps": {"design_gpm": 2400.0, "design_head_ft": 100.0, "motor_hp": 75.0, "ct_range_amps": 150.0,
                        "drive_model": "ach580"},
    "condenser_pumps": {"design_gpm": 3600.0, "design_head_ft": 80.0, "motor_hp": 100.0, "ct_range_amps": 200.0,
                        "drive_model": "ach580"},
    "cooling_towers": {"design_gpm": 3600.0, "range_f": 10.0, "approach_f": 7.0, "design_wb": 78.0,
                       "fan_hp": 60.0, "ct_range_amps": 100.0, "basin_gal": 6500.0, "drive_model": "ach580"},
    "tanks": {"volume_gal": 120000.0, "height_ft": 40.0, "sensor_count": 10},
    "branches": {"load_share_pct": 50.0, "design_remote_dp_psi": 8.0},
}

DEFAULT_LAYOUT: Dict[str, Any] = {
    "chillers": [{"tag": f"CH-{i}", "controller_device": 241001, "sim_device": 4010030 + i, "mac": 30 + i}
                 for i in (1, 2, 3)],
    "primary_pumps": [{"tag": f"PCHWP-{i}", "controller_device": 241001} for i in (1, 2, 3)],
    "secondary_pumps": [{"tag": f"SCHWP-{i}", "controller_device": 241002} for i in (1, 2, 3)],
    "condenser_pumps": [{"tag": f"CWP-{i}", "controller_device": 241003} for i in (1, 2, 3)],
    "cooling_towers": [{"tag": f"CT-{i}", "controller_device": 241003} for i in (1, 2, 3)],
    "tanks": [{"tag": f"TK-{i}", "controller_device": 241004} for i in (1, 2, 3)],
    "branches": [{"tag": "EAST", "load_share_pct": 55.0, "controller_device": 241011},
                 {"tag": "WEST", "load_share_pct": 45.0, "controller_device": 241012}],
}

HEADER_SENSORS = {
    "pchws_temp": ("Primary CHW supply header temp", _F, "primary"),
    "pchwr_temp": ("Primary CHW return header temp", _F, "primary"),
    "pchw_flow": ("Primary CHW total flow (supply header meter)", "usGallonsPerMinute", "primary"),
    "cws_temp": ("Condenser water supply header temp", _F, "primary"),
    "cwr_temp": ("Condenser water return header temp", _F, "primary"),
    "utility_power": ("Utility power status (phase monitor / ATS normal, closed = normal)", None, "primary"),
    "schws_temp": ("Secondary CHW supply temp", _F, "secondary"),
    "schwr_temp": ("Secondary CHW return temp", _F, "secondary"),
    "schw_flow": ("Secondary CHW flow", "usGallonsPerMinute", "secondary"),
    "schw_dp": ("Secondary plant DP (across secondary headers)", "poundsForcePerSquareInch", "secondary"),
    "dcplr_flow": ("Decoupler / TES flow (+ charging, - discharging)", "usGallonsPerMinute", "tes"),
    "tes_cold_temp": ("TES cold header temp", _F, "tes"),
    "tes_warm_temp": ("TES warm header temp", _F, "tes"),
}

PUMP_GROUPS = {"primary_pumps": "Primary CHW pump", "secondary_pumps": "Secondary CHW pump",
               "condenser_pumps": "Condenser water pump"}

# every plant drive is hardwired (drive "hardwired" preset); drive-neutral settings, each drive
# translates them into its own parameters.  CSV column drive_model picks the make / model.
DRIVE_COMMON = {"min_hz": 15.0, "accel_s": 30.0, "decel_s": 30.0}
DRIVE_FAULTS = {"overcurrent_trip": "Drive overcurrent trip", "cooling_fan_failed": "Drive cooling fan failed"}
VALVE_FAULTS = {
    "failed": ("actuator failed", "bool", None, ""),
    "stuck": ("stuck part open", "number", 40.0, "%"),
    "switch": ("end switch out of adjustment (open at 80 %)", "bool", None, ""),
    "leak": ("seat leak (5 % when closed)", "bool", None, ""),
}


def prefix(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(tag).lower())


def _rows(layout: Dict[str, Any], group: str) -> List[Dict[str, Any]]:
    out = []
    for row in layout.get(group) or []:
        r = dict(EQUIPMENT_DEFAULTS[group])
        r.update({k: v for k, v in row.items() if v not in (None, "")})
        r["tag"] = str(r["tag"])
        r["pre"] = prefix(r["tag"])
        out.append(r)
    return out


class ChwPlant(EquipmentModel):
    TYPE = "chw_plant"
    DESCRIPTION = "Chilled water plant: VSD chillers, towers, headered pumps, TES decoupler"
    MAX_SUBSTEP = 0.5

    DEFAULTS: Dict[str, Any] = {
        "peak_tons": 2400.0,             # building peak cooling load
        "chws_design_temp": 44.0,
        "chwr_design_temp": 56.0,
        "main_loss_psi": 15.0,           # secondary main + risers at design flow
        "loop_volume_gal": 30000.0,      # water in the building mains, risers and coils
        "design_speed_hz": 55.0,         # balancer speed: design flow at this drive frequency
        "occ_days": "mon-fri",
        "occ_start": 6.0,
        "occ_end": 18.0,
        "clock_start": "",
        "load_scale": 100.0,             # % of the schedule/weather load
        "oat": 85.0,                     # weather (follows the host weather station)
        "oa_w": 0.0130,
        "t_room": 75.0,                  # chiller room
        "initial_tank_charge": 80.0,     # % charged at start
        "initial_basin_temp": 70.0,
        "sensor_noise": 0.03,
        "pchwp_drive": {}, "schwp_drive": {}, "cwp_drive": {}, "ct_fan_drive": {},
    }
    WEATHER_PARAMS = {"oat": "oa_temp_actual", "oa_w": "oa_w_actual"}
    CONDITIONS = ["oat", "load_scale", "t_room"]

    # ------------------------------------------------------------------ build

    def __init__(self, params: Optional[Dict[str, Any]] = None, layout: Optional[Dict[str, Any]] = None,
                 seed: Optional[int] = None):
        lay = {k: v for k, v in DEFAULT_LAYOUT.items()}
        for k, v in (layout or {}).items():
            if v:
                lay[k] = v
        self.layout = lay
        self.rows = {g: _rows(lay, g) for g in EQUIPMENT_DEFAULTS}
        self.header_ctl = self._header_controllers(lay.get("header_sensors"))
        self.ahus: List[Dict[str, Any]] = []       # AHUs fed from this plant: {"model", "branch"}
        self.utility_ok = True
        self._build_meta()
        super().__init__(params, seed)

    def _header_controllers(self, cfg: Optional[Dict[Any, List[str]]]) -> Dict[str, int]:
        first = lambda g, d: int(self.rows[g][0]["controller_device"]) if self.rows[g] else d
        by_role = {"primary": first("primary_pumps", 241001), "secondary": first("secondary_pumps", 241002),
                   "tes": first("tanks", 241004)}
        out = {name: by_role[role] for name, (_d, _u, role) in HEADER_SENSORS.items()}
        for ctl, names in (cfg or {}).items():
            for n in names:
                if n not in HEADER_SENSORS:
                    raise ValueError(f"header_sensors: unknown sensor {n!r}; known: {list(HEADER_SENSORS)}")
                out[n] = int(ctl)
        return out

    def _build_meta(self) -> None:
        R = self.rows
        inputs: List[SignalDef] = []
        signals: List[SignalDef] = []
        faults: List[FaultDef] = []
        info: Dict[str, ParamInfo] = {
            "oat": ParamInfo("", "OA temp", "Conditions", "°F", minimum=-40, maximum=130),
            "load_scale": ParamInfo("", "Building load", "Conditions", "%", minimum=0, maximum=200),
            "t_room": ParamInfo("", "Chiller room temp", "Conditions", "°F", minimum=40, maximum=110),
        }
        actions: List[str] = []
        io: Dict[int, Dict[str, list]] = {}

        def ctl(dev) -> Dict[str, list]:
            return io.setdefault(int(dev), {"bo": [], "ao": [], "writes": []})

        def bo(dev, name, desc):
            inputs.append(SignalDef(name, "binary", description=desc, default=False))
            ctl(dev)["bo"].append(name)

        def ao(dev, name, desc):
            inputs.append(SignalDef(name, _A, "percent", desc, 0.0))
            ctl(dev)["ao"].append(name)

        def ai(dev, name, units, desc, default=0.0, object_name=None):
            signals.append(SignalDef(name, _A, units, desc, default))
            ctl(dev)["writes"].append((name, object_name or name))

        def bi(dev, name, desc, default=True):
            signals.append(SignalDef(name, "binary", description=desc, default=default))
            ctl(dev)["writes"].append((name, name))

        def truth(name, units="noUnits", desc="", kind=_A, default=0.0):
            signals.append(SignalDef(name, kind, units, desc, default))

        def valve_points(dev, base, label):
            bo(dev, base, f"{label} open command")
            bi(dev, f"{base}_open", f"{label} open end switch (closed = open)", False)
            bi(dev, f"{base}_closed", f"{label} closed end switch (closed = closed)", True)
            truth(f"{base}.pos", "percent", f"{label} position")
            for k, (txt, kind, dflt, units) in VALVE_FAULTS.items():
                faults.append(FaultDef(f"{base}_{k}", f"{label}: {txt}", label.split(" ")[0], kind, dflt, units))

        # chillers: BACnet gateway points + plant-side valves and flow meters
        for c in R["chillers"]:
            pre, dev, tag = c["pre"], c["controller_device"], c["tag"]
            for kind, inst, oname, key, units, role in YK_OBJECTS:
                if role == "cmd":
                    if kind.startswith("binary"):
                        inputs.append(SignalDef(f"{pre}.{key}", "binary", description=oname, default=False))
                    else:
                        dflt = float(c["chws_design_temp"]) if key == "rem_sp" else 100.0
                        inputs.append(SignalDef(f"{pre}.{key}", _A, units, oname, dflt))
                elif kind.startswith("binary"):
                    truth(f"{pre}.{key}", kind="binary", desc=oname, default=False)
                else:
                    truth(f"{pre}.{key}", units or "noUnits", oname)
            valve_points(dev, f"{pre}_chw_vlv", f"{tag} CHW isolation valve")
            valve_points(dev, f"{pre}_cw_vlv", f"{tag} CW isolation valve")
            ai(dev, f"{pre}_chw_flow", "usGallonsPerMinute", f"{tag} evaporator flow meter")
            ai(dev, f"{pre}_cw_flow", "usGallonsPerMinute", f"{tag} condenser flow meter")
            truth(f"{pre}.tons", "tonsRefrigeration", f"{tag} load")
            truth(f"{pre}.kw_per_ton", "noUnits", f"{tag} kW/ton")
            truth(f"{pre}.evap_dp", "poundsForcePerSquareInch", f"{tag} evaporator pressure drop")
            truth(f"{pre}.status", kind="text", desc=f"{tag} panel status", default="")
            for k, txt in YK_FAULTS.items():
                faults.append(FaultDef(f"{pre}_{k}", f"{tag}: {txt}", tag))
            faults.append(FaultDef(f"{pre}_chw_flow_zero", f"{tag} evaporator flow meter reads 0", "Sensors"))
            info[f"{pre}.local_sp"] = ParamInfo("", "Local leaving chilled liquid setpoint", f"{tag} panel", "°F",
                                                minimum=38, maximum=70)
            actions += [f"{pre}.{k}" for k in ("remote", "local", "run", "stop", "reset")]

        # pumps
        for g, label in PUMP_GROUPS.items():
            for r in R[g]:
                pre, dev, tag = r["pre"], r["controller_device"], r["tag"]
                bo(dev, f"{pre}_ss", f"{tag} start/stop")
                ao(dev, f"{pre}_vfd_spd", f"{tag} VFD speed (0-10 V), % of 10 V")
                ai(dev, f"{pre}_amps", "amperes", f"{tag} CT amps (0-{r['ct_range_amps']:.0f} A)")
                ai(dev, f"{pre}_vfd_fb", "percent", f"{tag} VFD speed feedback (AO1, % of 10 V)")
                bi(dev, f"{pre}_vfd_flt", f"{tag} VFD fault (RO3 closed = healthy)")
                truth(f"{pre}.flow", "usGallonsPerMinute", f"{tag} flow")
                truth(f"{pre}.head", "feetOfWater", f"{tag} head")
                truth(f"{pre}.freq", "hertz", f"{tag} output frequency")
                truth(f"{pre}.kw", "kilowatts", f"{tag} power")
                truth(f"{pre}.status", kind="text", desc=f"{tag} drive", default="")
                faults.append(FaultDef(f"{pre}_worn_impeller", f"{tag}: worn impeller (low head)", tag))
                faults.append(FaultDef(f"{pre}_bearing", f"{tag}: bearing / seal failure (overload)", tag))
                for k, txt in DRIVE_FAULTS.items():
                    faults.append(FaultDef(f"{pre}_{k}", f"{tag}: {txt}", tag))
                self._drive_info(info, pre, tag, r["drive_model"])
                actions += [f"{pre}.{k}" for k in ("hand", "off", "auto", "reset", "hand_ref")]

        # cooling towers
        for t in R["cooling_towers"]:
            pre, dev, tag = t["pre"], t["controller_device"], t["tag"]
            bo(dev, f"{pre}_fan_ss", f"{tag} fan start/stop")
            ao(dev, f"{pre}_fan_vfd_spd", f"{tag} fan VFD speed (0-10 V), % of 10 V")
            for v, label in (("inlet", "inlet"), ("outlet", "outlet"), ("bypass", "basin bypass"), ("eq", "equalizer")):
                valve_points(dev, f"{pre}_{v}_vlv", f"{tag} {label} valve")
            bo(dev, f"{pre}_basin_htr", f"{tag} basin heater")
            ai(dev, f"{pre}_fan_amps", "amperes", f"{tag} fan CT amps (0-{t['ct_range_amps']:.0f} A)")
            ai(dev, f"{pre}_fan_vfd_fb", "percent", f"{tag} fan VFD feedback (AO1, % of 10 V)")
            bi(dev, f"{pre}_fan_vfd_flt", f"{tag} fan VFD fault (RO3 closed = healthy)")
            ai(dev, f"{pre}_basin_temp", _F, f"{tag} basin temp", 70.0)
            bi(dev, f"{pre}_level_lo", f"{tag} low level switch (closed = level normal)")
            bi(dev, f"{pre}_level_hi", f"{tag} high level switch (closed = level normal)")
            for k, u, d in (("fill_flow", "usGallonsPerMinute", "flow over the fill"),
                            ("bypass_flow", "usGallonsPerMinute", "flow through the basin bypass"),
                            ("out_flow", "usGallonsPerMinute", "basin outlet flow"),
                            ("level", "inches", "basin level"), ("leaving", _F, "water leaving the fill"),
                            ("ice", "percent", "fill icing"), ("freq", "hertz", "fan frequency"),
                            ("tons", "tonsRefrigeration", "heat rejected"), ("makeup", "usGallonsPerMinute", "makeup")):
                truth(f"{pre}.{k}", u, f"{tag} {d}")
            truth(f"{pre}.vib_tripped", kind="binary", desc=f"{tag} vibration switch tripped (manual reset)", default=False)
            truth(f"{pre}.frozen", kind="binary", desc=f"{tag} basin frozen", default=False)
            truth(f"{pre}.heater_on", kind="binary", desc=f"{tag} basin heater energized", default=False)
            truth(f"{pre}.status", kind="text", desc=f"{tag} fan drive", default="")
            for k, txt, kind, dflt, units in (
                    ("vibration", "fan vibration (trips the vibration switch)", "bool", None, ""),
                    ("nozzles_clogged", "distribution nozzles clogged", "bool", None, ""),
                    ("fill_fouled", "fill fouled", "bool", None, ""),
                    ("makeup_stuck", "makeup valve stuck closed (level drops)", "bool", None, ""),
                    ("heater_failed", "basin heater failed", "bool", None, ""),
                    ("basin_sensor_failed", "basin temp sensor failed", "number", 140.0, "°F"),
                    ("overcurrent_trip", "fan drive overcurrent trip", "bool", None, "")):
                faults.append(FaultDef(f"{pre}_{k}", f"{tag}: {txt}", tag, kind, dflt, units))
            self._drive_info(info, pre, f"{tag} fan", t["drive_model"])
            actions += [f"{pre}.{k}" for k in ("hand", "off", "auto", "reset", "hand_ref", "vib_reset")]

        # tanks
        for k in R["tanks"]:
            pre, dev, tag = k["pre"], k["controller_device"], k["tag"]
            valve_points(dev, f"{pre}_top_vlv", f"{tag} top isolation valve")
            valve_points(dev, f"{pre}_bot_vlv", f"{tag} bottom isolation valve")
            n = int(k["sensor_count"])
            for i in range(n):
                ai(dev, f"{pre}_temp_{i + 1:02d}", _F,
                   f"{tag} temp {i + 1} of {n} ({'top' if i == 0 else 'bottom' if i == n - 1 else 'mid'})", 50.0)
            truth(f"{pre}.flow", "usGallonsPerMinute", f"{tag} flow (+ charging)")
            truth(f"{pre}.charge", "percent", f"{tag} state of charge")
            truth(f"{pre}.ton_hours", "noUnits", f"{tag} ton-hours stored")
            truth(f"{pre}.profile", kind="text", desc=f"{tag} temperature profile", default="")
            faults.append(FaultDef(f"{pre}_sensor_failed", f"{tag}: temp sensor 5 failed", "Sensors", "number", -40.0, "°F"))

        # header sensors
        for name, (desc, units, _role) in HEADER_SENSORS.items():
            dev = self.header_ctl[name]
            if units is None:
                bi(dev, name, desc)
            else:
                ai(dev, name, units, desc, 50.0 if units == _F else 0.0)
                if name in ("pchw_flow", "schw_flow", "dcplr_flow"):
                    faults.append(FaultDef(f"{name}_zero", f"{desc}: meter reads 0", "Sensors"))
                    faults.append(FaultDef(f"{name}_drift", f"{desc}: meter drift", "Sensors", "number", 15.0, "%"))
                else:
                    faults.append(FaultDef(f"{name}_drift", f"{desc}: sensor drift", "Sensors", "number", 3.0,
                                           "°F" if units == _F else "psi"))
        faults.append(FaultDef("dcplr_flow_reversed", "Decoupler flow meter electrode fault (sign reversed)", "Sensors"))

        # building branches / remote DP
        for b in R["branches"]:
            pre, dev, tag = b["pre"], b["controller_device"], b["tag"]
            ai(dev, f"{pre}_chw_remote_dp", "poundsForcePerSquareInch", f"{tag} riser remote DP",
               object_name="chw_remote_dp")
            for k, u, d in (("load", "tonsRefrigeration", "load"), ("met", "tonsRefrigeration", "load met"),
                            ("flow", "usGallonsPerMinute", "riser flow"), ("valve", "percent", "coil valves (aggregate)"),
                            ("rwt", _F, "riser return temp"), ("dp", "poundsForcePerSquareInch", "remote DP (true)"),
                            ("ahu_flow", "usGallonsPerMinute", "simulated AHU coil flow")):
                truth(f"{pre}.{k}", u, f"{tag} {d}")
            faults.append(FaultDef(f"{pre}_bypass_open", f"{tag} riser: bypass stuck open", "Building"))
            faults.append(FaultDef(f"{pre}_dp_tube_leak", f"{tag} remote DP sensor tubing leak (reads low)", "Sensors"))
        faults.append(FaultDef("low_dt", "Low delta-T syndrome (fouled coils, oversized valves)", "Building"))
        faults.append(FaultDef("load_spike", "Building load spike", "Building", "number", 30.0, "%"))

        # plant totals
        for k, u, d in (("primary_flow", "usGallonsPerMinute", "primary flow"),
                        ("secondary_flow", "usGallonsPerMinute", "secondary flow"),
                        ("tes_flow", "usGallonsPerMinute", "decoupler flow (+ charging)"),
                        ("cw_flow", "usGallonsPerMinute", "condenser water flow"),
                        ("building_tons", "tonsRefrigeration", "building load"),
                        ("plant_tons", "tonsRefrigeration", "chiller output"),
                        ("plant_kw", "kilowatts", "plant power"),
                        ("tes_charge", "percent", "TES state of charge"),
                        ("tes_ton_hours", "noUnits", "TES ton-hours"),
                        ("schws_actual", _F, "true secondary supply"), ("pchws_actual", _F, "true primary supply"),
                        ("cws_actual", _F, "true CW supply"), ("plant_dp", "poundsForcePerSquareInch", "true plant DP"),
                        ("oa_wb", _F, "OA wet bulb in use"), ("oat", _F, "OA temp in use")):
            truth(k, u, d)
        truth("occupied", kind="binary", desc="Building occupied (schedule)", default=False)
        truth("utility_ok", kind="binary", desc="Utility power present", default=True)
        truth("sim_clock", kind="text", desc="Simulated date/time", default="")
        truth("layout", kind="text", desc="Plant layout (json)", default="")

        self.INPUTS, self.SIGNALS, self.FAULTS = inputs, signals, faults
        self.PARAM_INFO, self.ACTIONS = info, actions
        self.io = io
        self.drive_keys = [r["pre"] for g in PUMP_GROUPS for r in R[g]] + [t["pre"] for t in R["cooling_towers"]]

    # ------------------------------------------------------------------ BACnet devices (Mode B)

    def bacnet_devices(self) -> List[Dict[str, Any]]:
        """The chillers' OptiView BAS gateways: one device each (used by plant_runner)."""
        from .components.york_yk import state_texts
        out = []
        for c in self.chillers:
            pre = c["pre"]
            cmds, mons = [], []
            for kind, n, oname, key, units, role in YK_OBJECTS:
                obj = f"{kind},{n}"
                if role == "cmd":
                    d = {"object": obj, "name": oname, "input": f"{pre}.{key}", "description": oname}
                    if kind.startswith("binary"):
                        d.update(active_text="RUN", inactive_text="STOP")
                    else:
                        d["units"] = units
                    cmds.append(d)
                elif isinstance(role, tuple):
                    _tag, table, count = role
                    mons.append({"object": obj, "name": oname, "signal": f"{pre}.{key}", "states": state_texts(table, count)})
                else:
                    d = {"object": obj, "name": oname, "signal": f"{pre}.{key}"}
                    if units:
                        d["units"] = units
                    mons.append(d)
            out.append({"tag": c["tag"], "pre": pre, "sim_device": int(c["sim_device"]), "mac": int(c["mac"]),
                        "model_name": "YORK YK OptiView (simulated)",
                        "description": c.get("description", "YORK YK VSD centrifugal chiller"),
                        "kind": "chiller", "commands": cmds, "monitors": mons})
        return out

    def device_offline(self, pre: str) -> bool:
        return (not self.utility_ok) or self.fault(f"{pre}_gateway_offline")

    def watch_states(self) -> Dict[str, str]:
        return {c["tag"]: self.signals[f"{c['pre']}.status"].split(" · ")[0] for c in self.chillers}

    @staticmethod
    def _drive_info(info: Dict[str, ParamInfo], pre: str, tag: str, model: str = "ach580") -> None:
        cls = drive_class(model)
        for k, i in cls.PARAM_INFO.items():
            info[f"{pre}.{k}"] = ParamInfo(i.id, i.label, f"{tag} drive ({cls.SHORT})", i.units, i.choices, i.minimum, i.maximum)

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        p = self.p
        R = self.rows
        s_d = p["design_speed_hz"] / 60.0
        self.faults_by: Dict[str, set] = {}

        def hdr_head(design_gpm, head, branch_ft=10.0):
            return 1.25 * head * s_d * s_d - 0.25 * head - branch_ft

        # drives and pumps
        self.drives: Dict[str, Vfd] = {}
        self.drive_p: Dict[str, Dict[str, Any]] = {}
        self.inject: Dict[str, set] = {}
        self.pumps: Dict[str, List[Dict[str, Any]]] = {}
        group_drive = {"primary_pumps": "pchwp_drive", "secondary_pumps": "schwp_drive", "condenser_pumps": "cwp_drive"}
        for g in PUMP_GROUPS:
            self.pumps[g] = []
            for r in R[g]:
                pump = Pump(float(r["design_gpm"]), float(r["design_head_ft"]), float(r["motor_hp"]),
                            branch_r=10.0 / float(r["design_gpm"]) ** 2)
                self._make_drive(r["pre"], float(r["motor_hp"]), 1.2, p[group_drive[g]], r["drive_model"])
                self.pumps[g].append({**r, "pump": pump})
        # chillers and their branches
        self.chillers: List[Dict[str, Any]] = []
        prim = self.pumps["primary_pumps"]
        q_tot = sum(float(c["chw_design_gpm"]) for c in R["chillers"]) or 1.0
        self.rc_p = 2.0 / q_tot ** 2                     # primary headers + TES path, 2 ft at full flow
        cw_tot = sum(float(c["cw_design_gpm"]) for c in R["chillers"]) or 1.0
        self.rc_c = 2.0 / cw_tot ** 2
        p_hd = prim[0] if prim else {"design_gpm": 2400.0, "design_head_ft": 60.0}
        c_hd = self.pumps["condenser_pumps"][0] if self.pumps["condenser_pumps"] else {"design_gpm": 3600.0, "design_head_ft": 80.0}
        for c in R["chillers"]:
            f = set()
            self.faults_by[c["pre"]] = f
            qd, qc = float(c["chw_design_gpm"]), float(c["cw_design_gpm"])
            h_br = hdr_head(qd, float(p_hd["design_head_ft"])) - self.rc_p * qd * qd
            pipe_e = max(1.0, h_br - float(c["evap_dp_ft"]) - 1.5)
            # condenser branch: header head minus tower static lift + nozzles + suction
            h_cb = hdr_head(qc, float(c_hd["design_head_ft"])) - self.rc_c * qc * qc - 12.0 - 5.0 - 2.0
            pipe_c = max(1.0, h_cb - float(c["cond_dp_ft"]) - 1.5)
            self.chillers.append({
                **c, "yk": YorkYK(c["tag"], c, f),
                "vchw": IsoValve(60.0), "vcw": IsoValve(60.0),
                "r_evap": (float(c["evap_dp_ft"]) + pipe_e) / qd ** 2, "r_vchw": 1.5 / qd ** 2,
                "r_cond": (float(c["cond_dp_ft"]) + pipe_c) / qc ** 2, "r_vcw": 1.5 / qc ** 2,
                "q_e": 0.0, "q_c": 0.0,
            })
        # towers
        self.towers: List[Dict[str, Any]] = []
        for t in R["cooling_towers"]:
            cell = TowerCell(float(t["design_gpm"]), float(t["range_f"]), float(t["approach_f"]),
                             float(t["design_wb"]), float(t["basin_gal"]), basin_t=p["initial_basin_temp"])
            self._make_drive(t["pre"], float(t["fan_hp"]), 8.0, p["ct_fan_drive"], t["drive_model"])
            qd = float(t["design_gpm"])
            self.towers.append({**t, "cell": cell, "vin": IsoValve(60.0), "vout": IsoValve(60.0),
                                "vbp": IsoValve(60.0, fail_closed=True), "veq": IsoValve(60.0),
                                "r_in": 5.0 / qd ** 2, "r_bp": 3.0 / qd ** 2, "r_out": 2.0 / qd ** 2,
                                "vib": False, "heater_on": False})
        # tanks
        self.tanks: List[Dict[str, Any]] = []
        q_tank = q_tot / max(1, len(R["tanks"]))
        for k in R["tanks"]:
            tank = StratifiedTank(float(k["volume_gal"]), float(k["height_ft"]),
                                  t_init_cold=p["chws_design_temp"], t_init_warm=p["chwr_design_temp"],
                                  charge=p["initial_tank_charge"] / 100.0)
            self.tanks.append({**k, "tank": tank, "vtop": IsoValve(60.0), "vbot": IsoValve(60.0),
                               "r": 1.0 / q_tank ** 2, "q": 0.0})
        # building branches
        self.branches: List[Dict[str, Any]] = []
        peak = p["peak_tons"]
        total_gpm = peak * 24.0 / max(1.0, p["chwr_design_temp"] - p["chws_design_temp"])
        self.r_main = p["main_loss_psi"] * FT_PER_PSI / total_gpm ** 2
        sec = self.pumps["secondary_pumps"]
        s_hd = sec[0] if sec else {"design_gpm": 2400.0, "design_head_ft": 100.0}
        plant_dp_d = hdr_head(float(s_hd["design_gpm"]), float(s_hd["design_head_ft"]))
        shares = sum(float(b["load_share_pct"]) for b in R["branches"]) or 1.0
        for b in R["branches"]:
            frac = float(b["load_share_pct"]) / shares
            qd = total_gpm * frac
            dp_d = float(b["design_remote_dp_psi"]) * FT_PER_PSI
            riser_ft = max(2.0, plant_dp_d - p["main_loss_psi"] * FT_PER_PSI - dp_d)
            self.branches.append({**b, "frac": frac, "q_d": qd, "dp_d": dp_d, "r_riser": riser_ft / qd ** 2,
                                  "c_open": 1.3 * qd / math.sqrt(dp_d), "pos": 0.6, "q": 0.0, "q_bank": 0.0,
                                  "q_ahu": 0.0, "q_bp": 0.0, "dp": dp_d, "load": 0.0, "met": 0.0,
                                  "rwt": p["chwr_design_temp"], "lwt": p["chwr_design_temp"]})
        # coil bank calibration (counterflow effectiveness at the design point, EAT 78 -> LAT 55)
        self.eat, self.lat = 78.0, 55.0
        dT = p["chwr_design_temp"] - p["chws_design_temp"]
        c_air, c_w = 1.0 / (self.eat - self.lat), 1.0 / dT
        cmin, cmax = min(c_air, c_w), max(c_air, c_w)
        eps_d = 1.0 / cmin / (self.eat - p["chws_design_temp"])
        cr = cmin / cmax
        self.ntu_d = bisect(lambda n: eps_d - self._eps(n, cr), 0.01, 20.0)
        # plant temperatures and flows
        t0 = p["t_room"]
        self.t_ps = self.t_r = self.t_s = p["chws_design_temp"] + 4.0
        self.t_br = p["chwr_design_temp"]
        self.t_cwr = self.t_cws = p["initial_basin_temp"]
        self.qp = self.qs = self.qt = self.qc = 0.0
        self.dp_tank = 0.0
        self.h_sec = 0.0
        self.plant_dp_ft = 0.0
        self.sens: Dict[str, float] = {}
        start = p["clock_start"]
        self.clock0 = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M") if start else _dt.datetime.now().replace(microsecond=0)
        self._occ = False
        self._t0 = t0

    def _make_drive(self, pre: str, hp: float, inertia: float, overrides: Dict[str, Any],
                    model: str = "ach580") -> None:
        """
        A hardwired plant drive.  overrides: the group's drive parameters, either flat (in the
        drive's own parameter names) or per model, e.g. {"ach580": {...}, "z1000": {...}}.
        """
        model = str(model or "ach580").lower()
        ov = overrides or {}
        if model in ov and isinstance(ov[model], dict):
            ov = ov[model]
        elif any(isinstance(v, dict) for v in ov.values()):
            ov = {}                                  # per-model overrides, none for this model
        self.inject[pre] = set()
        v = build_drive(model, {**DRIVE_COMMON, **motor_for_hp(hp, inertia)}, "hardwired", ov, self.inject[pre])
        self.drive_p[pre] = v.p
        self.drives[pre] = v

    @staticmethod
    def _eps(ntu: float, cr: float) -> float:
        if abs(1.0 - cr) < 1e-6:
            return ntu / (1.0 + ntu)
        e = math.exp(-ntu * (1.0 - cr))
        return (1.0 - e) / (1.0 - cr * e)

    # ------------------------------------------------------------- parameters

    def param_items(self) -> List[tuple]:
        items = []
        for k, i in self.PARAM_INFO.items():
            items.append((k, i, self._param_value(k)))
        return items

    def _param_value(self, name: str) -> Any:
        if "." in name:
            pre, k = name.split(".", 1)
            if pre in self.drive_p:
                return self.drive_p[pre][k]
            ch = self._chiller(pre)
            if ch and k == "local_sp":
                return ch["yk"].local_sp
            raise KeyError(name)
        return self.p[name]

    def param_label(self, name: str) -> str:
        info = self.PARAM_INFO.get(name)
        if info is None:
            return name
        pre = name.split(".", 1)[0] if "." in name else ""
        return f"{pre.upper()} {info.id} {info.label}".strip() if pre else super().param_label(name)

    def set_param(self, name: str, value: Any) -> None:
        if "." in name:
            pre, k = name.split(".", 1)
            if pre in self.drive_p and k in type(self.drives[pre]).DEFAULTS:
                self.drive_p[pre][k] = type(self.drives[pre]).check_param(k, value)
                return
            ch = self._chiller(pre)
            if ch and k == "local_sp":
                ch["yk"].local_sp = max(38.0, min(70.0, float(value)))
                return
            raise KeyError(f"no parameter {name!r}")
        super().set_param(name, value)

    def _chiller(self, pre: str) -> Optional[Dict[str, Any]]:
        return next((c for c in getattr(self, "chillers", []) if c["pre"] == pre), None)

    def action(self, name: str, value: Any = None) -> None:
        if "." in name:
            pre, key = name.split(".", 1)
            ch = self._chiller(pre)
            if ch:
                ch["yk"].panel(key)
            elif key == "vib_reset":
                t = next((t for t in self.towers if t["pre"] == pre), None)
                if t is None:
                    raise KeyError(name)
                if self.fault(f"{pre}_vibration"):
                    raise ValueError(f"{t['tag']}: fan is still vibrating - the switch won't reset")
                t["vib"] = False
            elif pre in self.drives:
                self._sync_inject()
                self.drives[pre].keypad(key, value)
            else:
                raise KeyError(name)
        else:
            raise KeyError(name)
        self._publish()

    def set_utility(self, ok: bool) -> None:
        self.utility_ok = bool(ok)

    # ------------------------------------------------------------- helpers

    def clock(self) -> _dt.datetime:
        return self.clock0 + _dt.timedelta(seconds=self.sim_time)

    def _occupied(self, now: _dt.datetime) -> bool:
        days = str(self.p["occ_days"]).lower()
        if "-" in days:
            a, b = days.split("-")
            allowed = WEEKDAYS[WEEKDAYS.index(a):WEEKDAYS.index(b) + 1]
        else:
            allowed = [x.strip() for x in days.split(",")]
        hour = now.hour + now.minute / 60.0
        return WEEKDAYS[now.weekday()] in allowed and self.p["occ_start"] <= hour < self.p["occ_end"]

    def _sync_inject(self) -> None:
        for pre in self.drives:
            inj = self.inject[pre]
            inj.clear()
            for k in DRIVE_FAULTS:
                if f"{pre}_{k}" in self.faults and self.fault(f"{pre}_{k}"):
                    inj.add(k)
        for c in self.chillers:
            f = self.faults_by[c["pre"]]
            f.clear()
            for k in YK_FAULTS:
                if self.fault(f"{c['pre']}_{k}"):
                    f.add(k)

    def _valve(self, h: float, v: IsoValve, base: str, powered: bool) -> float:
        stuck = self.fault_value(f"{base}_stuck") / 100.0 if self.fault(f"{base}_stuck") else None
        v.step(h, bool(self.inputs[base]), powered, self.fault(f"{base}_failed"), stuck)
        return v.fraction(self.fault(f"{base}_leak"))

    def building_fraction(self, now: _dt.datetime) -> float:
        """Building cooling load as a fraction of peak: people/lights, envelope, ventilation, sun."""
        p = self.p
        hour = now.hour + now.minute / 60.0
        occ = self._occ
        internal = 0.45 if occ else 0.10
        envelope = 0.30 * max(-0.2, min(1.3, (p["oat"] - 55.0) / 40.0))
        vent = (0.10 if occ else 0.02) * max(0.0, min(1.5, (p["oa_w"] - 0.006) / 0.010))
        sun = 0.15 * max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
        f = max(0.03, internal + envelope + vent + sun) * p["load_scale"] / 100.0
        if self.fault("load_spike"):
            f *= 1.0 + self.fault_value("load_spike") / 100.0
        return f

    def ahu_tons(self, branch: Dict[str, Any]) -> float:
        return sum(a["model"].cc.gpm_d / 2.0 for a in self.ahus if a["branch"] is branch)

    # ------------------------------------------------------------- hydraulics

    def _solve_chw(self) -> None:
        prim = [x["pump"] for x in self.pumps["primary_pumps"]]
        sec = [x["pump"] for x in self.pumps["secondary_pumps"]]
        # primary: chiller branches in parallel + common headers
        for c in self.chillers:
            c["r_e"] = c["r_evap"] + valve_r(c["r_vchw"], c["f_chw"]) if c["f_chw"] > 1e-4 else float("inf")
        r_par = parallel_r(c["r_e"] for c in self.chillers)
        rt_p = r_par + self.rc_p
        # building: main + parallel risers, each = riser pipe + far-end coil bank / AHUs / bypass
        for b in self.branches:
            c_bank = b["c_open"] * equal_percentage(b["pos"]) * (1.3 if self.fault("low_dt") else 1.0)
            c_ahu = b["q_ahu"] / math.sqrt(max(b["dp"], 1.0))
            c_bp = 0.25 * b["c_open"] if self.fault(f"{b['pre']}_bypass_open") else 0.0
            b["c_bank"], b["c_ahu"], b["c_bp"] = c_bank, c_ahu, c_bp
            c_tot = c_bank + c_ahu + c_bp
            b["r_end"] = 1.0 / c_tot ** 2 if c_tot > 1e-6 else float("inf")
            b["r_b"] = b["r_riser"] + b["r_end"]
        r_bld = self.r_main + parallel_r(b["r_b"] for b in self.branches)
        # tanks (both valves in series with the tank)
        for k in self.tanks:
            f = min(k["f_top"], k["f_bot"])
            k["r_k"] = k["r"] / f ** 2 if f > 1e-3 else float("inf")
        r_tes = parallel_r(k["r_k"] for k in self.tanks)

        def prim_flow(dp):
            return solve_pumps(prim, dp, rt_p)

        def sec_flow(dp):
            return solve_pumps(sec, -dp, r_bld)

        if r_tes < 1e12:
            # decoupler: tank flow qt sets the pressure difference across the tanks
            def resid(qt):
                dp = r_tes * qt * abs(qt)
                return prim_flow(dp)[0] - sec_flow(dp)[0] - qt

            lo, hi = self.qt - 300.0, self.qt + 300.0
            if resid(lo) < 0 or resid(hi) > 0:
                qp0, qs0 = prim_flow(0.0)[0], sec_flow(0.0)[0]
                lo, hi = -qs0 - 10.0, qp0 + 10.0
            qt = root(resid, lo, hi, xtol=0.05)
            dp = r_tes * qt * abs(qt)
            qp, _hp = prim_flow(dp)
            qs, hs = sec_flow(dp)
            qt = qp - qs
        else:
            # no decoupler: primary and secondary in series
            hmax = max([x.shutoff for x in prim + sec] + [1.0])
            dp = root(lambda d: prim_flow(d)[0] - sec_flow(d)[0], -hmax, hmax, xtol=0.01)
            qp, _hp = prim_flow(dp)
            qs, hs = sec_flow(dp)
            qs = qp = 0.5 * (qp + qs)
            qt = 0.0
        self.dp_tank = dp
        self.qp, self.qs, self.qt = qp, qs, qt
        self.h_sec = hs
        # split primary among chillers
        cs = [1.0 / math.sqrt(c["r_e"]) if c["r_e"] < 1e12 else 0.0 for c in self.chillers]
        ctot = sum(cs) or 1.0
        for c, ci in zip(self.chillers, cs):
            c["q_e"] = qp * ci / ctot
        # tanks
        ks = [1.0 / math.sqrt(k["r_k"]) if k["r_k"] < 1e12 else 0.0 for k in self.tanks]
        ktot = sum(ks) or 1.0
        for k, ki in zip(self.tanks, ks):
            k["q"] = qt * ki / ktot
        # building
        self.plant_dp_ft = max(0.0, hs + dp) if qs > 0 else max(0.0, hs + dp) * 0.0
        h_after = max(0.0, hs + dp - self.r_main * qs * qs)
        for b in self.branches:
            qb = math.sqrt(h_after / b["r_b"]) if b["r_b"] < 1e12 and qs > 0 else 0.0
            b["q"] = qb
            b["dp"] = b["r_end"] * qb * qb if b["r_end"] < 1e12 else h_after
            ct = b["c_bank"] + b["c_ahu"] + b["c_bp"]
            b["q_bank"] = qb * b["c_bank"] / ct if ct > 0 else 0.0
            b["q_bp"] = qb * b["c_bp"] / ct if ct > 0 else 0.0
            b["q_ahu_hyd"] = qb * b["c_ahu"] / ct if ct > 0 else 0.0

    def _solve_cw(self) -> None:
        cwp = [x["pump"] for x in self.pumps["condenser_pumps"]]
        for c in self.chillers:
            c["r_cw"] = c["r_cond"] + valve_r(c["r_vcw"], c["f_cw"]) if c["f_cw"] > 1e-4 else float("inf")
        r_cb = parallel_r(c["r_cw"] for c in self.chillers)
        c_out, c_out_full = [], 0.0
        for t in self.towers:
            cell = t["cell"]
            base = 1.0 / math.sqrt(valve_r(t["r_out"], t["f_out"])) if t["f_out"] > 1e-3 else 0.0
            lvl = 0.0 if cell.frozen else max(0.0, min(1.0, cell.level_in / 4.0))
            c_out.append(base * lvl)
            c_out_full += base
        c_tot = sum(c_out)
        for x in cwp:
            x.cavitation = 0.0 if c_out_full <= 0 else max(0.0, 1.0 - c_tot / c_out_full)
        if r_cb >= 1e12 or c_tot <= 1e-9 or max_speed(cwp) < 0.05:
            q = 0.0
            for x in cwp:
                x.flow, x.head = 0.0, x.shutoff * x.speed * x.speed
            for t in self.towers:
                t["q_in"] = t["q_bp"] = t["q_out"] = 0.0
            for c in self.chillers:
                c["q_c"] = 0.0
            self.qc = 0.0
            return
        r_suct = 1.0 / c_tot ** 2
        paths = []
        for t in self.towers:
            r_in = valve_r(t["r_in"], t["f_in"]) if t["f_in"] > 1e-3 else float("inf")
            r_bp = valve_r(t["r_bp"], t["f_bp"]) if t["f_bp"] > 1e-3 else float("inf")
            paths.append((t, r_in, r_bp))

        def q_paths(hh):
            q = 0.0
            for _t, r_in, r_bp in paths:
                if r_in < 1e12 and hh > 12.0:
                    q += math.sqrt((hh - 12.0) / r_in)
                if r_bp < 1e12 and hh > 2.0:
                    q += math.sqrt((hh - 2.0) / r_bp)
            return q

        r_series = r_cb + self.rc_c + r_suct

        def resid(hh):
            q = q_paths(hh)
            return pumps_flow(cwp, hh + r_series * q * q) - q

        hmax = max(x.shutoff for x in cwp) + 15.0
        hh = root(resid, 0.0, hmax, xtol=1e-3)
        q = q_paths(hh)
        head = hh + r_series * q * q
        for x in cwp:
            x.flow = x.flow_at(head)
            x.head = x.pump_head(x.flow)
        self.qc = q
        for t, r_in, r_bp in paths:
            t["q_in"] = math.sqrt((hh - 12.0) / r_in) if r_in < 1e12 and hh > 12.0 else 0.0
            t["q_bp"] = math.sqrt((hh - 2.0) / r_bp) if r_bp < 1e12 and hh > 2.0 else 0.0
        for t, co in zip(self.towers, c_out):
            t["q_out"] = q * co / c_tot
        cs = [1.0 / math.sqrt(c["r_cw"]) if c["r_cw"] < 1e12 else 0.0 for c in self.chillers]
        ctot = sum(cs) or 1.0
        for c, ci in zip(self.chillers, cs):
            c["q_c"] = q * ci / ctot

    # ------------------------------------------------------------- step

    def _step(self, h: float) -> None:
        p = self.p
        now = self.clock()
        self._occ = self._occupied(now)
        self._sync_inject()
        util = self.utility_ok
        pa = psychro.p_atm(0)
        key = (round(p["oat"], 2), round(p["oa_w"], 5))
        if getattr(self, "_wb_key", None) != key:
            self._wb_key = key
            self.wb = psychro.wetbulb(p["oat"], p["oa_w"], pa)

        # ---------------- valves ----------------
        for c in self.chillers:
            c["f_chw"] = self._valve(h, c["vchw"], f"{c['pre']}_chw_vlv", util)
            c["f_cw"] = self._valve(h, c["vcw"], f"{c['pre']}_cw_vlv", util)
        for t in self.towers:
            pre = t["pre"]
            t["f_in"] = self._valve(h, t["vin"], f"{pre}_inlet_vlv", util)
            t["f_out"] = self._valve(h, t["vout"], f"{pre}_outlet_vlv", util)
            t["f_bp"] = self._valve(h, t["vbp"], f"{pre}_bypass_vlv", util)
            t["f_eq"] = self._valve(h, t["veq"], f"{pre}_eq_vlv", util)
        for k in self.tanks:                              # tank actuators are on the UPS
            k["f_top"] = self._valve(h, k["vtop"], f"{k['pre']}_top_vlv", True)
            k["f_bot"] = self._valve(h, k["vbot"], f"{k['pre']}_bot_vlv", True)

        # ---------------- drives ----------------
        for g, items in self.pumps.items():
            on_ups = g == "secondary_pumps"
            for x in items:
                pre, pump = x["pre"], x["pump"]
                v = self.drives[pre]
                v.powered = util or on_ups
                v.wire(start=bool(self.inputs[f"{pre}_ss"]), safeties=True, speed_ref=float(self.inputs[f"{pre}_vfd_spd"]))
                bearing = 1.25 if self.fault(f"{pre}_bearing") else 1.0
                v.step(h, lambda s, pump=pump, b=bearing: pump.load_torque_pu(s) * b)
                pump.wear = 0.82 if self.fault(f"{pre}_worn_impeller") else 1.0
                pump.speed = v.speed_pu
        for t in self.towers:
            pre = t["pre"]
            v = self.drives[pre]
            if self.fault(f"{pre}_vibration") and v.speed_pu > 0.2:
                t["vib"] = True
            v.powered = util
            v.wire(start=bool(self.inputs[f"{pre}_fan_ss"]), safeties=not t["vib"],     # vibration switch in the safeties
                   speed_ref=float(self.inputs[f"{pre}_fan_vfd_spd"]))
            ice = t["cell"].ice
            v.step(h, lambda s, ice=ice: 0.03 + 0.9 * s * s * (1.0 + 0.3 * ice))

        # ---------------- hydraulics ----------------
        self._solve_chw()
        self._solve_cw()

        # ---------------- chilled water temperatures ----------------
        qp, qs, qt = self.qp, self.qs, self.qt
        tops = [(k["q"], k["tank"].top) for k in self.tanks if k["q"] > 0]
        bots = [(-k["q"], k["tank"].bottom) for k in self.tanks if k["q"] < 0]
        if qp > 1.0:
            num = qs * self.t_br + sum(q * t for q, t in tops)
            den = qs + sum(q for q, _ in tops)
            t_r = num / den if den > 1.0 else self.t_r
        else:
            t_r = self.t_r + (p["t_room"] - self.t_r) * min(1.0, h / 3600.0)
        self.t_r = t_r
        for c in self.chillers:
            yk = c["yk"]
            yk.powered = util
            yk.rem_run = bool(self.inputs[f"{c['pre']}.rem_run"])
            yk.rem_sp = float(self.inputs[f"{c['pre']}.rem_sp"])
            yk.rem_cur_limit = float(self.inputs[f"{c['pre']}.rem_cur_limit"])
            yk.step(h, c["q_e"], c["q_c"], t_r, self.t_cws, p["t_room"])
        q_e_tot = sum(c["q_e"] for c in self.chillers)
        if q_e_tot > 1.0:
            t_ps = sum(c["q_e"] * c["yk"].lchwt for c in self.chillers) / q_e_tot
        else:
            t_ps = self.t_ps + (p["t_room"] - self.t_ps) * min(1.0, h / 3600.0)
        self.t_ps = t_ps
        num = qp * t_ps + sum(q * t for q, t in bots)
        den = qp + sum(q for q, _ in bots)
        self.t_s = num / den if den > 1.0 else self.t_s + (p["t_room"] - self.t_s) * min(1.0, h / 3600.0)
        for k in self.tanks:
            t_in = t_ps if k["q"] > 0 else self.t_br
            k["tank"].step(h, k["q"], t_in, p["t_room"])

        # ---------------- building ----------------
        frac = self.building_fraction(now)
        t_s = self.t_s
        num_r = den_r = 0.0
        for b in self.branches:
            agg_tons = max(0.0, p["peak_tons"] * b["frac"] - self.ahu_tons(b))
            load = agg_tons * 12000.0 * frac
            l_d = max(1.0, agg_tons * 12000.0)
            c_air = l_d / (self.eat - self.lat) * max(0.3, min(1.2, load / l_d))
            c_air_d = l_d / (self.eat - self.lat)
            low_dt = 0.55 if self.fault("low_dt") else 1.0

            def capacity(q: float) -> float:
                if q < 0.5 or t_s >= self.eat:
                    return 0.0
                cw = 500.0 * q
                # tube-side film coefficient falls with water velocity (laminar at low flow)
                ua = self.ntu_d * c_air_d * (c_air / c_air_d) ** 0.6 * (q / max(1.0, b["q_d"])) ** 0.8 * low_dt
                cmin, cmax = min(c_air, cw), max(c_air, cw)
                return self._eps(ua / cmin, cmin / cmax) * cmin * (self.eat - t_s)

            qbank = b["q_bank"]
            c_w = 500.0 * qbank
            cap = capacity(qbank)
            met = min(load, cap)
            # the coil valves (aggregate of many local loops) move toward the opening that meets the load
            q_need = root(lambda q: load - capacity(q), 0.0, 2.0 * b["q_d"], xtol=0.5) if load > 0 else 0.0
            frac_need = q_need / math.sqrt(max(b["dp"], 0.3)) / b["c_open"]
            pos_t = 1.0 if frac_need >= 1.0 else bisect(lambda x: frac_need - equal_percentage(x), 0.0, 1.0, iters=20)
            b["pos"] += (pos_t - b["pos"]) * min(1.0, h / 30.0)
            lwt = t_s + met / c_w if c_w > 1.0 else t_s
            b["lwt"], b["load"], b["met"] = lwt, load, met
            # AHUs on this riser
            q_ahu = t_ahu_num = 0.0
            for a in self.ahus:
                if a["branch"] is b:
                    m = a["model"]
                    q_ahu += m.cc.flow_gpm
                    t_ahu_num += m.cc.flow_gpm * m.cc.lwt
                    m.p["chw_supply_temp"] = round(t_s, 2)
                    m.p["loop_dp_pct"] = max(0.0, min(200.0, 100.0 * b["dp"] / b["dp_d"])) if qs > 1.0 else 0.0
            b["q_ahu"] = q_ahu
            qb = qbank + b["q_bp"] + q_ahu
            rwt = (qbank * lwt + b["q_bp"] * t_s + t_ahu_num) / qb if qb > 1.0 else b["rwt"]
            b["rwt"] = rwt
            num_r += b["q"] * rwt
            den_r += b["q"]
        if den_r > 1.0:
            # water in the mains, risers and coils: the return answers over minutes, not instantly
            tau = p["loop_volume_gal"] / max(den_r, 200.0) * 60.0
            self.t_br += (num_r / den_r - self.t_br) * min(1.0, h / tau)
        self.building_tons = sum(b["load"] for b in self.branches) / 12000.0 + sum(
            max(0.0, -a["model"].cc.q) / 12000.0 for a in self.ahus)

        # ---------------- condenser water / towers ----------------
        q_c_tot = sum(c["q_c"] for c in self.chillers)
        if q_c_tot > 1.0:
            self.t_cwr = sum(c["q_c"] * c["yk"].lcwt for c in self.chillers) / q_c_tot
        levels = [t["cell"].level_in for t in self.towers if t["f_eq"] > 0.3]
        temps = [t["cell"].basin_t for t in self.towers if t["f_eq"] > 0.3]
        l_mean = sum(levels) / len(levels) if levels else 0.0
        t_eq = sum(temps) / len(temps) if temps else 60.0
        q_out_tot = t_out_num = 0.0
        for t in self.towers:
            pre, cell = t["pre"], t["cell"]
            q_eq = 1500.0 * t["f_eq"] * (l_mean - cell.level_in) if t["f_eq"] > 0.3 and len(levels) > 1 else 0.0
            heater_cmd = bool(self.inputs[f"{pre}_basin_htr"]) and util and not self.fault(f"{pre}_heater_failed")
            t["heater_on"] = heater_cmd and not cell.level_low()          # low level switch locks the heater out
            fan_pu = self.drives[pre].speed_pu
            cell.step(h, self.t_cwr, t["q_in"], t["q_bp"], t["q_out"], q_eq, t_eq, fan_pu, self.wb, p["oat"],
                      t["heater_on"], not self.fault(f"{pre}_makeup_stuck"),
                      self.fault(f"{pre}_nozzles_clogged"), self.fault(f"{pre}_fill_fouled"))
            q_out_tot += t["q_out"]
            t_out_num += t["q_out"] * cell.basin_t
        if q_out_tot > 1.0:
            self.t_cws = t_out_num / q_out_tot
        else:
            avg = sum(t["cell"].basin_t for t in self.towers) / max(1, len(self.towers))
            self.t_cws += (avg - self.t_cws) * min(1.0, h / 600.0)

        # header sensor lags (thermowells / mixing length)
        for name, val in (("pchws_temp", self.t_ps), ("pchwr_temp", self.t_r), ("schws_temp", self.t_s),
                          ("schwr_temp", self.t_br), ("cws_temp", self.t_cws), ("cwr_temp", self.t_cwr)):
            old = self.sens.get(name, val)
            self.sens[name] = old + (val - old) * min(1.0, h / 15.0)

    # ------------------------------------------------------------- publish

    def _noise(self, sd: float) -> float:
        return self.rng.gauss(0.0, sd) if sd > 0 else 0.0

    def _publish(self) -> None:
        if not hasattr(self, "drives"):
            return
        p, s = self.p, self.signals
        n = p["sensor_noise"]
        drift = lambda name: self.fault_value(f"{name}_drift") if self.fault(f"{name}_drift") else 0.0

        def flow_meter(name, q):
            if self.fault(f"{name}_zero"):
                return 0.0
            return round(q * (1.0 + drift(name) / 100.0) + self._noise(2.0), 0)

        # chillers
        plant_tons = plant_kw = 0.0
        for c in self.chillers:
            pre, yk = c["pre"], c["yk"]
            for key, val in yk.points().items():
                s[f"{pre}.{key}"] = val
            s[f"{pre}.status"] = yk.status_text()
            tons = yk.q / 12000.0
            s[f"{pre}.tons"] = round(tons, 0)
            s[f"{pre}.kw_per_ton"] = round(yk.kw / tons, 3) if tons > 10 else 0.0
            s[f"{pre}.evap_dp"] = round(float(c["evap_dp_ft"]) * (c["q_e"] / float(c["chw_design_gpm"])) ** 2 / FT_PER_PSI, 2)
            for base, v in ((f"{pre}_chw_vlv", c["vchw"]), (f"{pre}_cw_vlv", c["vcw"])):
                self._publish_valve(base, v)
            s[f"{pre}_chw_flow"] = 0.0 if self.fault(f"{pre}_chw_flow_zero") else round(max(0.0, c["q_e"] + self._noise(2.0)), 0)
            s[f"{pre}_cw_flow"] = round(max(0.0, c["q_c"] + self._noise(2.0)), 0)
            plant_tons += tons
            plant_kw += yk.kw
        # pumps
        for g, items in self.pumps.items():
            for x in items:
                pre, pump = x["pre"], x["pump"]
                v = self.drives[pre]
                plant_kw += max(0.0, v.power_kw)
                self._publish_drive(pre, float(x["ct_range_amps"]), "")
                s[f"{pre}.flow"] = round(pump.flow, 0)
                s[f"{pre}.head"] = round(pump.head, 1)
        # towers
        for t in self.towers:
            pre, cell = t["pre"], t["cell"]
            v = self.drives[pre]
            plant_kw += max(0.0, v.power_kw)
            self._publish_drive(pre, float(t["ct_range_amps"]), "fan_")
            for v_name, valve in (("inlet", t["vin"]), ("outlet", t["vout"]), ("bypass", t["vbp"]), ("eq", t["veq"])):
                self._publish_valve(f"{pre}_{v_name}_vlv", valve)
            s[f"{pre}_basin_temp"] = round(self.fault_value(f"{pre}_basin_sensor_failed")
                                           if self.fault(f"{pre}_basin_sensor_failed") else cell.basin_t + self._noise(n), 2)
            s[f"{pre}_level_lo"] = not cell.level_low()
            s[f"{pre}_level_hi"] = not cell.level_high()
            s[f"{pre}.fill_flow"] = round(t.get("q_in", 0.0), 0)
            s[f"{pre}.bypass_flow"] = round(t.get("q_bp", 0.0), 0)
            s[f"{pre}.out_flow"] = round(t.get("q_out", 0.0), 0)
            s[f"{pre}.level"] = round(cell.level_in, 1)
            s[f"{pre}.leaving"] = round(cell.t_out, 1)
            s[f"{pre}.ice"] = round(100.0 * cell.ice, 0)
            s[f"{pre}.tons"] = round(cell.heat_rejected / 15000.0, 0)
            s[f"{pre}.makeup"] = round(cell.q_makeup, 0)
            s[f"{pre}.vib_tripped"] = bool(t["vib"])
            s[f"{pre}.frozen"] = cell.frozen
            s[f"{pre}.heater_on"] = bool(t["heater_on"])
        # tanks
        th = 0.0
        vol = 0.0
        charge_num = 0.0
        for k in self.tanks:
            pre, tank = k["pre"], k["tank"]
            cnt = int(k["sensor_count"])
            for i in range(cnt):
                val = tank.sensor(i, cnt) + self._noise(n)
                if i == min(4, cnt - 1) and self.fault(f"{pre}_sensor_failed"):
                    val = self.fault_value(f"{pre}_sensor_failed")
                s[f"{pre}_temp_{i + 1:02d}"] = round(val, 2)
            self._publish_valve(f"{pre}_top_vlv", k["vtop"])
            self._publish_valve(f"{pre}_bot_vlv", k["vbot"])
            s[f"{pre}.flow"] = round(k["q"], 0)
            ch = tank.charge_fraction(p["chws_design_temp"], p["chwr_design_temp"])
            s[f"{pre}.charge"] = round(100.0 * ch, 1)
            s[f"{pre}.ton_hours"] = round(tank.ton_hours(p["chwr_design_temp"]), 0)
            s[f"{pre}.profile"] = ",".join(f"{x:.1f}" for x in tank.t[::-1])
            th += s[f"{pre}.ton_hours"]
            vol += tank.volume_gal
            charge_num += ch * tank.volume_gal
        # header sensors
        sens = self.sens
        for name in ("pchws_temp", "pchwr_temp", "schws_temp", "schwr_temp", "cws_temp", "cwr_temp"):
            s[name] = round(sens.get(name, 50.0) + self._noise(n) + drift(name), 2)
        s["pchw_flow"] = flow_meter("pchw_flow", self.qp)
        s["schw_flow"] = flow_meter("schw_flow", self.qs)
        dq = flow_meter("dcplr_flow", self.qt)
        s["dcplr_flow"] = -dq if self.fault("dcplr_flow_reversed") else dq
        s["schw_dp"] = round(max(0.0, self.plant_dp_ft / FT_PER_PSI + self._noise(0.03) + drift("schw_dp")), 2)
        s["utility_power"] = bool(getattr(self, "utility_avail", self.utility_ok))   # phase monitor: the real utility
        cold = [k["tank"].bottom for k in self.tanks]
        warm = [k["tank"].top for k in self.tanks]
        t_cold = self.t_ps if self.qt > 1 else (sum(k["tank"].bottom * -k["q"] for k in self.tanks if k["q"] < 0)
                                                / max(1.0, -self.qt) if self.qt < -1 else (sum(cold) / len(cold) if cold else 50.0))
        t_warm = self.t_br if self.qt < -1 else (sum(k["tank"].top * k["q"] for k in self.tanks if k["q"] > 0)
                                                / max(1.0, self.qt) if self.qt > 1 else (sum(warm) / len(warm) if warm else 50.0))
        s["tes_cold_temp"] = round(t_cold + self._noise(n) + drift("tes_cold_temp"), 2)
        s["tes_warm_temp"] = round(t_warm + self._noise(n) + drift("tes_warm_temp"), 2)
        # branches
        for b in self.branches:
            pre = b["pre"]
            dp = b["dp"] / FT_PER_PSI if self.qs > 1.0 else 0.0
            meas = dp * (0.5 if self.fault(f"{pre}_dp_tube_leak") else 1.0)
            s[f"{pre}_chw_remote_dp"] = round(max(0.0, meas + self._noise(0.03)), 2)
            s[f"{pre}.load"] = round(b["load"] / 12000.0, 0)
            s[f"{pre}.met"] = round(b["met"] / 12000.0, 0)
            s[f"{pre}.flow"] = round(b["q"], 0)
            s[f"{pre}.valve"] = round(100.0 * b["pos"], 0)
            s[f"{pre}.rwt"] = round(b["rwt"], 1)
            s[f"{pre}.dp"] = round(dp, 2)
            s[f"{pre}.ahu_flow"] = round(b["q_ahu"], 0)
        # totals
        s["primary_flow"] = round(self.qp, 0)
        s["secondary_flow"] = round(self.qs, 0)
        s["tes_flow"] = round(self.qt, 0)
        s["cw_flow"] = round(self.qc, 0)
        s["building_tons"] = round(getattr(self, "building_tons", 0.0), 0)
        s["plant_tons"] = round(plant_tons, 0)
        s["plant_kw"] = round(plant_kw, 0)
        s["tes_charge"] = round(100.0 * charge_num / vol, 1) if vol else 0.0
        s["tes_ton_hours"] = round(th, 0)
        s["schws_actual"] = round(self.t_s, 2)
        s["pchws_actual"] = round(self.t_ps, 2)
        s["cws_actual"] = round(self.t_cws, 2)
        s["plant_dp"] = round(self.plant_dp_ft / FT_PER_PSI, 2)
        s["oa_wb"] = round(getattr(self, "wb", 0.0), 1)
        s["oat"] = round(p["oat"], 1)
        s["occupied"] = self._occ
        s["utility_ok"] = bool(self.utility_ok)
        s["sim_clock"] = self.clock().strftime("%a %Y-%m-%d %H:%M")
        s["layout"] = json.dumps({
            "chillers": [c["pre"] for c in self.chillers],
            "primary_pumps": [x["pre"] for x in self.pumps["primary_pumps"]],
            "secondary_pumps": [x["pre"] for x in self.pumps["secondary_pumps"]],
            "condenser_pumps": [x["pre"] for x in self.pumps["condenser_pumps"]],
            "towers": [t["pre"] for t in self.towers],
            "tanks": [k["pre"] for k in self.tanks],
            "branches": [b["pre"] for b in self.branches],
            "tags": {**{c["pre"]: c["tag"] for c in self.chillers},
                     **{x["pre"]: x["tag"] for g in self.pumps.values() for x in g},
                     **{t["pre"]: t["tag"] for t in self.towers}, **{k["pre"]: k["tag"] for k in self.tanks},
                     **{b["pre"]: b["tag"] for b in self.branches}},
        })

    def _publish_valve(self, base: str, v: IsoValve) -> None:
        s = self.signals
        s[f"{base}_open"] = v.open_switch(self.fault(f"{base}_switch"))
        s[f"{base}_closed"] = v.closed_switch()
        s[f"{base}.pos"] = round(100.0 * v.pos, 0)

    def _publish_drive(self, pre: str, ct_range: float, sub: str) -> None:
        s = self.signals
        v = self.drives[pre]
        amps = v.current + (self._noise(0.1) if v.modulating else 0.0)
        s[f"{pre}_{sub}amps"] = round(min(ct_range, max(0.0, amps)), 2)
        s[f"{pre}_{sub}vfd_fb"] = round(v.role_out("speed_fb"), 1)
        s[f"{pre}_{sub}vfd_flt"] = v.role_out("fault_out")
        s[f"{pre}.freq"] = round(v.freq, 1)
        s[f"{pre}.status"] = v.status_line()
        if f"{pre}.kw" in s:
            s[f"{pre}.kw"] = round(max(0.0, v.power_kw), 1)


def max_speed(pumps) -> float:
    return max([x.speed for x in pumps] + [0.0])
