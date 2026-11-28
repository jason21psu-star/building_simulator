"""
Trane CenTraVac CVHE with Symbio 800 controls, as seen through its BACnet (or
Modbus) interface.  Built from (docs/TRANE_CVHE_DESIGN.md):

  * Symbio 800 Integration Points List, BACnet, CenTraVac  BAS-PTS009C-EN (12/2025)
    object naming, Simplex AI / AV / BI / BV / MI / MV tables, diagnostic BIs and codes,
    Modbus registers
  * CenTraVac CVHE, CVHF, CVHG with Symbio Controls IOM  CVHE-SVX005E-EN (04/2026)
    sequence of operation (figures 50-56), field control wiring (table 22)

Operation in short:
  * the BAS writes Chiller Auto Stop Command BAS (1 = Auto, 0 = Stop); in Auto the chiller
    energizes its evaporator water pump relay, proves flow within 4 min 15 s (6 s filter), and
    starts when leaving water is above setpoint + differential to start, its start-to-start and
    stop-to-start timers allow it
  * start: condenser water pump relay, condenser flow within 4 min 15 s, oil pump to 12 psid for
    60 s prelube, IGVs overdriven closed, wye-delta start; then IGV modulation to setpoint with
    soft load and limit modes (demand limit, condenser pressure, evaporator refrigerant temp)
  * satisfied (below setpoint - differential to stop): IGVs close (up to 50 s), compressor off,
    3 min post-lube, condenser pump relay off; Stop / external auto-stop / latching diagnostic
    do the same and then drop the evaporator pump relay after its off delay
  * diagnostics: MAR (non-latching), MMR (latching, manual reset), informational; one BI each,
    the last code in Unit Source ID (Last Diagnostic Code); Reset Diagnostic BV resets

R-514A low-pressure refrigerant: the evaporator runs in a vacuum.

Values not in either document are marked (A).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

from ...base import ParamInfo
from ..vendor import register
from .base import Chiller

# ---------------------------------------------------------------- states and codes (points list)

RUNNING_MODE = {0: "Chiller Off", 1: "Chiller In Start Mode", 2: "Chiller In Run Mode",
                3: "Chiller In Pre-Shutdown Mode", 4: "Chiller In Service Mode"}        # MI-10100 (value = code + 1)
OPERATING_MODE = {0: "Cool", 1: "Heat", 2: "Ice Making", 3: "Free Cooling"}          # MI-10101 / MV-10100
SETPOINT_SOURCE = {0: "BAS", 1: "External", 2: "Front Panel"}                       # MI-10102
MODEL_INFO = {0: "CVHF", 1: "CVGF", 2: "CVHS", 10: "CDHF", 11: "CVR", 12: "CVHH", 13: "CDHH", 19: "CVHM",
              37: "CVHE", 38: "CVHG", 39: "CVHL", 42: "CDHG", 71: "CVZA", 72: "CVXA"}   # MI-10103
REFRIGERANT = {0: "R-11", 1: "R-12", 2: "R-22", 3: "R-123", 4: "R-134a", 7: "R-113", 8: "R-114", 9: "R-500",
               10: "R-502", 12: "R-513A", 13: "R-1233zd(E)", 14: "R-514A"}            # MI-10105

MAR, MMR, INFO = "MAR", "MMR", "IFW"
# diagnostic key -> (code, name, type, BACnet BI instance, Modbus register)
DIAGNOSTICS: Dict[str, Tuple[int, str, str, int, int]] = {
    "evap_flow_overdue": (31001, "Evaporator Water Flow Overdue", MAR, 11211, 34212),
    "evap_flow_lost": (31002, "Evaporator Water Flow Lost", MAR, 11210, 34211),
    "cond_flow_overdue": (41001, "Condenser Water Flow Overdue", MAR, 11196, 34197),
    "cond_flow_lost": (41002, "Condenser Water Flow Lost", MAR, 11195, 34196),
    "emergency_stop": (71001, "Emergency Stop", MMR, 11202, 34203),
    "low_lwt_unit_on": (161002, "Low Evap Leaving Water Temp: Unit On", MAR, 11261, 34263),
    "oil_overdue": (171008, "Differential Oil Pressure Overdue", MMR, 11200, 34201),
    "low_oil_dp": (171001, "Low Differential Oil Pressure", MMR, 11259, 34261),
    "low_evap_rfgt": (201001, "Low Evaporator Refrigerant Temperature", MMR, 11262, 34264),
    "high_cond_press": (0, "High Condenser Pressure", MMR, 11228, 34230),     # code not in the table (A)
    "surge": (211005, "Extended Compressor Surge", MMR, 11213, 34215),
    "starter_fault": (211081, "Starter Fault Type I", MMR, 11314, 34316),
    "motor_hot": (211160, "High Motor Winding Temperature 1", MMR, 11235, 34237),
    "power_loss": (211091, "EM Momentary Power Loss", MAR, 11271, 34273),
    "restart_inhibit": (211186, "Restart Inhibit", INFO, 11303, 34305),
}

TRANE_FAULTS = {
    "evap_fs_failed": "Chilled water flow proving input fails open (31002 / 31001)",
    "cond_fs_failed": "Condenser water flow proving input fails open (41002 / 41001)",
    "low_charge": "Low refrigerant charge (evap limit, then 201001)",
    "cond_fouled": "Condenser tubes fouled (condenser limit, then High Condenser Pressure)",
    "evap_fouled": "Evaporator tubes fouled",
    "starter_fault": "Starter fault (211081 Starter Fault Type I)",
    "gateway_offline": "BACnet / Modbus interface offline (last BAS values held)",
    "surge": "Compressor surge (211005 Extended Compressor Surge)",
    "low_oil_pressure": "Oil pump weak (171001 Low Differential Oil Pressure / 171008 overdue)",
    "motor_winding_hot": "Motor winding overheating (211160)",
}

# ---------------------------------------------------------------- sequence values

BOOT_S = 15.0                    # IOM: controller power-up up to 15 s
FLOW_PROVE_S = 255.0             # 4 min 15 s
FLOW_FILTER_S = 6.0
PRELUBE_S = 60.0
OIL_PROVE_S = 180.0              # 12 psid within 3 min
OIL_MIN_PSID = 12.0
OIL_RUN_PSID = 18.0              # IOM checklist: the oil pump can provide 18 psid
STAR_DELTA_S = 8.0               # (A) wye to delta transition after acceleration
IGV_CLOSE_S = 50.0
POSTLUBE_S = 180.0               # CVH: 3 minutes
LOW_LWT_CUTOUT_F = 36.0          # (A) leaving water temperature cutout
LOW_EVAP_RFGT_F = 32.0           # (A) low evaporator refrigerant temperature cutout (limit 3 °F above)
HIGH_COND_SAT_F = 115.0          # (A) high condenser pressure cutout as a saturation temperature
COND_LIMIT_F = 108.0             # (A) condenser pressure limit
SOFTLOAD_PER_S = 0.10 / 60.0     # (A) 10 %/min loading limit after a start
SOFTLOAD_S = 900.0               # (A)

P_ATM = 14.696


def r514a_psia(t_f: float) -> float:
    """R-514A saturation pressure (A: Clausius-Clapeyron fit, NBP 84.2 °F, slope of R-123)."""
    t_k = (t_f - 32.0) * 5.0 / 9.0 + 273.15
    return math.exp(13.643 - 3310.0 / t_k)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ---------------------------------------------------------------- integration list
# key, BACnet (kind, instance, name), Modbus (kind, register, encoding), units, role
_F, _DF, _PSI, _PCT = "degreesFahrenheit", "deltaDegreesFahrenheit", "poundsForcePerSquareInch", "percent"
_MSV = lambda table, n: ("msv", table, n)
_AI = [  # instance, name, key, units
    (10100, "Active Chilled Water Setpoint", "active_sp", _F),
    (10101, "Chilled Water Setpoint Status", "sp_status", _F),
    (10106, "Active Cool/Heat Setpoint Temperature", "active_coolheat_sp", _F),
    (10107, "Evaporator Leaving Water Temperature", "lchwt", _F),
    (10108, "Evaporator Entering Water Temperature", "echwt", _F),
    (10109, "Condenser Entering Water Temperature", "ecwt", _F),
    (10110, "Condenser Leaving Water Temperature", "lcwt", _F),
    (10118, "Unit Source ID (Last Diagnostic Code)", "last_diag", "noUnits"),
    (10124, "Evaporator Refrigerant Pressure Circuit 1", "evap_psia", _PSI),
    (10125, "Condenser Refrigerant Pressure Circuit 1", "cond_psia", _PSI),
    (10126, "Differential Refrigerant Pressure Circuit 1", "diff_rfgt", _PSI),
    (10129, "Oil Differential Pressure Circuit 1", "oil_dp", _PSI),
    (10130, "Oil Temperature - Compressor 1A", "oil_temp", _F),
    (10131, "Evaporator Saturated Refrigerant Temperature Circuit 1", "evap_sat", _F),
    (10132, "Condenser Saturated Refrigerant Temperature Circuit 1", "cond_sat", _F),
    (10134, "Inlet Guide Vane 1 Percent Open Circuit 1", "igv", _PCT),
    (10145, "Starts - Compressor 1A", "starts", "noUnits"),
    (10146, "Run Time - Compressor 1A (in seconds)", "run_s", "noUnits"),
    (10154, "Average Line Current Circuit 1", "amps", "amperes"),
    (10155, "Line 1 Current RLA - Compressor 1A", "pct_rla", _PCT),
    (10158, "Actual Running Capacity", "capacity_pct", _PCT),
    (10159, "Unit Power Consumption", "kw", "kilowatts"),
    (10163, "Motor Winding Temperature 1 Circuit 1", "winding_t", _F),
    (10200, "Restart Inhibit Time Remaining", "restart_inhibit_s", "noUnits"),
    (10201, "Chiller Design Capacity", "design_tons", "tonsRefrigeration"),
    (10206, "Condenser Approach Temperature Circuit 1", "cond_approach", _DF),
    (10207, "Evaporator Approach Temperature Circuit 1", "evap_approach", _DF),
    (10209, "Active Demand Limit Setpoint", "active_demand_limit", _PCT),
    (10228, "Energy Consumption", "kwh", "kilowattHours"),
    (10234, "Condenser Refrigerant Gauge Pressure Circuit 1", "cond_psig", _PSI),
    (10237, "Evaporator Refrigerant Gauge Pressure Circuit 1", "evap_psig", _PSI),
    (10244, "Active Cooling Differential to Start", "active_diff_start", _DF),
    (10245, "Active Cooling Differential to Stop", "active_diff_stop", _DF),
]
_BI = [  # instance, name, key
    (10101, "Emergency Stop", "estop_bi"), (10102, "Manual Override Exists", "manual_override"),
    (10104, "Run Enabled", "run_enabled"), (10105, "Local Setpoint Control", "local_sp_control"),
    (10106, "Maximum Capacity", "max_capacity"), (10108, "Head Relief Request", "head_relief"),
    (10113, "Chiller Running State", "running_state"), (10114, "Evaporator Water Pump Request", "evap_pump_req"),
    (10115, "Evaporator Water Flow Status", "evap_flow"), (10116, "Condenser Water Pump Request", "cond_pump_req"),
    (10117, "Condenser Water Flow Status", "cond_flow"), (10118, "Diagnostic Present", "diag_present"),
    (10119, "Diagnostic Shutdown Present", "diag_shutdown"),
    (10120, "Diagnostic: Manual Reset Required", "diag_manual_reset"),
    (10121, "Diagnostic: Local Manual Reset Required", "diag_local_reset"),
    (10122, "Diagnostic Present: Information", "diag_info"), (10123, "Diagnostic Present: Advisory", "diag_advisory"),
    (10124, "Diagnostic Present: Critical", "diag_critical"),
    (10125, "Diagnostic Present: Service Required", "diag_service"),
    (10126, "Compressor 1A Status", "compressor_on"), (10127, "Front Panel Auto Stop", "front_panel_auto"),
    (10128, "External Auto Stop Input Status", "ext_auto_stop_bi"),
]
_AV = [  # instance, name, key, units
    (10100, "Chilled Water Setpoint", "chw_sp", _F),
    (10101, "Demand Limit Setpoint", "demand_limit", _PCT),
    (10104, "BAS Cooling Differential to Start", "diff_start", _DF),
    (10105, "BAS Cooling Differential to Stop", "diff_stop", _DF),
]
_BV = [  # instance, name, key
    (10101, "Reset Diagnostic", "reset_diag"),
    (10102, "Evaporator Water Pump Request BAS", "evap_pump_bas"),
    (10103, "Condenser Water Pump Request BAS", "cond_pump_bas"),
    (10104, "Chiller Auto Stop Command BAS", "auto_cmd"),
]
_MI = [  # instance, name, key, table, states
    (10100, "Running Mode", "running_mode", RUNNING_MODE, 5),
    (10101, "Operating Mode", "operating_mode", OPERATING_MODE, 4),
    (10102, "Chiller Setpoint Source", "sp_source", SETPOINT_SOURCE, 3),
    (10103, "Model Information [GEN2]", "model_info", MODEL_INFO, 73),
    (10104, "Cooling Type", "cooling_type", {0: "Water Cooled"}, 1),
    (10105, "Refrigerant Type", "refrigerant", REFRIGERANT, 15),
]

_POINTS: List[Tuple] = []
for n, name, key, u in _AV:
    _POINTS.append((key, ("analog-value", n, name), ("holding-register", 40011 + 2 * (n - 10100), "float32"), u, "cmd"))
for n, name, key in _BV:
    _POINTS.append((key, ("binary-value", n, name), ("holding-register", 43011 + (n - 10100), 1.0), None, "cmd"))
_POINTS.append(("mode_cmd", ("multi-state-value", 10100, "Chiller Mode Command BAS"), ("holding-register", 42011, 1.0),
                None, "cmd"))
for n, name, key, u in _AI:
    _POINTS.append((key, ("analog-input", n, name), ("input-register", 30011 + 2 * (n - 10100), "float32"), u, None))
for n, name, key in _BI:
    _POINTS.append((key, ("binary-input", n, name), ("input-register", 33011 + (n - 10100), 1.0), None, None))
for n, name, key, table, count in _MI:
    _POINTS.append((key, ("multi-state-input", n, name), ("input-register", 32011 + (n - 10100), 1.0), None,
                    _MSV(table, count)))
for key, (_code, name, _t, n, reg) in DIAGNOSTICS.items():
    _POINTS.append((f"diag_{key}", ("binary-input", n, f"Diagnostic: {name}"), ("input-register", reg, 1.0), None, None))

TRANE_OBJECTS = [(b[0], b[1], b[2], key, u, role) for key, b, _m, u, role in _POINTS]
TRANE_MODBUS_OBJECTS = [(m[0], m[1], b[2], key, u, role, m[2]) for key, b, m, u, role in _POINTS]


@register
class TraneCVHE(Chiller):
    MODEL = "trane_cvhe"
    MAKE = "Trane"
    DISPLAY = "Trane CenTraVac CVHE (Symbio 800)"
    SHORT = "Trane CVHE"
    PROTOCOL = "bacnet"
    INTEGRATION = "Symbio 800 BACnet points (BAS-PTS009C)"
    DEVICE_MODEL_NAME = "Trane Symbio 800 CenTraVac (simulated)"
    DEFAULT_DESCRIPTION = "Trane CenTraVac CVHE, R-514A, IGV, wye-delta"
    OBJECTS = TRANE_OBJECTS
    PART_LOAD_CURVE = "igv"
    ROLES = {"run_cmd": "auto_cmd", "chws_sp": "chw_sp", "demand_limit": "demand_limit",
             "running": "compressor_on", "alarm": "diag_manual_reset", "lchwt": "lchwt", "echwt": "echwt",
             "lcwt": "lcwt", "ecwt": "ecwt", "pct_load": "pct_rla", "kw": "kw", "ready": "run_enabled",
             "local": None, "pump_request": "evap_pump_req"}
    PANEL = ["auto", "stop", "reset"]
    PANEL_PARAMS = {"front_sp": ParamInfo("", "Front panel chilled water setpoint", "display", "°F", minimum=0,
                                          maximum=90)}
    SETTINGS = {"start_to_start_min": 20.0, "stop_to_start_s": 30.0, "evap_pump_off_delay_min": 1.0,
                "diff_to_start_f": 2.0, "diff_to_stop_f": 2.0, "power_up_delay_s": 0.0}
    FAULTS = TRANE_FAULTS
    FAULT_ALIASES = {"comm_loss": "gateway_offline"}
    CODE_TABLES = {"diagnostics": {v[0]: v[1] for v in DIAGNOSTICS.values()}, "running_mode": RUNNING_MODE}
    BINARY_TEXTS = {"auto_cmd": ("Auto", "Stop"), "reset_diag": ("Reset", "Normal"), "evap_pump_bas": ("On", "Auto"),
                    "cond_pump_bas": ("On", "Auto")}
    CMD_STATES = {"mode_cmd": (OPERATING_MODE, 4)}
    HARDWIRED_INPUTS = {"ext_auto_stop": ("External Auto Stop input (1A13 J2-1/2, closed = normal)", True),
                        "estop": ("Emergency Stop input (1A13 J2-3/4, closed = normal)", True)}

    @classmethod
    def cmd_default(cls, key: str, cfg: Dict[str, Any]) -> Any:
        """Relinquish defaults: the front panel values (front panel mode Auto (A))."""
        return {"chw_sp": float(cfg.get("chws_design_temp", 44.0)), "demand_limit": 100.0,
                "diff_start": 2.0, "diff_stop": 2.0, "auto_cmd": True}.get(key, 0.0)

    def __init__(self, tag: str, cfg: Dict[str, Any], faults: Set[str], rng: Optional[Any] = None):
        self.tag = tag
        self.cfg = cfg
        self.faults = faults
        self._init_core(cfg)
        st = self.settings_for(cfg)
        self.start_to_start_s = float(st["start_to_start_min"]) * 60.0
        self.stop_to_start_s = float(st["stop_to_start_s"])
        self.evap_off_delay_s = float(st["evap_pump_off_delay_min"]) * 60.0
        self.front_sp = self.sp_d
        self.power_up_delay_s = float(st["power_up_delay_s"])
        # BAS objects
        self.chw_sp = self.sp_d
        self.demand_limit = 100.0
        self.diff_start = float(st["diff_to_start_f"])
        self.diff_stop = float(st["diff_to_stop_f"])
        self.reset_diag = False
        self.evap_pump_bas = False
        self.cond_pump_bas = False
        self.auto_cmd = True
        self.mode_cmd = 0.0
        self.hardwired = {k: v[1] for k, v in self.HARDWIRED_INPUTS.items()}
        self.front_panel_auto = True
        self.powered = True
        self.comm_ok = True
        self.reset_state()

    def reset_state(self) -> None:
        self._reset_core()
        self.state = "boot"              # boot, stopped, auto, starting, running, preparing, shutting
        self.sub = ""                    # starting sub-step: cond_flow, prelube, accel
        self.timer = BOOT_S
        self.since_start = 1e9
        self.since_stop = 1e9
        self.evap_pump_req = False
        self.cond_pump_req = False
        self.evap_off_timer = 0.0
        self.postlube = 0.0
        self.oil_dp = 0.0
        self.oil_low_s = 0.0
        self.oil_temp = 120.0
        self.winding_t = 95.0
        self.starts = 0
        self.run_s = 0.0
        self.igv = 0.0
        self.diags: Dict[str, bool] = {}   # active diagnostic key -> latched (MMR waiting for reset)
        self.last_diag = 0
        self._evap_flow_s = 0.0
        self._cond_flow_s = 0.0
        self.evap_flow = False
        self.cond_flow = False
        self._evap_wait = 0.0
        self._cond_wait = 0.0
        self._prev_reset = False
        self._surge_s = 0.0
        self._lowref_s = 0.0
        self.limit = ""
        self._was_running = False

    # ----- BAS side ------------------------------------------------------
    def bas_write(self, key: str, value: Any, seen: bool = True) -> None:
        if key not in self.cmd_keys():
            raise KeyError(f"{self.DISPLAY}: {key!r} is not a command object")
        if key in ("reset_diag", "evap_pump_bas", "cond_pump_bas", "auto_cmd"):
            setattr(self, key, bool(value))
        else:
            setattr(self, key, float(value))

    def set_hardwired(self, key: str, closed: bool) -> None:
        self.hardwired[key] = bool(closed)

    # ----- helpers ---------------------------------------------------------
    @property
    def setpoint(self) -> float:
        return _clamp(float(self.chw_sp), 0.0, 90.0)

    @property
    def active_setpoint(self) -> float:
        return self.setpoint

    @property
    def sp_source(self) -> int:
        return 2 if abs(float(self.chw_sp) - self.front_sp) < 1e-6 else 0

    @property
    def active_demand_limit(self) -> float:
        return _clamp(float(self.demand_limit), 40.0, 100.0)

    @property
    def running(self) -> bool:
        """Compressor energized."""
        return self.state in ("running", "preparing") or (self.state == "starting" and self.sub == "accel")

    @property
    def alarm(self) -> bool:
        return any(self.DIAG_TYPE(k) == MMR for k in self.diags)

    @staticmethod
    def DIAG_TYPE(key: str) -> str:
        return DIAGNOSTICS[key][2]

    def _diag(self, key: str) -> None:
        if key not in self.diags:
            self.diags[key] = True
            code = DIAGNOSTICS[key][0]
            if code:
                self.last_diag = code

    def _clear_diag(self, key: str) -> None:
        if key in self.diags and self.DIAG_TYPE(key) != MMR:
            del self.diags[key]

    def _shutdown_diag(self) -> bool:
        return any(DIAGNOSTICS[k][2] in (MMR, MAR) for k in self.diags
                   if k not in ("power_loss", "cond_flow_overdue", "cond_flow_lost"))

    def _auto(self) -> bool:
        return (bool(self.auto_cmd) and self.front_panel_auto and self.hardwired["ext_auto_stop"]
                and self.hardwired["estop"] and not any(DIAGNOSTICS[k][2] == MMR for k in self.diags))

    def _pct_rla(self) -> float:
        return 100.0 * self.kw / (self.kw_d * 1.08)

    def restart_inhibit_left(self) -> float:
        return max(0.0, self.start_to_start_s - self.since_start, self.stop_to_start_s - self.since_stop)

    # ----- panel -----------------------------------------------------------
    def panel(self, key: str) -> None:
        if key == "auto":
            self.front_panel_auto = True
        elif key == "stop":
            self.front_panel_auto = False
        elif key == "reset":
            self._reset_diagnostics()
        else:
            raise KeyError(key)

    def _reset_diagnostics(self) -> None:
        for k in list(self.diags):
            if not self._cause_present(k):
                del self.diags[k]

    def _cause_present(self, key: str) -> bool:
        f = self.faults
        return {"emergency_stop": not self.hardwired["estop"], "starter_fault": "starter_fault" in f,
                "motor_hot": "motor_winding_hot" in f and self.winding_t > 220.0}.get(key, False)

    # ----- main step ---------------------------------------------------------
    def step(self, h: float, evap_gpm: float, cond_gpm: float, ewt: float, ecwt: float, t_room: float = 75.0) -> None:
        f = self.faults
        self.evap_gpm, self.cond_gpm = max(0.0, evap_gpm), max(0.0, cond_gpm)
        self.echwt, self.ecwt = ewt, ecwt
        self.since_start += h
        self.since_stop += h
        self.limit = ""

        if not self.powered:
            if self.running:
                self._diag("power_loss")
            if self.state not in ("boot",):
                self.since_stop = 0.0 if self.running else self.since_stop
            self.state, self.sub, self.timer = "boot", "", BOOT_S + self.power_up_delay_s
            self.cap, self.igv = 0.0, 0.0
            self.evap_pump_req = self.cond_pump_req = False
            self.postlube = 0.0
            self._core_thermal(h, t_room, coasting=False)
            self._oil(h)
            self.kw = 0.0
            return

        # flow proving inputs (6 s filter)
        e_ok = self.evap_gpm > 0.5 * self.chw_gpm_d and "evap_fs_failed" not in f
        c_ok = self.cond_gpm > 0.5 * self.cw_gpm_d and "cond_fs_failed" not in f
        self._evap_flow_s = self._evap_flow_s + h if e_ok != self.evap_flow else 0.0
        self._cond_flow_s = self._cond_flow_s + h if c_ok != self.cond_flow else 0.0
        if self._evap_flow_s >= FLOW_FILTER_S:
            self.evap_flow, self._evap_flow_s = e_ok, 0.0
        if self._cond_flow_s >= FLOW_FILTER_S:
            self.cond_flow, self._cond_flow_s = c_ok, 0.0

        # diagnostic reset over BACnet: on the write of Reset
        if self.reset_diag and not self._prev_reset:
            self._reset_diagnostics()
        self._prev_reset = bool(self.reset_diag)

        if not self.hardwired["estop"]:
            if self.running or self.state == "starting":
                self._panic_stop()
            self._diag("emergency_stop")

        # flow diagnostics clear themselves once flow is proven (MAR)
        if self.evap_flow:
            self._clear_diag("evap_flow_overdue")
            self._clear_diag("evap_flow_lost")
        if self.cond_flow:
            self._clear_diag("cond_flow_overdue")
            self._clear_diag("cond_flow_lost")
        auto = self._auto()
        sp = self.setpoint

        # ---- state machine ----
        st = self.state
        if st == "boot":
            self.timer -= h
            if self.timer <= 0:
                st = "auto" if auto else "stopped"
        elif st == "stopped":
            self.cond_pump_req = False
            if self.evap_off_timer > 0:
                self.evap_off_timer -= h
            self.evap_pump_req = self.evap_off_timer > 0 or self.evap_pump_bas
            if auto:
                st, self._evap_wait = "auto", 0.0
        if st == "auto":
            self.evap_pump_req = True
            self.cond_pump_req = False
            if not auto:
                st, self.evap_off_timer = "stopped", self.evap_off_delay_s
            else:
                if not self.evap_flow:
                    self._evap_wait += h
                    if self._evap_wait > FLOW_PROVE_S:
                        self._diag("evap_flow_overdue")
                else:
                    self._evap_wait = 0.0
                    self._clear_diag("evap_flow_overdue")
                    self._clear_diag("evap_flow_lost")
                need = self.lchwt > sp + self.diff_start
                if self.lchwt >= sp + 2.0:
                    self._clear_diag("low_lwt_unit_on")
                if self.restart_inhibit_left() > 0 and need:
                    self.diags.setdefault("restart_inhibit", True)
                else:
                    self.diags.pop("restart_inhibit", None)
                # condenser flow diagnostics don't block: the next start re-proves condenser flow
                blocking = [k for k in self.diags if DIAGNOSTICS[k][2] in (MAR, MMR)
                            and k not in ("power_loss", "cond_flow_overdue", "cond_flow_lost")]
                if (need and self.evap_flow and not blocking and self.restart_inhibit_left() <= 0
                        and self.postlube <= 0):
                    st, self.sub, self.timer, self._cond_wait = "starting", "cond_flow", 0.0, 0.0
                    self.diags.pop("restart_inhibit", None)
        elif st == "starting":
            self.evap_pump_req = True
            self.cond_pump_req = True
            if not auto:
                st, self.sub, self.evap_off_timer = "stopped", "", self.evap_off_delay_s
                self.since_stop = 0.0
            elif not self.evap_flow:
                st = self._flow_trip("evap_flow_lost")
            elif self.sub == "cond_flow":
                if self.cond_flow:
                    self._clear_diag("cond_flow_overdue")
                    self.sub, self.timer = "prelube", 0.0
                else:
                    self._cond_wait += h
                    if self._cond_wait > FLOW_PROVE_S:
                        self._diag("cond_flow_overdue")
                        st, self.sub = "auto", ""
                        self.cond_pump_req = False
            elif self.sub == "prelube":
                self.timer += h
                if self.oil_dp >= OIL_MIN_PSID:
                    self.oil_low_s += h
                else:
                    self.oil_low_s = 0.0
                    if self.timer > OIL_PROVE_S:
                        self._diag("oil_overdue")
                        st, self.sub = "stopped", ""
                        self.evap_off_timer = self.evap_off_delay_s
                if st == "starting" and self.oil_low_s >= PRELUBE_S:
                    self.sub, self.timer = "accel", 0.0
                    self.starts += 1
                    self.since_start = 0.0
                    if "starter_fault" in f:
                        self._diag("starter_fault")
                        self._panic_stop()
                        st = "shutting"
            elif self.sub == "accel":
                self.timer += h
                self.cap = 0.0
                if self.timer >= STAR_DELTA_S:
                    st, self.sub, self.timer = "running", "", 0.0
                    self.diags.pop("power_loss", None)
        elif st == "running":
            self.evap_pump_req = self.cond_pump_req = True
            self.timer += h
            if not self.evap_flow:
                st = self._flow_trip("evap_flow_lost")
            elif not self.cond_flow:
                st = self._flow_trip("cond_flow_lost")
            elif "starter_fault" in f:
                self._diag("starter_fault")
                self._panic_stop()
                st = "shutting"
            elif not auto or self._shutdown_diag():
                st, self.timer = "preparing", 0.0
            elif self.lchwt < sp - self.diff_stop:
                st, self.timer = "preparing", 0.0
            else:
                self._capacity_control(h, sp)
        elif st == "preparing":                 # IGVs close, up to 50 s
            self.evap_pump_req = self.cond_pump_req = True
            self.timer += h
            self.cap = max(0.0, self.cap - h * max(self.cap, 0.05) / max(1.0, IGV_CLOSE_S - self.timer + h))
            if auto and not self._shutdown_diag() and self.lchwt > sp + self.diff_start:
                st = "running"
            elif self.timer >= IGV_CLOSE_S or self.cap <= 0.0:
                self.cap, self.igv = 0.0, 0.0
                self.postlube = POSTLUBE_S
                self.since_stop = 0.0
                st = "shutting"
        if st == "shutting":                    # compressor off, post-lube
            self.cond_pump_req = False
            self.postlube = max(0.0, self.postlube - h)
            if self.postlube <= 0:
                if self._auto():
                    st, self._evap_wait = "auto", 0.0
                else:
                    st, self.evap_off_timer = "stopped", self.evap_off_delay_s
            else:
                self.evap_pump_req = True              # the chilled water pump runs through post-lube
        self.state = st
        if not self.running:
            self.cap = 0.0
        self.cond_pump_req = self.cond_pump_req or bool(self.cond_pump_bas)
        self.evap_pump_req = self.evap_pump_req or bool(self.evap_pump_bas)

        self._core_thermal(h, t_room, coasting=self.state == "shutting")
        self._oil(h)
        self._protect(h, sp)
        if self.running:
            self.run_s += h
        self.kwh += self.kw * h / 3600.0

    def _flow_trip(self, key: str) -> str:
        self._diag(key)
        self._panic_stop()
        return "shutting"

    def _panic_stop(self) -> None:
        """Immediate stop: compressor off now, IGVs not sequenced (IOM: panic stop)."""
        if self.running or self.state == "starting":
            self.since_stop = 0.0
            self.postlube = POSTLUBE_S if self.running or self.sub == "accel" else self.postlube
        self.cap, self.igv = 0.0, 0.0
        self.state, self.sub = "shutting", ""
        if self.postlube <= 0:
            self.postlube = 1e-6

    def _capacity_control(self, h: float, sp: float) -> None:
        err = self.lchwt - sp
        up = SOFTLOAD_PER_S if self.since_start < SOFTLOAD_S else 0.004
        rate = _clamp(0.0025 * err, -0.01, up)
        fla = self._pct_rla()
        lim = self.active_demand_limit
        if fla > lim:
            rate, self.limit = min(rate, -0.004), "Current Limit"
        elif fla > lim - 2.0:
            rate, self.limit = min(rate, 0.0), "Current Limit"
        if self.cond_sat > COND_LIMIT_F:
            rate, self.limit = min(rate, -0.003), "Condenser Limit"
        if self.evap_sat < LOW_EVAP_RFGT_F + 3.0:
            rate, self.limit = min(rate, -0.003), "Evaporator Limit"
        self.cap = _clamp(self.cap + rate * h, 0.0, 1.05)
        q_max = 500.0 * self.evap_gpm * max(0.0, self.echwt - 34.0)
        if self.cap * self.capacity * 12000.0 > q_max:
            self.cap = q_max / (self.capacity * 12000.0)
        self.igv = _clamp(100.0 * math.sqrt(self.cap / 1.05) if self.cap > 0 else 0.0, 0.0, 100.0)

    def _oil(self, h: float) -> None:
        pumping = self.state in ("starting", "running", "preparing") or self.postlube > 0
        pumping = pumping and self.powered and not (self.state == "starting" and self.sub == "cond_flow")
        target = (OIL_RUN_PSID * (0.55 if "low_oil_pressure" in self.faults else 1.0)) if pumping else 0.0
        self.oil_dp += (target - self.oil_dp) * min(1.0, h / 8.0)
        tgt_t = 135.0 if self.running else 142.0          # IOM: heater holds 140-145 °F when off
        self.oil_temp += (tgt_t - self.oil_temp) * min(1.0, h / 900.0)

    def _protect(self, h: float, sp: float) -> None:
        f = self.faults
        if self.running:
            if self.oil_dp < OIL_MIN_PSID and self.state == "running":
                self.oil_low_s += h
                if self.oil_low_s > 10.0:
                    self._diag("low_oil_dp")
                    self._panic_stop()
                    self.state = "shutting"
            if self.evap_sat < LOW_EVAP_RFGT_F:
                self._lowref_s += h
                if self._lowref_s > 30.0:
                    self._diag("low_evap_rfgt")
            else:
                self._lowref_s = 0.0
            if self.cond_sat > HIGH_COND_SAT_F:
                self._diag("high_cond_press")
            if self.lchwt < LOW_LWT_CUTOUT_F:
                self._diag("low_lwt_unit_on")
            if "surge" in f:
                self._surge_s += h
                if self._surge_s > 60.0:
                    self._diag("surge")
            else:
                self._surge_s = 0.0
            load = self.kw / max(1.0, self.kw_d)
            w_tgt = 140.0 + 60.0 * load + (120.0 if "motor_winding_hot" in f else 0.0)
            if self.winding_t > 265.0:
                self._diag("motor_hot")
        else:
            w_tgt = 95.0
            self._lowref_s = 0.0
        self.winding_t += (w_tgt - self.winding_t) * min(1.0, h / 300.0)

    # ----- published values ----------------------------------------------------
    def running_mode_code(self) -> int:
        if self.state == "starting":
            return 1
        if self.state == "running":
            return 2
        if self.state == "preparing":
            return 3
        return 0

    def points(self) -> Dict[str, Any]:
        p_e, p_c = r514a_psia(self.evap_sat), r514a_psia(self.cond_sat)
        diag_types = {DIAGNOSTICS[k][2] for k in self.diags}
        out = {
            "chw_sp": float(self.chw_sp), "demand_limit": float(self.demand_limit), "diff_start": float(self.diff_start),
            "diff_stop": float(self.diff_stop), "reset_diag": bool(self.reset_diag),
            "evap_pump_bas": bool(self.evap_pump_bas), "cond_pump_bas": bool(self.cond_pump_bas),
            "auto_cmd": bool(self.auto_cmd), "mode_cmd": float(self.mode_cmd),
            "active_sp": round(self.setpoint, 1), "sp_status": round(self.setpoint, 1),
            "active_coolheat_sp": round(self.setpoint, 1),
            "lchwt": round(self.lchwt, 1), "echwt": round(self.echwt, 1), "ecwt": round(self.ecwt, 1),
            "lcwt": round(self.lcwt, 1), "last_diag": float(self.last_diag),
            "evap_psia": round(p_e, 2), "cond_psia": round(p_c, 2), "diff_rfgt": round(p_c - p_e, 2),
            "oil_dp": round(self.oil_dp, 1), "oil_temp": round(self.oil_temp, 1),
            "evap_sat": round(self.evap_sat, 1), "cond_sat": round(self.cond_sat, 1), "igv": round(self.igv, 0),
            "starts": float(self.starts), "run_s": float(int(self.run_s)),
            "amps": round(self._pct_rla() / 100.0 * self.cfg_rla(), 0), "pct_rla": round(self._pct_rla(), 1),
            "capacity_pct": round(100.0 * self.q / (self.capacity * 12000.0), 0), "kw": round(self.kw, 0),
            "winding_t": round(self.winding_t, 1), "restart_inhibit_s": round(self.restart_inhibit_left(), 0),
            "design_tons": self.capacity, "cond_approach": round(self.cond_sat - self.lcwt, 1),
            "evap_approach": round(self.lchwt - self.evap_sat, 1),
            "active_demand_limit": round(self.active_demand_limit, 0), "kwh": round(self.kwh, 0),
            "cond_psig": round(p_c - P_ATM, 2), "evap_psig": round(p_e - P_ATM, 2),
            "active_diff_start": float(self.diff_start), "active_diff_stop": float(self.diff_stop),
            "estop_bi": not self.hardwired["estop"], "manual_override": bool(self.evap_pump_bas or self.cond_pump_bas),
            "run_enabled": self._auto() and self.state in ("auto", "starting", "running", "preparing"),
            "local_sp_control": self.sp_source == 2, "max_capacity": self.running and self.cap >= 1.0,
            "head_relief": self.limit == "Condenser Limit", "running_state": self.running,
            "evap_pump_req": self.evap_pump_req, "evap_flow": self.evap_flow, "cond_pump_req": self.cond_pump_req,
            "cond_flow": self.cond_flow, "diag_present": bool(self.diags),
            "diag_shutdown": bool(diag_types & {MAR, MMR}), "diag_manual_reset": MMR in diag_types,
            "diag_local_reset": False, "diag_info": INFO in diag_types, "diag_advisory": False,
            "diag_critical": MMR in diag_types, "diag_service": False, "compressor_on": self.running,
            "front_panel_auto": not self.front_panel_auto, "ext_auto_stop_bi": self.hardwired["ext_auto_stop"],
            "running_mode": self.running_mode_code(), "operating_mode": 0, "sp_source": self.sp_source,
            "model_info": 37, "cooling_type": 0, "refrigerant": 14,
        }
        for k in DIAGNOSTICS:
            out[f"diag_{k}"] = k in self.diags
        if not self.powered:
            out = {k: (False if isinstance(v, bool) else 0.0) if k not in self.cmd_keys() else v for k, v in out.items()}
        return out

    def cfg_rla(self) -> float:
        """Nameplate RLA (A): design kW x 1.08 at 480 V, 0.9 power factor."""
        return self.kw_d * 1.08 / (1.732 * 0.48 * 0.9)

    def status_text(self) -> str:
        if not self.powered:
            return "NO POWER"
        if self.state == "boot":
            return "Power Up"
        text = {"stopped": "Stopped", "auto": "Auto", "starting": "Starting", "running": "Running",
                "preparing": "Preparing Shutdown", "shutting": "Shutting Down"}[self.state]
        if self.state == "stopped" and (self.diags or not self.hardwired["ext_auto_stop"]):
            text = "Run Inhibit"
        sub = {"auto": "Waiting to Start", "running": self.limit and f"Running – Limit ({self.limit})",
               "starting": {"cond_flow": "Establishing Condenser Water Flow", "prelube": "Prelube",
                            "accel": "Starting Compressor"}.get(self.sub, ""),
               "shutting": f"Post-lube {self.postlube:.0f} s"}.get(self.state, "")
        if self.state == "stopped":
            sub = ("External Auto-Stop" if not self.hardwired["ext_auto_stop"] else
                   "Local Stop" if not self.front_panel_auto else "Tracer Stop" if not self.auto_cmd else "")
        txt = text + (f" · {sub}" if sub else "")
        if "restart_inhibit" in self.diags and self.state == "auto":
            txt += f" · Restart Inhibit {self.restart_inhibit_left():.0f} s"
        for k in self.diags:
            if k != "restart_inhibit":
                txt += f" · {DIAGNOSTICS[k][2]} {DIAGNOSTICS[k][1]}"
        return txt

    def generic_state(self) -> str:
        if not self.powered:
            return "no_power"
        if self.alarm:
            return "lockout"
        if self.state == "starting":
            return "starting"
        if self.state == "running":
            return "running"
        if self.state in ("preparing", "shutting"):
            return "stopping"
        if any(DIAGNOSTICS[k][2] == MAR for k in self.diags):
            return "alarm"
        return "off"

    def codes(self) -> List[str]:
        return [f"{DIAGNOSTICS[k][2]} {DIAGNOSTICS[k][0] or ''} {DIAGNOSTICS[k][1]}".replace("  ", " ")
                for k in self.diags]

    def view_fields(self) -> List[Tuple[str, str]]:
        out = [("Running Mode", RUNNING_MODE[self.running_mode_code()]),
               ("Auto/Stop", f"BAS {'Auto' if self.auto_cmd else 'Stop'} · panel {'Auto' if self.front_panel_auto else 'Stop'}"),
               ("IGV", f"{self.igv:.0f} %"), ("% RLA", f"{self._pct_rla():.0f} %"),
               ("Evap", f"{r514a_psia(self.evap_sat) - P_ATM:+.1f} psig")]
        if self.restart_inhibit_left() > 0 and not self.running:
            out.append(("Restart inhibit", f"{self.restart_inhibit_left() / 60.0:.1f} min"))
        if self.evap_pump_req or self.cond_pump_req:
            out.append(("Pump requests", f"evap {'ON' if self.evap_pump_req else 'off'} · cond "
                                         f"{'ON' if self.cond_pump_req else 'off'}"))
        return out


@register
class TraneCVHEModbus(TraneCVHE):
    """The same chiller on Symbio 800 Modbus (32-bit floats, high word first)."""

    MODEL = "trane_cvhe_modbus"
    DISPLAY = "Trane CenTraVac CVHE (Symbio 800, Modbus)"
    SHORT = "Trane CVHE (Modbus)"
    PROTOCOL = "modbus_tcp"
    INTEGRATION = "Symbio 800 Modbus registers (BAS-PTS009C)"
    OBJECTS = TRANE_MODBUS_OBJECTS
