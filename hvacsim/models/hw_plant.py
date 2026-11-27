"""
Hot water plant (docs/HW_PLANT_DESIGN.md).

  B-1..n (Lochinvar Crest, BMS firing rate) each with a dedicated PHWP-n (VFD)
  primary supply hdr ── DECOUPLER (common pipe, + = supply -> return) ── primary return hdr
  SHWP-1..n (VFD, headered) ── main ── branches (hw_branches) ── coil banks + connected
  equipment (AHU coils, VAV reheat, unit heaters) + 3-way valve at the end ── return

Equipment comes from CSV schedules, so inputs / signals / faults are built per
instance (same conventions as chw_plant).  The boilers are Mode B devices
serving the Crest BMS object list (components/crest.py); everything else is
hardwired to the ALC controller in each row's controller_device.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any, Dict, List, Optional

from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef
from .chw_plant import DRIVE_FAULTS, ChwPlant, prefix
from .components.airside import equal_percentage
from .components.crest import CREST_FAULTS, CREST_OBJECTS, CrestBoiler
from .components.hydronic import FT_PER_PSI, IsoValve, Pump, bisect, parallel_r, root, solve_pumps

_A = "analog"
_F = "degreesFahrenheit"
_GPM = "usGallonsPerMinute"
_PSI = "poundsForcePerSquareInch"

EQUIPMENT_DEFAULTS = {
    "boilers": {"input_mbh": 4000.0, "design_gpm": 400.0, "min_flow_gpm": 120.0, "turndown": 25.0, "hx_dp_ft": 15.0},
    "primary_pumps": {"design_gpm": 400.0, "design_head_ft": 40.0, "motor_hp": 7.5, "ct_range_amps": 20.0, "boiler": "",
                      "drive_model": "ach580"},
    "secondary_pumps": {"design_gpm": 600.0, "design_head_ft": 80.0, "motor_hp": 20.0, "ct_range_amps": 50.0,
                        "drive_model": "ach580"},
    "branches": {"load_share_pct": 50.0, "design_remote_dp_psi": 6.0, "eob_bypass_pct": 5.0},
}

DEFAULT_LAYOUT: Dict[str, Any] = {
    "boilers": [{"tag": f"B-{i}", "controller_device": 242001, "sim_device": 4010050 + i, "mac": 50 + i}
                for i in (1, 2, 3)],
    "primary_pumps": [{"tag": f"PHWP-{i}", "boiler": f"B-{i}", "controller_device": 242001} for i in (1, 2, 3)],
    "secondary_pumps": [{"tag": f"SHWP-{i}", "controller_device": 242002} for i in (1, 2, 3)],
    "branches": [{"tag": "EAST", "load_share_pct": 55.0, "controller_device": 242011},
                 {"tag": "WEST", "load_share_pct": 45.0, "controller_device": 242012}],
}

# plant sensors: description, units (None = binary, True = normal), role (which controller by default)
HEADER_SENSORS = {
    "utility_power": ("Utility power status (closed = normal)", None, "plant"),
    "louver_open": ("Combustion air louver end switch (closed = open)", None, "plant"),
    "gas_press_ok": ("Building gas pressure switch (closed = normal)", None, "plant"),
    "boiler_estop": ("Boiler room emergency stop (closed = normal, open = pressed)", None, "plant"),
    "boiler_rm_temp": ("Boiler room temp", _F, "plant"),
    "phws_temp": ("Primary HW supply header temp", _F, "secondary"),
    "phwr_temp": ("Primary HW return header temp", _F, "secondary"),
    "shws_temp": ("Secondary HW supply temp", _F, "secondary"),
    "shwr_temp": ("Secondary HW return temp", _F, "secondary"),
    "shw_flow": ("Secondary HW flow", _GPM, "secondary"),
    "hw_dcplr_flow": ("Decoupler flow (+ supply to return, - return to supply)", _GPM, "secondary"),
    "hw_dp": ("Secondary plant DP (across secondary headers)", _PSI, "secondary"),
    "hw_loop_press": ("Loop pressure at the expansion tank connection", _PSI, "secondary"),
}

PUMP_GROUPS = {"primary_pumps": "Primary HW pump", "secondary_pumps": "Secondary HW pump"}
PUMP_FAULTS = {
    "worn_impeller": "worn impeller (low head)",
    "bearing": "bearing / seal failure (overload)",
    "cavitation": "cavitation (low suction pressure: low head, noisy)",
    "seized": "seized motor / pump (drive stalls and trips)",
}


def _rows(layout: Dict[str, Any], group: str) -> List[Dict[str, Any]]:
    out = []
    for row in layout.get(group) or []:
        r = dict(EQUIPMENT_DEFAULTS[group])
        r.update({k: v for k, v in row.items() if v not in (None, "")})
        r["tag"] = str(r["tag"])
        r["pre"] = prefix(r["tag"])
        out.append(r)
    return out


class HwPlant(EquipmentModel):
    TYPE = "hw_plant"
    DESCRIPTION = "Hot water plant: condensing boilers (BMS firing rate), primary/secondary, decoupler"
    MAX_SUBSTEP = 0.5

    DEFAULTS: Dict[str, Any] = {
        "design_mbh": 8000.0,            # building heating load at the design OA temperature
        "design_oat": 0.0,
        "balance_oat": 65.0,             # no envelope load above this
        "reheat_floor_pct": 10.0,        # VAV reheat all year (occupied)
        "warmup_factor": 1.3,            # morning warm-up after night setback
        "warmup_hours": 1.5,
        "hws_design_temp": 160.0,
        "hwr_design_temp": 140.0,
        "main_loss_psi": 8.0,            # secondary main at design flow
        "loop_volume_gal": 15000.0,
        "design_speed_hz": 55.0,         # balancer speed for design flow
        "fill_pressure_psi": 12.0,       # expansion tank connection, cold fill (60 °F)
        "relief_psi": 30.0,
        "occ_days": "mon-fri",
        "occ_start": 6.0,
        "occ_end": 18.0,
        "clock_start": "",
        "load_scale": 100.0,
        "oat": 30.0,                     # follows the host weather station
        "oa_w": 0.0030,
        "initial_loop_temp": 130.0,
        "sensor_noise": 0.03,
        "phwp_drive": {}, "shwp_drive": {},
    }
    WEATHER_PARAMS = {"oat": "oa_temp_actual", "oa_w": "oa_w_actual"}
    CONDITIONS = ["oat", "load_scale"]

    # ------------------------------------------------------------------ build

    def __init__(self, params: Optional[Dict[str, Any]] = None, layout: Optional[Dict[str, Any]] = None,
                 seed: Optional[int] = None):
        lay = dict(DEFAULT_LAYOUT)
        for k, v in (layout or {}).items():
            if v:
                lay[k] = v
        self.layout = lay
        self.rows = {g: _rows(lay, g) for g in EQUIPMENT_DEFAULTS}
        self._check_pumps()
        self.header_ctl = self._header_controllers(lay.get("header_sensors"))
        self.loads: List[Dict[str, Any]] = []      # connected equipment: {"kind", "model", "branch", "name"}
        self.utility_ok = True
        self.estop = False
        self._build_meta()
        super().__init__(params, seed)

    def _check_pumps(self) -> None:
        tags = [b["tag"] for b in self.rows["boilers"]]
        by_boiler: Dict[str, list] = {}
        for r in self.rows["primary_pumps"]:
            b = str(r.get("boiler") or "")
            if b not in tags:
                raise ValueError(f"primary pump {r['tag']}: boiler {b!r} is not in boilers.csv ({tags}) - "
                                 f"each boiler needs exactly one dedicated primary pump")
            by_boiler.setdefault(b, []).append(r["tag"])
        for t in tags:
            n = len(by_boiler.get(t, []))
            if n != 1:
                raise ValueError(f"boiler {t} has {n} primary pumps ({by_boiler.get(t, [])}) - "
                                 f"add or fix a row in primary_pumps.csv (boiler column)")

    def _header_controllers(self, cfg) -> Dict[str, int]:
        first = lambda g, d: int(self.rows[g][0]["controller_device"]) if self.rows[g] else d
        by_role = {"plant": first("boilers", 242001), "secondary": first("secondary_pumps", 242002)}
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
        }
        actions: List[str] = ["estop", "estop_reset"]
        io: Dict[int, Dict[str, list]] = {}
        self.io_names: Dict[str, str] = {}          # input -> I/O device object name (when different)

        def ctl(dev):
            return io.setdefault(int(dev), {"bo": [], "ao": [], "writes": []})

        def bo(dev, name, desc):
            inputs.append(SignalDef(name, "binary", description=desc, default=False))
            ctl(dev)["bo"].append(name)

        def ao(dev, name, desc, object_name=None):
            inputs.append(SignalDef(name, _A, "percent", desc, 0.0))
            ctl(dev)["ao"].append(name)
            if object_name:
                self.io_names[name] = object_name

        def ai(dev, name, units, desc, default=0.0, object_name=None):
            signals.append(SignalDef(name, _A, units, desc, default))
            ctl(dev)["writes"].append((name, object_name or name))

        def bi(dev, name, desc, default=True):
            signals.append(SignalDef(name, "binary", description=desc, default=default))
            ctl(dev)["writes"].append((name, name))

        def truth(name, units="noUnits", desc="", kind=_A, default=0.0):
            signals.append(SignalDef(name, kind, units, desc, default))

        # boilers: Crest BMS objects + hardwired alarm relay
        for b in R["boilers"]:
            pre, dev, tag = b["pre"], b["controller_device"], b["tag"]
            for kind, _n, oname, key, units, role in CREST_OBJECTS:
                if role == "cmd":
                    if kind.startswith("binary"):
                        inputs.append(SignalDef(f"{pre}.{key}", "binary", description=oname, default=False))
                    else:
                        inputs.append(SignalDef(f"{pre}.{key}", _A, units, oname, 0.0))
                elif kind.startswith("binary"):
                    truth(f"{pre}.{key}", kind="binary", desc=oname, default=False)
                elif kind.startswith("multi"):
                    truth(f"{pre}.{key}", "noUnits", oname, default=0)
                else:
                    truth(f"{pre}.{key}", units or "noUnits", oname)
            bi(dev, f"{pre}_alarm", f"{tag} alarm contacts (closed = alarm / lockout)", False)
            truth(f"{pre}.flow", _GPM, f"{tag} water flow")
            truth(f"{pre}.mbh_out", "kiloBtusPerHour", f"{tag} output MBH")
            truth(f"{pre}.hx_dp", _PSI, f"{tag} heat exchanger pressure drop")
            truth(f"{pre}.status", kind="text", desc=f"{tag} display status", default="")
            for k, txt in CREST_FAULTS.items():
                faults.append(FaultDef(f"{pre}_{k}", f"{tag}: {txt}", tag))
            actions += [f"{pre}.{k}" for k in ("local", "bms", "reset")]
            info[f"{pre}.local_sp"] = ParamInfo("", "Local supply setpoint", f"{tag} display", "°F", minimum=80, maximum=190)

        # pumps
        for g, label in PUMP_GROUPS.items():
            for r in R[g]:
                pre, dev, tag = r["pre"], r["controller_device"], r["tag"]
                bo(dev, f"{pre}_ss", f"{tag} start/stop")
                ao(dev, f"{pre}_vfd_spd", f"{tag} VFD speed (0-10 V), % of 10 V")
                ai(dev, f"{pre}_amps", "amperes", f"{tag} CT amps (0-{r['ct_range_amps']:.0f} A)")
                ai(dev, f"{pre}_vfd_fb", "percent", f"{tag} VFD speed feedback (AO1, % of 10 V)")
                bi(dev, f"{pre}_vfd_flt", f"{tag} VFD fault (RO3 closed = healthy)")
                for k, u, d in (("flow", _GPM, "flow"), ("head", "feetOfWater", "head"),
                                ("freq", "hertz", "output frequency"), ("kw", "kilowatts", "power")):
                    truth(f"{pre}.{k}", u, f"{tag} {d}")
                truth(f"{pre}.status", kind="text", desc=f"{tag} drive", default="")
                for k, txt in {**PUMP_FAULTS, **DRIVE_FAULTS}.items():
                    faults.append(FaultDef(f"{pre}_{k}", f"{tag}: {txt}", tag))
                ChwPlant._drive_info(info, pre, tag, r["drive_model"])
                actions += [f"{pre}.{k}" for k in ("hand", "off", "auto", "reset", "hand_ref")]

        # boiler room: louver
        plant_dev = self.header_ctl["louver_open"]
        bo(plant_dev, "louver", "Combustion air louver open command")

        # plant sensors
        for name, (desc, units, _role) in HEADER_SENSORS.items():
            dev = self.header_ctl[name]
            if units is None:
                bi(dev, name, desc)
            else:
                ai(dev, name, units, desc, 12.0 if units == _PSI and name == "hw_loop_press" else (130.0 if units == _F else 0.0))
                if units == _F:
                    faults.append(FaultDef(f"{name}_drift", f"{desc}: sensor drift", "Sensors", "number", 5.0, "°F"))
                    faults.append(FaultDef(f"{name}_failed", f"{desc}: sensor failed (open)", "Sensors"))
        faults += [
            FaultDef("shw_flow_drift", "Secondary flow meter drift", "Sensors", "number", 15.0, "%"),
            FaultDef("hw_dcplr_flow_failed", "Decoupler flow meter failed (reads 0)", "Sensors"),
            FaultDef("hw_dp_tube_leak", "Plant DP sensor tubing leak (reads low)", "Sensors"),
            FaultDef("gas_press_low", "Building gas pressure low", "Plant"),
            FaultDef("gas_press_high", "Building gas pressure high", "Plant"),
            FaultDef("louver_stuck_closed", "Combustion air louver stuck closed", "Plant"),
            FaultDef("air_in_loop", "Air in the loop (noisy DP, poorer heat transfer)", "Plant"),
            FaultDef("exp_tank_waterlogged", "Expansion tank waterlogged (pressure swings, relief lifts)", "Plant"),
        ]

        # building branches
        for b in R["branches"]:
            pre, dev, tag = b["pre"], b["controller_device"], b["tag"]
            ai(dev, f"{pre}_hw_remote_dp", _PSI, f"{tag} branch remote DP", object_name="hw_remote_dp")
            ai(dev, f"{pre}_hw_eob_temp", _F, f"{tag} end-of-branch supply temp", 130.0, object_name="hw_eob_temp")
            ao(dev, f"{pre}_hw_eob_vlv", f"{tag} end-of-branch 3-way valve (0-10 V), % open to bypass",
               object_name="hw_eob_vlv")
            for k, u, d in (("load", "kiloBtusPerHour", "load MBH"), ("met", "kiloBtusPerHour", "load met MBH"),
                            ("flow", _GPM, "branch flow"), ("valve", "percent", "coil valves (aggregate)"),
                            ("rwt", _F, "branch return temp"), ("dp", _PSI, "remote DP (true)"),
                            ("eob_flow", _GPM, "3-way bypass flow"), ("eob_pos", "percent", "3-way valve position"),
                            ("conn_flow", _GPM, "connected equipment flow"), ("eob_temp", _F, "end-of-branch temp (true)")):
                truth(f"{pre}.{k}", u, f"{tag} {d}")
            faults.append(FaultDef(f"{pre}_eob_vlv_failed", f"{tag} 3-way valve actuator failed", "Building"))
            faults.append(FaultDef(f"{pre}_dp_tube_leak", f"{tag} remote DP sensor tubing leak (reads low)", "Sensors"))
        faults.append(FaultDef("load_spike", "Building load spike", "Building", "number", 30.0, "%"))

        for k, u, d in (("primary_flow", _GPM, "primary flow"), ("secondary_flow", _GPM, "secondary flow"),
                        ("dcplr_flow", _GPM, "decoupler flow (+ supply to return)"),
                        ("building_mbh", "kiloBtusPerHour", "building load MBH"), ("plant_mbh", "kiloBtusPerHour", "boiler output MBH"),
                        ("gas_mbh", "kiloBtusPerHour", "gas input MBH"), ("plant_eff", "percent", "plant efficiency"),
                        ("plant_kw", "kilowatts", "pump power"),
                        ("phws_actual", _F, "true primary supply"), ("phwr_actual", _F, "true primary return"),
                        ("shws_actual", _F, "true secondary supply"), ("shwr_actual", _F, "true secondary return"),
                        ("plant_dp", _PSI, "true plant DP"), ("loop_press", _PSI, "true loop pressure"),
                        ("room_temp", _F, "true boiler room temp"), ("louver_pos", "percent", "louver position"),
                        ("oat", _F, "OA temp in use")):
            truth(k, u, d)
        truth("relief_lifting", kind="binary", desc="Relief valve lifting", default=False)
        truth("estop_pressed", kind="binary", desc="Emergency stop pressed", default=False)
        truth("occupied", kind="binary", desc="Building occupied (schedule)", default=False)
        truth("warmup", kind="binary", desc="Morning warm-up", default=False)
        truth("utility_ok", kind="binary", desc="Utility power present", default=True)
        truth("sim_clock", kind="text", desc="Simulated date/time", default="")
        truth("layout", kind="text", desc="Plant layout (json)", default="")

        self.INPUTS, self.SIGNALS, self.FAULTS = inputs, signals, faults
        self.PARAM_INFO, self.ACTIONS = info, actions
        self.io = io
        self.drive_keys = [r["pre"] for g in PUMP_GROUPS for r in R[g]]

    # ------------------------------------------------------------------ BACnet devices (Mode B)

    def bacnet_devices(self) -> List[Dict[str, Any]]:
        """The boilers' BMS interfaces: one device each (used by plant_runner)."""
        out = []
        for b in self.boilers:
            pre = b["pre"]
            cmds, mons = [], []
            for kind, n, oname, key, units, role in CREST_OBJECTS:
                obj = f"{kind},{n}"
                if role == "cmd":
                    d = {"object": obj, "name": oname, "input": f"{pre}.{key}", "description": oname}
                    if kind.startswith("binary"):
                        d.update(active_text="ENABLE", inactive_text="DISABLE")
                    else:
                        d["units"] = units
                    cmds.append(d)
                elif isinstance(role, tuple):
                    table = role[1]
                    mons.append({"object": obj, "name": oname, "signal": f"{pre}.{key}",
                                 "states": [table.get(i, f"Code {i}") for i in range(max(table) + 1)]})
                else:
                    d = {"object": obj, "name": oname, "signal": f"{pre}.{key}"}
                    if units:
                        d["units"] = units
                    mons.append(d)
            out.append({"tag": b["tag"], "pre": pre, "sim_device": int(b["sim_device"]), "mac": int(b["mac"]),
                        "model_name": "Lochinvar Crest SMART TOUCH BMS (simulated)",
                        "description": b.get("description", "Lochinvar Crest condensing boiler"),
                        "kind": "boiler", "commands": cmds, "monitors": mons})
        return out

    def device_offline(self, pre: str) -> bool:
        return (not self.utility_ok) or self.estop or self.fault(f"{pre}_gateway_offline")

    def watch_states(self) -> Dict[str, str]:
        return {b["tag"]: b["crest"].status_text().split(" · ")[0].split(":")[0] for b in self.boilers}

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        p = self.p
        R = self.rows
        s_d = p["design_speed_hz"] / 60.0
        self.drives, self.drive_p, self.inject = {}, {}, {}
        self.pumps: Dict[str, List[Dict[str, Any]]] = {}
        group_drive = {"primary_pumps": "phwp_drive", "secondary_pumps": "shwp_drive"}
        boilers_by_tag = {b["tag"]: b for b in R["boilers"]}
        for g in PUMP_GROUPS:
            self.pumps[g] = []
            for r in R[g]:
                qd, hd = float(r["design_gpm"]), float(r["design_head_ft"])
                if g == "primary_pumps":
                    # dedicated circuit: at the balancer speed and ~0 decoupler DP the pump makes boiler design flow
                    b = boilers_by_tag[str(r["boiler"])]
                    qb = float(b["design_gpm"])
                    k = 0.25 * hd / qd ** 2
                    br = max(1e-9, (1.25 * hd * s_d * s_d) / qb ** 2 - k)
                    pump = Pump(qd, hd, float(r["motor_hp"]), branch_r=br)
                else:
                    pump = Pump(qd, hd, float(r["motor_hp"]), branch_r=10.0 / qd ** 2)
                self._make_drive(r["pre"], float(r["motor_hp"]), 1.0, p[group_drive[g]], r["drive_model"])
                self.pumps[g].append({**r, "pump": pump})
        # boilers (each with its pump)
        self.boilers: List[Dict[str, Any]] = []
        self.faults_by: Dict[str, set] = {}
        for b in R["boilers"]:
            f: set = set()
            self.faults_by[b["pre"]] = f
            pump = next(x for x in self.pumps["primary_pumps"] if str(x["boiler"]) == b["tag"])
            self.boilers.append({**b, "crest": CrestBoiler(b["tag"], b, f, self.rng, t_init=p["initial_loop_temp"] - 40.0),
                                 "pumpx": pump, "q": 0.0})
        q_prim = sum(float(b["design_gpm"]) for b in R["boilers"]) or 1.0
        self.r_dec = 0.5 / q_prim ** 2                 # common pipe: 0.5 ft at full primary flow
        # building branches
        self.design_gpm = p["design_mbh"] * 1000.0 / (500.0 * max(1.0, p["hws_design_temp"] - p["hwr_design_temp"]))
        self.r_main = p["main_loss_psi"] * FT_PER_PSI / self.design_gpm ** 2
        sec = self.pumps["secondary_pumps"]
        s_hd = sec[0] if sec else {"design_gpm": 600.0, "design_head_ft": 80.0}
        plant_dp_d = 1.25 * float(s_hd["design_head_ft"]) * s_d * s_d - 0.25 * float(s_hd["design_head_ft"]) - 10.0
        shares = sum(float(b["load_share_pct"]) for b in R["branches"]) or 1.0
        self.branches: List[Dict[str, Any]] = []
        for b in R["branches"]:
            frac = float(b["load_share_pct"]) / shares
            qd = self.design_gpm * frac
            dp_d = float(b["design_remote_dp_psi"]) * FT_PER_PSI
            riser_ft = max(2.0, plant_dp_d - p["main_loss_psi"] * FT_PER_PSI - dp_d)
            self.branches.append({**b, "frac": frac, "q_d": qd, "dp_d": dp_d, "r_riser": riser_ft / qd ** 2,
                                  "c_open": 1.3 * qd / math.sqrt(dp_d),
                                  "c_eob": float(b["eob_bypass_pct"]) / 100.0 * qd / math.sqrt(dp_d),
                                  "pos": 0.5, "eob_pos": 0.0, "q": 0.0, "q_bank": 0.0, "q_conn": 0.0, "q_eob": 0.0,
                                  "dp": dp_d, "load": 0.0, "met": 0.0, "rwt": p["initial_loop_temp"] - 20.0,
                                  "t_eob": p["initial_loop_temp"] - 10.0})
        # aggregate coil bank calibration (heating coils: EAT 55 -> LAT 105 at design EWT / ΔT)
        self.eat, self.lat = 55.0, 105.0
        dT = p["hws_design_temp"] - p["hwr_design_temp"]
        c_air, c_w = 1.0 / (self.lat - self.eat), 1.0 / dT
        cmin, cmax = min(c_air, c_w), max(c_air, c_w)
        eps_d = 1.0 / cmin / (p["hws_design_temp"] - self.eat)
        self.ntu_d = bisect(lambda n: eps_d - ChwPlant._eps(n, cmin / cmax), 0.01, 20.0)
        # louver
        self.louver = IsoValve(30.0, fail_closed=True)
        # temperatures, flows
        t0 = p["initial_loop_temp"]
        self.t_ps = self.t_pr = self.t_s = t0 - 10.0
        self.t_br = t0 - 20.0
        self.qp = self.qs = self.qd = 0.0
        self.dp_dec = 0.0
        self.plant_dp_ft = 0.0
        self.room = 70.0
        self.loop_press = p["fill_pressure_psi"]
        self.relief = False
        self.sens: Dict[str, float] = {}
        start = p["clock_start"]
        self.clock0 = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M") if start else _dt.datetime.now().replace(microsecond=0)
        self._occ = False
        self._warm = False
        self.building_mbh = 0.0

    _make_drive = ChwPlant._make_drive
    _publish_drive = ChwPlant._publish_drive
    _eps = staticmethod(ChwPlant._eps)
    clock = ChwPlant.clock
    _occupied = ChwPlant._occupied

    # ------------------------------------------------------------- parameters

    def param_items(self) -> List[tuple]:
        return [(k, i, self._param_value(k)) for k, i in self.PARAM_INFO.items()]

    def _param_value(self, name: str) -> Any:
        if "." in name:
            pre, k = name.split(".", 1)
            if pre in self.drive_p:
                return self.drive_p[pre][k]
            b = self._boiler(pre)
            if b and k == "local_sp":
                return b["crest"].local_sp
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
            b = self._boiler(pre)
            if b and k == "local_sp":
                b["crest"].local_sp = max(80.0, min(190.0, float(value)))
                return
            ChwPlant.set_param(self, name, value)
            return
        super().set_param(name, value)

    def _chiller(self, pre: str) -> None:        # (ChwPlant.set_param helper)
        return None

    def _boiler(self, pre: str) -> Optional[Dict[str, Any]]:
        return next((b for b in getattr(self, "boilers", []) if b["pre"] == pre), None)

    def action(self, name: str, value: Any = None) -> None:
        if name == "estop":
            self.estop = True
        elif name == "estop_reset":
            self.estop = False
        elif "." in name:
            pre, key = name.split(".", 1)
            b = self._boiler(pre)
            if b:
                b["crest"].display(key)
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

    def _sync_inject(self) -> None:
        for pre in self.drives:
            inj = self.inject[pre]
            inj.clear()
            for k in DRIVE_FAULTS:
                if self.fault(f"{pre}_{k}"):
                    inj.add(k)
        for b in self.boilers:
            f = self.faults_by[b["pre"]]
            f.clear()
            for k in CREST_FAULTS:
                if self.fault(f"{b['pre']}_{k}"):
                    f.add(k)

    def load_fraction(self, now: _dt.datetime) -> float:
        """Building heating load as a fraction of design: envelope by OA, reheat floor, warm-up."""
        p = self.p
        occ = self._occ
        env = max(0.0, (p["balance_oat"] - p["oat"]) / max(1.0, p["balance_oat"] - p["design_oat"]))
        floor = p["reheat_floor_pct"] / 100.0
        hour = now.hour + now.minute / 60.0
        self._warm = occ and p["occ_start"] <= hour < p["occ_start"] + p["warmup_hours"]
        if occ:
            f = max(floor, env * (1.0 - floor) + floor)
            if self._warm:
                f *= p["warmup_factor"]
        else:
            f = 0.7 * env + 0.03                   # night setback, air handlers off
        f *= p["load_scale"] / 100.0
        if self.fault("load_spike"):
            f *= 1.0 + self.fault_value("load_spike") / 100.0
        return f

    def connected_mbh(self, branch: Dict[str, Any]) -> float:
        return sum(self._conn_design_gpm(x) * 10.0 for x in self.loads if x["branch"] is branch)

    @staticmethod
    def _conn_design_gpm(x: Dict[str, Any]) -> float:
        m = x["model"]
        if x["kind"] == "ahu":
            return m.ph.gpm_d + m.hc.gpm_d
        if x["kind"] == "vav":
            return float(m.p["reheat_gpm"])
        return float(m.p["hw_flow_gpm"])

    @staticmethod
    def _conn_flow(x: Dict[str, Any]) -> tuple:
        """(gpm, return temp) of connected equipment, from its own model."""
        m = x["model"]
        if x["kind"] == "ahu":
            q = m.ph.flow_gpm + m.hc.flow_gpm
            t = (m.ph.flow_gpm * m.ph.lwt + m.hc.flow_gpm * m.hc.lwt) / q if q > 0.01 else m.p["hw_supply_temp"]
            return q, t
        if x["kind"] == "vav":
            q = m.coil.flow_gpm
            return q, (m.coil.lwt if q > 0.01 else m.p["hw_supply_temp"])
        q = float(m.hw_gpm) if hasattr(m, "hw_gpm") else 0.0
        t = m.hw_rwt if hasattr(m, "hw_rwt") else m.p["hw_supply_temp"]
        return q, t

    # ------------------------------------------------------------- hydraulics

    def _solve(self) -> None:
        prim = [b["pumpx"]["pump"] for b in self.boilers]
        sec = [x["pump"] for x in self.pumps["secondary_pumps"]]
        air = 0.85 if self.fault("air_in_loop") else 1.0
        for b in self.branches:
            c_bank = b["c_open"] * equal_percentage(b["pos"]) * air
            c_conn = b["q_conn"] / math.sqrt(max(b["dp"], 1.0))
            c_eob = b["c_eob"] * b["eob_pos"]
            b["c_bank"], b["c_conn"], b["c_eob2"] = c_bank, c_conn, c_eob
            c_tot = c_bank + c_conn + c_eob
            b["r_end"] = 1.0 / c_tot ** 2 if c_tot > 1e-6 else float("inf")
            b["r_b"] = b["r_riser"] + b["r_end"]
        r_bld = self.r_main + parallel_r(b["r_b"] for b in self.branches)
        r_dec = self.r_dec

        def prim_flow(dp):
            q = 0.0
            for x in prim:
                q += x.flow_at(dp)
            return q

        def sec_flow(dp):
            return solve_pumps(sec, -dp, r_bld)

        def resid(qd):
            dp = r_dec * qd * abs(qd)
            return prim_flow(dp) - sec_flow(dp)[0] - qd

        lo, hi = self.qd - 200.0, self.qd + 200.0
        if resid(lo) < 0 or resid(hi) > 0:
            lo, hi = -sec_flow(0.0)[0] - 10.0, prim_flow(0.0) + 10.0
        qd = root(resid, lo, hi, xtol=0.05)
        dp = r_dec * qd * abs(qd)
        qs, hs = sec_flow(dp)
        for x in prim:
            x.flow = x.flow_at(dp)
            x.head = x.pump_head(x.flow) if x.flow > 0 else x.shutoff * x.speed * x.speed
        qp = sum(x.flow for x in prim)
        self.qp, self.qs, self.qd = qp, qs, qp - qs
        self.dp_dec = dp
        for b in self.boilers:
            b["q"] = b["pumpx"]["pump"].flow
        self.plant_dp_ft = max(0.0, hs + dp)
        h_after = max(0.0, hs + dp - self.r_main * qs * qs)
        for b in self.branches:
            qb = math.sqrt(h_after / b["r_b"]) if b["r_b"] < 1e12 and qs > 0 else 0.0
            b["q"] = qb
            b["dp"] = b["r_end"] * qb * qb if b["r_end"] < 1e12 else h_after
            ct = b["c_bank"] + b["c_conn"] + b["c_eob2"]
            b["q_bank"] = qb * b["c_bank"] / ct if ct > 0 else 0.0
            b["q_eob"] = qb * b["c_eob2"] / ct if ct > 0 else 0.0

    # ------------------------------------------------------------- step

    def _step(self, h: float) -> None:
        p = self.p
        now = self.clock()
        self._occ = self._occupied(now)
        self._sync_inject()
        util = self.utility_ok

        # ---------------- louver, gas ----------------
        if self.fault("louver_stuck_closed"):
            self.louver.step(h, False, util, stuck_at=0.0)
        else:
            self.louver.step(h, bool(self.inputs["louver"]), util)
        louver_ok = self.louver.open_switch()
        gas_ok = not (self.fault("gas_press_low") or self.fault("gas_press_high"))

        # ---------------- drives ----------------
        for g, items in self.pumps.items():
            for x in items:
                pre, pump = x["pre"], x["pump"]
                v = self.drives[pre]
                v.powered = util
                v.wire(start=bool(self.inputs[f"{pre}_ss"]), safeties=True, speed_ref=float(self.inputs[f"{pre}_vfd_spd"]))
                if self.fault(f"{pre}_seized"):
                    v.step(h, lambda s: 3.0)                 # locked rotor: the drive stalls and trips
                else:
                    b = 1.25 if self.fault(f"{pre}_bearing") else 1.0
                    v.step(h, lambda s, pump=pump, b=b: pump.load_torque_pu(s) * b)
                wear = 0.82 if self.fault(f"{pre}_worn_impeller") else 1.0
                if self.fault(f"{pre}_cavitation"):
                    wear *= 0.85 + 0.05 * self.rng.random()
                pump.wear = wear
                pump.cavitation = 0.3 if self.fault(f"{pre}_cavitation") else 0.0
                pump.speed = 0.0 if self.fault(f"{pre}_seized") else v.speed_pu

        # ---------------- hydraulics ----------------
        self._solve()

        # ---------------- boilers / primary temperatures ----------------
        qp, qs, qd = self.qp, self.qs, self.qd
        # primary return: secondary return plus (when primary > secondary) supply short-circuiting
        if qp > 1.0:
            if qd > 0:
                self.t_pr = ((qp - qd) * self.t_br + qd * self.t_ps) / qp
            else:
                self.t_pr = self.t_br
        else:
            self.t_pr += (self.room - self.t_pr) * min(1.0, h / 7200.0)
        num = den = 0.0
        for b in self.boilers:
            c = b["crest"]
            pre = b["pre"]
            c.powered = util and not self.estop
            c.louver_ok, c.gas_ok = louver_ok, gas_ok
            c.comm_ok = not self.device_offline(pre)
            c.bms_enable = bool(self.inputs[f"{pre}.bms_enable"])
            c.bms_rate = float(self.inputs[f"{pre}.bms_rate"])
            t_out = c.step(h, b["q"], self.t_pr if b["q"] > 1.0 else c.t_hx, self.room)
            if b["q"] > 1.0:
                num += b["q"] * t_out
                den += b["q"]
        if den > 1.0:
            self.t_ps = num / den
        else:
            self.t_ps += (self.room - self.t_ps) * min(1.0, h / 7200.0)
        # secondary supply: primary supply, plus (when secondary > primary) return water through the decoupler
        if qs > 1.0:
            self.t_s = (min(qs, qp) * self.t_ps + max(0.0, -qd) * self.t_br) / qs if qp > 1.0 or qd < 0 else self.t_br
        else:
            self.t_s += (self.t_ps - self.t_s) * min(1.0, h / 600.0)

        # ---------------- building ----------------
        frac = self.load_fraction(now)
        t_s = self.t_s
        air = 0.85 if self.fault("air_in_loop") else 1.0
        num_r = den_r = 0.0
        total_load = 0.0
        for b in self.branches:
            pre = b["pre"]
            agg = max(0.0, p["design_mbh"] * 1000.0 * b["frac"] - self.connected_mbh(b) * 1000.0)
            load = agg * frac
            l_d = max(1.0, agg)
            c_air_d = l_d / (self.lat - self.eat)
            c_air = c_air_d * max(0.3, min(1.2, load / l_d))

            def capacity(q: float) -> float:
                if q < 0.5 or t_s <= self.eat:
                    return 0.0
                cw = 500.0 * q
                ua = self.ntu_d * c_air_d * (c_air / c_air_d) ** 0.6 * (q / max(1.0, b["q_d"])) ** 0.8 * air
                cmin, cmax = min(c_air, cw), max(c_air, cw)
                return self._eps(ua / cmin, cmin / cmax) * cmin * (t_s - self.eat)

            qbank = b["q_bank"]
            cap = capacity(qbank)
            met = min(load, cap)
            q_need = root(lambda q: load - capacity(q), 0.0, 2.0 * b["q_d"], xtol=0.5) if load > 0 else 0.0
            frac_need = q_need / math.sqrt(max(b["dp"], 0.3)) / b["c_open"]
            pos_t = 1.0 if frac_need >= 1.0 else bisect(lambda x: frac_need - equal_percentage(x), 0.0, 1.0, iters=20)
            b["pos"] += (pos_t - b["pos"]) * min(1.0, h / 30.0)
            lwt = t_s - met / (500.0 * qbank) if qbank > 0.5 else t_s
            b["load"], b["met"] = load, met
            total_load += load
            # 3-way valve at the end of the branch
            cmd = float(self.inputs[f"{pre}_hw_eob_vlv"]) / 100.0
            if not self.fault(f"{pre}_eob_vlv_failed"):
                b["eob_pos"] += max(-h / 60.0, min(h / 60.0, cmd - b["eob_pos"]))
            # end-of-branch pipe: purged by the flow that reaches the far end, cools toward the ceiling space
            q_end = b["q_eob"] + 0.15 * qbank
            vol = p["loop_volume_gal"] * b["frac"] * 0.15
            k_flow = q_end / max(1.0, vol) / 60.0
            k_loss = 1.0 / (3.0 * 3600.0)
            b["t_eob"] += (k_flow * (t_s - b["t_eob"]) + k_loss * (70.0 - b["t_eob"])) * h
            b["t_eob"] = min(max(b["t_eob"], 50.0), max(t_s, 70.0))
            # connected equipment
            q_conn = t_conn = 0.0
            for x in self.loads:
                if x["branch"] is not b:
                    continue
                m = x["model"]
                m.p["hw_supply_temp"] = round(t_s, 2)
                m.p["hw_dp_pct"] = max(0.0, min(200.0, 100.0 * b["dp"] / b["dp_d"])) if qs > 1.0 else 0.0
                gq, gt = self._conn_flow(x)
                q_conn += gq
                t_conn += gq * gt
            b["q_conn"] = q_conn
            qb = qbank + b["q_eob"] + q_conn
            rwt = (qbank * lwt + b["q_eob"] * b["t_eob"] + t_conn) / qb if qb > 1.0 else b["rwt"]
            b["rwt"] = rwt
            num_r += b["q"] * rwt
            den_r += b["q"]
        if den_r > 1.0:
            tau = p["loop_volume_gal"] / max(den_r, 100.0) * 60.0
            self.t_br += (num_r / den_r - self.t_br) * min(1.0, h / tau)
        else:
            self.t_br += (70.0 - self.t_br) * min(1.0, h / 14400.0)
        self.building_mbh = total_load / 1000.0 + sum(
            max(0.0, self._conn_flow(x)[0] * 500.0 * (t_s - self._conn_flow(x)[1])) / 1000.0 for x in self.loads)

        # ---------------- expansion tank / loop pressure ----------------
        t_avg = 0.5 * (self.t_s + self.t_br)
        slope = (18.0 if self.fault("exp_tank_waterlogged") else 3.0) / 120.0
        press = p["fill_pressure_psi"] + slope * (t_avg - 60.0)
        self.relief = press >= p["relief_psi"]
        self.loop_press = min(press, p["relief_psi"] + 0.5)

        # ---------------- boiler room ----------------
        firing = sum(b["crest"].rate * float(b["input_mbh"]) for b in self.boilers if b["crest"].flame) / 1000.0
        share = 0.5 * self.louver.pos
        target = 68.0 + 1.5 * firing + (p["oat"] - 68.0) * (0.1 + share)
        self.room += (target - self.room) * min(1.0, h / 900.0)

        # header sensor lags
        for name, val in (("phws_temp", self.t_ps), ("phwr_temp", self.t_pr), ("shws_temp", self.t_s),
                          ("shwr_temp", self.t_br), ("boiler_rm_temp", self.room)):
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
        plant_mbh = gas_mbh = 0.0
        for b in self.boilers:
            pre, c = b["pre"], b["crest"]
            for key, val in c.points().items():
                s[f"{pre}.{key}"] = val
            s[f"{pre}.status"] = c.status_text()
            s[f"{pre}_alarm"] = bool(c.lockout)
            s[f"{pre}.flow"] = round(b["q"], 0)
            s[f"{pre}.mbh_out"] = round(c.q / 1000.0, 0)
            s[f"{pre}.hx_dp"] = round(float(b["hx_dp_ft"]) * (b["q"] / float(b["design_gpm"])) ** 2 / FT_PER_PSI, 2)
            plant_mbh += c.q / 1000.0
            gas_mbh += (c.rate * float(b["input_mbh"])) if c.flame else 0.0
        kw = 0.0
        for g, items in self.pumps.items():
            for x in items:
                pre, pump = x["pre"], x["pump"]
                kw += max(0.0, self.drives[pre].power_kw)
                self._publish_drive(pre, float(x["ct_range_amps"]), "")
                s[f"{pre}.flow"] = round(pump.flow, 0)
                s[f"{pre}.head"] = round(pump.head, 1)
        sens = self.sens
        for name in ("phws_temp", "phwr_temp", "shws_temp", "shwr_temp", "boiler_rm_temp"):
            if self.fault(f"{name}_failed"):
                s[name] = -40.0
                continue
            d = self.fault_value(f"{name}_drift") if self.fault(f"{name}_drift") else 0.0
            s[name] = round(sens.get(name, 70.0) + self._noise(n) + d, 2)
        drift = self.fault_value("shw_flow_drift") / 100.0 if self.fault("shw_flow_drift") else 0.0
        s["shw_flow"] = round(max(0.0, self.qs * (1.0 + drift) + self._noise(1.0)), 0)
        s["hw_dcplr_flow"] = 0.0 if self.fault("hw_dcplr_flow_failed") else round(self.qd + self._noise(1.0), 0)
        noisy = 0.4 if self.fault("air_in_loop") else 0.03
        dp_meas = self.plant_dp_ft / FT_PER_PSI * (0.5 if self.fault("hw_dp_tube_leak") else 1.0)
        s["hw_dp"] = round(max(0.0, dp_meas + self._noise(noisy)), 2)
        s["hw_loop_press"] = round(self.loop_press + self._noise(0.05), 2)
        s["utility_power"] = bool(getattr(self, "utility_avail", self.utility_ok))   # phase monitor: the real utility
        s["louver_open"] = self.louver.open_switch()
        s["gas_press_ok"] = not (self.fault("gas_press_low") or self.fault("gas_press_high"))
        s["boiler_estop"] = not self.estop
        for b in self.branches:
            pre = b["pre"]
            dp = b["dp"] / FT_PER_PSI if self.qs > 1.0 else 0.0
            meas = dp * (0.5 if self.fault(f"{pre}_dp_tube_leak") else 1.0)
            s[f"{pre}_hw_remote_dp"] = round(max(0.0, meas + self._noise(noisy)), 2)
            s[f"{pre}_hw_eob_temp"] = round(b["t_eob"] + self._noise(n), 2)
            s[f"{pre}.load"] = round(b["load"] / 1000.0, 0)
            s[f"{pre}.met"] = round(b["met"] / 1000.0, 0)
            s[f"{pre}.flow"] = round(b["q"], 0)
            s[f"{pre}.valve"] = round(100.0 * b["pos"], 0)
            s[f"{pre}.rwt"] = round(b["rwt"], 1)
            s[f"{pre}.dp"] = round(dp, 2)
            s[f"{pre}.eob_flow"] = round(b["q_eob"], 1)
            s[f"{pre}.eob_pos"] = round(100.0 * b["eob_pos"], 0)
            s[f"{pre}.conn_flow"] = round(b["q_conn"], 1)
            s[f"{pre}.eob_temp"] = round(b["t_eob"], 1)
        s["primary_flow"] = round(self.qp, 0)
        s["secondary_flow"] = round(self.qs, 0)
        s["dcplr_flow"] = round(self.qd, 0)
        s["building_mbh"] = round(self.building_mbh, 0)
        s["plant_mbh"] = round(plant_mbh, 0)
        s["gas_mbh"] = round(gas_mbh, 0)
        s["plant_eff"] = round(100.0 * plant_mbh / gas_mbh, 1) if gas_mbh > 1 else 0.0
        s["plant_kw"] = round(kw, 1)
        s["phws_actual"] = round(self.t_ps, 2)
        s["phwr_actual"] = round(self.t_pr, 2)
        s["shws_actual"] = round(self.t_s, 2)
        s["shwr_actual"] = round(self.t_br, 2)
        s["plant_dp"] = round(self.plant_dp_ft / FT_PER_PSI, 2)
        s["loop_press"] = round(self.loop_press, 2)
        s["room_temp"] = round(self.room, 1)
        s["louver_pos"] = round(100.0 * self.louver.pos, 0)
        s["oat"] = round(p["oat"], 1)
        s["relief_lifting"] = bool(self.relief)
        s["estop_pressed"] = bool(self.estop)
        s["occupied"] = self._occ
        s["warmup"] = bool(self._warm)
        s["utility_ok"] = bool(self.utility_ok)
        s["sim_clock"] = self.clock().strftime("%a %Y-%m-%d %H:%M")
        s["layout"] = json.dumps({
            "boilers": [b["pre"] for b in self.boilers],
            "primary_pumps": [x["pre"] for x in self.pumps["primary_pumps"]],
            "secondary_pumps": [x["pre"] for x in self.pumps["secondary_pumps"]],
            "branches": [b["pre"] for b in self.branches],
            "tags": {**{b["pre"]: b["tag"] for b in self.boilers},
                     **{x["pre"]: x["tag"] for g in self.pumps.values() for x in g},
                     **{b["pre"]: b["tag"] for b in self.branches}},
            "connected": [{"name": x["name"], "kind": x["kind"], "branch": x["branch"]["tag"]} for x in self.loads],
        })
