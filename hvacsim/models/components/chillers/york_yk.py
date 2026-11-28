"""
YORK YK centrifugal chiller with variable speed drive and OptiView control
center (form 160.75-O1), as seen through its BACnet gateway (YK OptiView BAS
points list Rev K_04d, tab "YK EM or SSS").

The chiller is a component: the plant model gives it water flows and
entering temperatures, the ALC writes reach it through the BACnet objects
in YK_OBJECTS (REM RUN STOP CMD, REM LEAVING CH LIQ TEMP SP, REM MOTOR
CURRENT SP), and it publishes the gateway's points and status codes.

Behaviour (manual + design review):
  * runs on BAS command only with the panel in Remote (ISN) and the panel
    switch in RUN; Local runs from the panel switch on the local setpoint
  * start needs the chiller's own flow switches made, anti-recycle (start to
    start) and the 150 s oil coastdown expired; 50 s oil pre-lube, VSD ramp,
    refrigerant level pulldown, then capacity control to the leaving
    chilled liquid setpoint (speed first, then pre-rotation vanes)
  * motor current limit, evaporator low pressure and condenser high
    pressure limits unload the chiller (warnings 4/5) before they trip
  * cycling shutdowns (flow switch, low leaving temperature, power failure)
    restart by themselves; safety shutdowns latch until the panel is reset
  * surge at low load and high lift: warning 30, then safety 53
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Set

from ...base import ParamInfo
from ..hydronic import r134a_psig
from ..vendor import register, state_texts  # noqa: F401  (state_texts kept importable from here)
from .base import Chiller

# ---------------------------------------------------------------------------
# Status code tables (BACnet MSV present value = code + 1)
# ---------------------------------------------------------------------------

OPERATION_CODES = {
    0: "Unit Stopped - Ready To Start", 1: "Unit Stopped - Local Shutdown", 2: "Unit Stopped - Remote Shutdown",
    3: "Unit Stopped - Warning Active", 4: "Unit Stopped - Cycling Shutdown", 5: "Unit Stopped - Safety Shutdown",
    6: "Unit Start Inhibit", 7: "Unit Starting", 8: "Unit Running - No Abnormal Condition",
    9: "Unit Running - Warning Active", 10: "Unit Running - Soft Shutdown", 11: "Unit Coastdown",
    12: "Unit Running - Modified Run", 13: "Unit Stopped - Internal Shutdown",
    14: "Unit Starting - MVVSD Startup - Drive Not Ready",
    **{i: "Reserved" for i in range(15, 21)},
    21: "Unit Running - Cooling - No Abnormal Condition", 22: "Unit Running - Cooling - Warning Active",
    23: "Unit Running - Cooling - Modified Run", 24: "Unit Running - Heating - No Abnormal Condition",
    25: "Unit Running - Heating - Warning Active", 26: "Unit Running - Heating - Modified Run",
    27: "Unit Running - Cooling & Heating - No Abnormal Condition",
    28: "Unit Running - Cooling & Heating - Warning Active", 29: "Unit Running - Cooling & Heating - Modified Run",
}
SAFETY_CODES = {
    0: "No Safety Faults Present", 1: "Evaporator - Low Pressure", 2: "Evaporator - Transducer Or Leaving Liquid Probe",
    3: "Evaporator - Transducer Or Temperature Sensor", 4: "Condenser - High Pressure Contacts Open",
    5: "Condenser - High Pressure", 6: "Condenser - Pressure Transducer Out Of Range",
    7: "Auxiliary Safety - Contacts Closed", 8: "Discharge - Low Temperature", 9: "Discharge - High Temperature",
    10: "Oil - High Sump Temperature", 11: "Oil - Low Differential Pressure", 12: "Oil - High Differential Pressure",
    19: "Safety Stop", 21: "Control Panel - Power Failure", 22: "Control Panel - Loss of Control Voltage",
    23: "Motor Or Starter - Current Imbalance", 29: "VSD - High Heatsink Temperature",
    30: "VSD - 105% Motor Current Overload", 52: "Evaporator - Low Pressure - Smart Freeze",
    53: "Surge Protection - Excess Surge", 61: "Motor - High Winding Temperature", 62: "Motor - High Bearing Temperature",
    77: "VSD - Control Fault", 104: "VSD - Ground Fault", 114: "Isolation Valves - Not Opened",
}
CYCLING_CODES = {
    0: "No Cycling Faults Present", 1: "Multiunit Cycling - Contacts Open", 2: "System Cycling - Contacts Open",
    3: "Oil - Low Temperature Differential", 4: "Oil - Low Sump Temperature", 5: "Control Panel - Power Failure",
    6: "Leaving Chilled Liquid - Low Temperature", 7: "Leaving Chilled Liquid - Flow Switch Open",
    8: "Condenser - Flow Switch Open", 9: "Motor Controller - Contacts Open", 10: "Motor Controller - Loss Of Current",
    11: "Power Fault", 12: "Control Panel - Schedule", 13: "Starter - Low Supply Line Voltage",
    14: "Starter - High Supply Line Voltage", 25: "VSD - Single Phase Input Power", 26: "VSD - High DC Bus Voltage",
    28: "VSD - Low DC Bus Voltage", 156: "Leaving Condenser Liquid - High Temperature",
    157: "Condenser - Freeze Threat - Flow Switch Open", 158: "Isolation Valves - Not Closed",
}
WARNING_CODES = {
    0: "No Warnings Present", 1: "Real-Time Clock Failure", 2: "Setpoint Override",
    3: "Condenser Or Evaporator Transducer Error", 4: "Evaporator - Low Pressure Limit",
    5: "Condenser - High Pressure Limit", 6: "WARNING - OIL - HIGH SUPPLY TEMPERATURE",
    9: "Standby Lube - Low Oil Pressure", 29: "Surge Protection - Excess Surge Limit", 30: "Excess Surge Detected",
    44: "VSD - Input Voltage Imbalance", 46: "Condenser- Freeze Threat From Low Pressure",
    47: "Liquid Level Setpoint Not Achieved", 48: "VSD - DC Bus Active",
}
CONTROL_SOURCE = {0: "Local", 1: "ISN (BAS)", 2: "Analog", 3: "Digital", 4: "Modem", 5: "Remote"}
MODIFIED_RUN_CODES = {
    0: "No Modified Run Conditions Present", 4: "Motor - Pulldown Limit", 5: "Motor - High Current Limit",
    6: "Motor Controller - Input Pulldown Limit", 7: "Motor Controller - High Input Power Limit",
    8: "Load Control Mode", 11: "VSD - Input Pulldown Limit", 12: "VSD - High Input Current Limit",
    13: "Discharge - Low Superheat Limit",
}
INHIBIT_CODES = {
    0: "No Inhibit Faults Present", 1: "Anti-Recycle", 2: "Vane Motor Switch Open", 3: "Motor Current > 15% FLA",
    8: "VSD - Frequency > 0 Hz", 10: "Line Voltage Not Set", 11: "Line Frequency Not Set",
    15: "PRV Not Calibrated",
}
INTERNAL_CODES = {
    0: "No Internal Faults Present", 1: "Internal Error - No Timer Handles Available",
    2: "Watchdog - Software Reboot", 3: "Internal Error - Cannot Create Task",
}


# ---------------------------------------------------------------------------
# BACnet gateway object list (YK OptiView BAS Rev K_04d, YK EM or SSS tab)
# kind, instance, object name, chiller key, units, role
# role: "cmd" = writable by the BAS, None = read only, ("msv", table, states)
# ---------------------------------------------------------------------------

_F, _PSI, _PCT = "degreesFahrenheit", "poundsForcePerSquareInch", "percent"
YK_OBJECTS = [
    ("analog-value", 1, "REM LEAVING CH LIQ TEMP SP", "rem_sp", _F, "cmd"),
    ("analog-value", 2, "REM MOTOR CURRENT SP", "rem_cur_limit", _PCT, "cmd"),
    ("binary-value", 1, "REM RUN STOP CMD", "rem_run", None, "cmd"),
    ("analog-value", 5, "LEAVING CH LIQ TEMP", "lchwt", _F, None),
    ("analog-value", 6, "ENTERING CH LIQ TEMP", "echwt", _F, None),
    ("analog-value", 7, "EVAPORATOR PRESSURE", "evap_p", _PSI, None),
    ("analog-value", 8, "CONDENSER PRESSURE", "cond_p", _PSI, None),
    ("analog-value", 9, "OIL DIFERENTIAL PRESSURE", "oil_dp", _PSI, None),
    ("analog-value", 10, "ENTERING COND WATER TEMP", "ecwt", _F, None),
    ("analog-value", 11, "LEAVING COND WATER TEMP", "lcwt", _F, None),
    ("analog-value", 12, "MOTOR CURRENT PCT FLA", "pct_fla", _PCT, None),
    ("analog-value", 19, "LEAVING CH LIQ TEMP ACT SP", "active_sp", _F, None),
    ("analog-value", 20, "MOTOR CURRENT LIMIT ACTIVE SP", "active_cur_limit", _PCT, None),
    ("analog-value", 21, "EVAP SATURATION TEMP", "evap_sat", _F, None),
    ("analog-value", 22, "COND SATURATION TEMP", "cond_sat", _F, None),
    ("analog-value", 23, "DISCHARGE TEMP", "disch_t", _F, None),
    ("analog-value", 24, "OIL SUMP TEMPERATURE", "oil_sump_t", _F, None),
    ("analog-value", 25, "COND REFRIGERANT LEVEL", "ref_level", _PCT, None),
    ("analog-value", 26, "OPERATING HOURS", "run_hours", "hours", None),
    ("analog-value", 27, "NUMBER OF STARTS", "starts", "noUnits", None),
    ("analog-value", 28, "OIL SUMP PRESSURE", "oil_sump_p", _PSI, None),
    ("analog-value", 29, "OIL PUMP PRESSURE", "oil_pump_p", _PSI, None),
    ("analog-value", 35, "ANTI RECYCLE TIME REMAIN", "anti_recycle", "minutes", None),
    ("analog-value", 112, "PRE ROTATION VANES POS", "prv", _PCT, None),
    ("analog-value", 114, "COND REFRIGERANT LEVEL SP", "ref_level_sp", _PCT, None),
    ("analog-value", 119, "INPUT POWER", "kw", "kilowatts", None),
    ("analog-value", 120, "INPUT KWH", "kwh", "kilowattHours", None),
    ("analog-value", 136, "VSD OIL PUMP CMD FREQUENCY", "oil_pump_hz", "hertz", None),
    # VSD tab of the BAS points list not provided: names and instances UNCONFIRMED
    ("analog-value", 901, "VSD OUTPUT FREQUENCY", "vsd_hz", "hertz", None),
    ("analog-value", 902, "VSD DC BUS VOLTAGE", "vsd_bus_v", "volts", None),
    ("binary-value", 5, "COMPR MOTOR STATUS", "motor_run", None, None),
    ("binary-value", 6, "LIQUID LINE SOLENOID", "liq_line_sol", None, None),
    ("binary-value", 7, "CH LIQ PUMP STAT", "chl_pump", None, None),
    ("binary-value", 8, "START STOP SWITCH STATUS", "panel_switch_run", None, None),
    ("binary-value", 9, "CH LIQ FLOW SWITCH STAT", "evap_fs", None, None),
    ("binary-value", 10, "CND FLOW SWITCH STAT", "cond_fs", None, None),
    ("multi-state-value", 1, "UNIT OPERATION CODE", "op_code", None, ("msv", OPERATION_CODES, 160)),
    ("multi-state-value", 2, "UNIT SAFETY FAULT CODE", "safety_code", None, ("msv", SAFETY_CODES, 300)),
    ("multi-state-value", 3, "UNIT CYCLING FAULT CODE", "cycling_code", None, ("msv", CYCLING_CODES, 300)),
    ("multi-state-value", 4, "UNIT WARNING CODE", "warning_code", None, ("msv", WARNING_CODES, 160)),
    ("multi-state-value", 5, "UNIT CONTROL SOURCE", "control_source", None, ("msv", CONTROL_SOURCE, 6)),
    ("multi-state-value", 6, "UNIT MODIFIED RUN CODE", "modrun_code", None, ("msv", MODIFIED_RUN_CODES, 140)),
    ("multi-state-value", 7, "UNIT START INHIBIT CODE", "inhibit_code", None, ("msv", INHIBIT_CODES, 160)),
    ("multi-state-value", 8, "UNIT INTERNAL FAULT CODE", "internal_code", None, ("msv", INTERNAL_CODES, 140)),
]

YK_FAULTS = {
    "evap_fs_failed": "Chilled liquid flow switch failed open",
    "cond_fs_failed": "Condenser flow switch failed open",
    "low_charge": "Low refrigerant charge",
    "cond_fouled": "Condenser tubes fouled (high approach)",
    "evap_fouled": "Evaporator tubes fouled",
    "vsd_fault": "VSD fault (control fault, trips)",
    "gateway_offline": "BACnet gateway offline",
}

# ---------------------------------------------------------------------------


@register
class YorkYK(Chiller):
    MODEL = "york_yk"
    MAKE = "YORK"
    DISPLAY = "YORK YK (OptiView)"
    SHORT = "YORK YK"
    PROTOCOL = "bacnet"
    INTEGRATION = "OptiView BAS gateway"
    BINARY_CMD_TEXT = ("RUN", "STOP")
    DEVICE_MODEL_NAME = "YORK YK OptiView (simulated)"
    DEFAULT_DESCRIPTION = "YORK YK VSD centrifugal chiller"
    OBJECTS = YK_OBJECTS
    ROLES = {"run_cmd": "rem_run", "chws_sp": "rem_sp", "demand_limit": "rem_cur_limit",
             "running": "motor_run", "alarm": None, "lchwt": "lchwt", "echwt": "echwt", "lcwt": "lcwt",
             "ecwt": "ecwt", "pct_load": "pct_fla", "kw": "kw", "ready": None, "local": None}
    PANEL = ["remote", "local", "run", "stop", "reset"]
    PANEL_PARAMS = {"local_sp": ParamInfo("", "Local leaving chilled liquid setpoint", "panel", "°F",
                                          minimum=38, maximum=70)}
    SETTINGS = {"anti_recycle_min": 30.0, "lchwt_cycling_offset": 4.0, "lchwt_restart_offset": 0.0}
    FAULTS = YK_FAULTS
    FAULT_ALIASES = {"starter_fault": "vsd_fault", "comm_loss": "gateway_offline"}
    CODE_TABLES = {"operation": OPERATION_CODES, "safety": SAFETY_CODES, "cycling": CYCLING_CODES,
                   "warning": WARNING_CODES, "inhibit": INHIBIT_CODES, "modified_run": MODIFIED_RUN_CODES}

    @classmethod
    def cmd_default(cls, key: str, cfg: Dict[str, Any]) -> Any:
        if key == "rem_run":
            return False
        return float(cfg.get("chws_design_temp", 44.0)) if key == "rem_sp" else 100.0

    def bas_write(self, key: str, value: Any, seen: bool = True) -> None:
        if key == "rem_run":
            self.rem_run = bool(value)
        elif key in ("rem_sp", "rem_cur_limit"):
            setattr(self, key, float(value))
        else:
            raise KeyError(f"{self.DISPLAY}: {key!r} is not a command object")

    PRELUBE_S = 50.0
    COASTDOWN_S = 150.0
    VSD_RAMP_S = 30.0
    LEVEL_PULLDOWN_S = 180.0

    def __init__(self, tag: str, cfg: Dict[str, Any], faults: Set[str], rng: Optional[Any] = None):
        self.tag = tag
        self.cfg = cfg
        self.faults = faults                      # active fault keys (owner keeps it in sync)
        self._init_core(cfg)
        st = self.settings_for(cfg)
        self.anti_recycle_s = float(st["anti_recycle_min"]) * 60.0
        self.cycle_offset = float(st["lchwt_cycling_offset"])
        self.restart_offset = float(st["lchwt_restart_offset"])
        self.comm_ok = True
        # panel
        self.panel_remote = True                 # Remote (ISN) vs Local
        self.panel_switch_run = True             # compressor switch in RUN (vs STOP/RESET)
        self.local_sp = self.sp_d
        # BACnet inputs (set by the owner from the gateway objects)
        self.rem_run = False
        self.rem_sp = self.sp_d
        self.rem_cur_limit = 100.0
        self.powered = True
        self.reset_state()

    # ------------------------------------------------------------------

    def reset_state(self) -> None:
        self.state = "stopped"                   # stopped, prelube, starting, running, soft_stop, coastdown
        self.timer = 0.0
        self.coast_left = 0.0
        self.since_start = 1e9
        self._reset_core()
        self.run_hours = 0.0
        self.starts = 0
        self.disch_t = 76.0
        self.oil_sump_t = 120.0
        self.oil_dp = 0.0
        self.ref_level = 0.0
        self.speed = 0.0
        self.prv = 0.0
        self.safety = 0
        self.cycling = 0
        self.warning = 0
        self.modrun = 0
        self.inhibit = 0
        self.surge_s = 0.0
        self.surging = False
        self.evap_low_s = 0.0
        self.evap_fs = False
        self.cond_fs = False
        self._efs_s = 0.0
        self._cfs_s = 0.0
        self.power_failed = False

    # ----- panel --------------------------------------------------------

    def panel(self, key: str) -> None:
        if key == "remote":
            self.panel_remote = True
        elif key == "local":
            self.panel_remote = False
        elif key == "run":
            self.panel_switch_run = True
        elif key in ("stop", "stop_reset"):
            self.panel_switch_run = False
            self._trip_motor()
        elif key == "reset":
            if self.safety and not self._safety_cause():
                self.safety = 0
            elif self.safety:
                raise ValueError(f"{self.tag}: safety cause still present ({SAFETY_CODES.get(self.safety)})")
        else:
            raise KeyError(key)

    # ----- helpers ------------------------------------------------------

    @property
    def setpoint(self) -> float:
        if self.panel_remote:
            return max(38.0, min(70.0, float(self.rem_sp)))
        return self.local_sp

    @property
    def current_limit(self) -> float:
        return max(30.0, min(100.0, float(self.rem_cur_limit))) if self.panel_remote else 100.0

    @property
    def run_request(self) -> bool:
        if not self.panel_switch_run:
            return False
        return bool(self.rem_run) if self.panel_remote else True

    @property
    def running(self) -> bool:
        return self.state in ("starting", "running", "soft_stop")

    @property
    def alarm(self) -> bool:
        return bool(self.safety)

    @property
    def active_setpoint(self) -> float:
        return self.setpoint

    def _safety_cause(self) -> bool:
        if self.safety == 77:
            return "vsd_fault" in self.faults
        if self.safety == 5:
            return r134a_psig(self.cond_sat) > 170.0
        return False

    def _trip_motor(self) -> None:
        if self.running or self.state == "prelube":
            if self.running:
                self.state = "coastdown"
                self.coast_left = self.COASTDOWN_S
            else:
                self.state = "stopped"
        self.cap = 0.0

    def _safety_trip(self, code: int) -> None:
        if not self.safety:
            self.safety = code
        self._trip_motor()

    def _cycle(self, code: int) -> None:
        self.cycling = code
        self._trip_motor()

    # ----- main step ----------------------------------------------------

    def step(self, h: float, evap_gpm: float, cond_gpm: float, ewt: float, ecwt: float, t_room: float = 75.0) -> None:
        self.evap_gpm, self.cond_gpm = max(0.0, evap_gpm), max(0.0, cond_gpm)
        self.echwt, self.ecwt = ewt, ecwt
        self.since_start += h
        self.warning = 0
        self.modrun = 0
        self.inhibit = 0

        # --- power ---
        if not self.powered:
            if self.state != "stopped" or self.cap > 0:
                self.state = "stopped"
            self.cap = 0.0
            self.power_failed = True
            self.coast_left = self.COASTDOWN_S
            self._thermal(h, t_room)
            self.kw = 0.0
            return
        if self.power_failed:
            self.cycling = 5                       # Control Panel - Power Failure (auto restart)
            self.power_failed = False

        # --- flow switches (paddle, about 50 % of design, 2 s debounce) ---
        e_ok = self.evap_gpm > (0.45 if self.evap_fs else 0.50) * self.chw_gpm_d and "evap_fs_failed" not in self.faults
        c_ok = self.cond_gpm > (0.45 if self.cond_fs else 0.50) * self.cw_gpm_d and "cond_fs_failed" not in self.faults
        self._efs_s = self._efs_s + h if e_ok != self.evap_fs else 0.0
        self._cfs_s = self._cfs_s + h if c_ok != self.cond_fs else 0.0
        if self._efs_s >= 2.0:
            self.evap_fs, self._efs_s = e_ok, 0.0
        if self._cfs_s >= 2.0:
            self.cond_fs, self._cfs_s = c_ok, 0.0

        # --- trips that act while running ---
        if self.running or self.state == "prelube":
            if "vsd_fault" in self.faults and self.running:
                self._safety_trip(77)
            elif not self.evap_fs:
                self._cycle(7)
            elif not self.cond_fs:
                self._cycle(8)

        # --- clear cycling shutdowns whose cause has gone ---
        sp = self.setpoint
        if self.cycling == 7 and self.evap_fs:
            self.cycling = 0
        if self.cycling == 8 and self.cond_fs:
            self.cycling = 0
        if self.cycling == 6 and self.lchwt >= sp + self.restart_offset:
            self.cycling = 0
        if self.cycling == 5 and (self.running or not self.run_request):
            self.cycling = 0

        # --- state machine ---
        if self.state == "coastdown":
            self.coast_left -= h
            if self.coast_left <= 0:
                self.state, self.coast_left = "stopped", 0.0
        elif self.coast_left > 0:
            self.coast_left = max(0.0, self.coast_left - h)

        if self.state == "stopped":
            if self.run_request and not self.safety:
                if self.cycling == 0:
                    if not self.evap_fs:
                        self.cycling = 7
                    elif not self.cond_fs:
                        self.cycling = 8
                if self.cycling in (0, 5) and self.evap_fs and self.cond_fs:
                    ar = max(0.0, self.anti_recycle_s - self.since_start)
                    if ar > 0:
                        self.inhibit = 1
                    elif self.coast_left <= 0:
                        self.state, self.timer = "prelube", 0.0
        elif self.state == "prelube":
            if not self.run_request or self.safety:
                self.state = "stopped"
            else:
                self.timer += h
                if self.timer >= self.PRELUBE_S:
                    self.state, self.timer = "starting", 0.0
                    self.starts += 1
                    self.since_start = 0.0
                    self.cycling = 0
        elif self.state == "starting":
            self.timer += h
            self.speed = min(1.0, self.timer / self.VSD_RAMP_S) * 0.6
            self.cap = self.min_load * min(1.0, self.timer / self.VSD_RAMP_S)
            if not self.run_request:
                self.state = "soft_stop"
            elif self.timer >= self.VSD_RAMP_S:
                self.state, self.timer = "running", 0.0
        elif self.state == "running":
            self.timer += h
            if not self.run_request:
                self.state = "soft_stop"
            else:
                self._capacity_control(h, sp)
        elif self.state == "soft_stop":
            if self.run_request:
                self.state = "running"
            else:
                self.cap = max(0.0, self.cap - 0.02 * h)          # unload, then stop the motor
                if self.cap <= self.min_load:
                    self.state, self.coast_left, self.cap = "coastdown", self.COASTDOWN_S, 0.0

        self._thermal(h, t_room)

        # --- protective limits and trips ---
        if self.running:
            evap_p = r134a_psig(self.evap_sat)
            cond_p = r134a_psig(self.cond_sat)
            if self.evap_sat < 31.5:
                self.evap_low_s += h
                if self.evap_low_s > 5.0:
                    self._safety_trip(1)
            else:
                self.evap_low_s = 0.0
            if cond_p > 180.0:
                self._safety_trip(5)
            if self.running and self.lchwt < sp - self.cycle_offset:
                self._cycle(6)
            if self.surging:
                self.surge_s += h
                self.warning = 30
                if self.surge_s > 300.0:
                    self._safety_trip(53)
            else:
                self.surge_s = max(0.0, self.surge_s - 0.2 * h)
            if self.warning == 0:
                if evap_p < r134a_psig(33.5):
                    self.warning = 4
                elif cond_p > 165.0:
                    self.warning = 5
            self.run_hours += h / 3600.0
        else:
            self.surging = False
            self.evap_low_s = 0.0
        self.kwh += self.kw * h / 3600.0

    def _capacity_control(self, h: float, sp: float) -> None:
        """Speed / PRV capacity control to the leaving chilled liquid setpoint, with limits."""
        err = self.lchwt - sp
        rate = max(-0.01, min(0.004, 0.0025 * err))          # per second; load slowly, unload faster
        limit_cap = 1.08
        # motor current limit
        fla = self._pct_fla()
        if fla > self.current_limit:
            rate = min(rate, -0.004)
            self.modrun = 5
        elif fla > self.current_limit - 2:
            rate = min(rate, 0.0)
            self.modrun = 5
        # evaporator low pressure / condenser high pressure limits (hold, then unload)
        if self.evap_sat < 33.5:
            rate = min(rate, -0.003)
        if r134a_psig(self.cond_sat) > 165.0:
            rate = min(rate, -0.003)
        # vanes fully closed at minimum speed: capacity cannot drop below the minimum
        self.cap = max(self.min_load, min(limit_cap, self.cap + rate * h))
        # capacity cannot exceed what the water can give (LCHWT stays above the refrigerant)
        q_max = 500.0 * self.evap_gpm * max(0.0, self.echwt - 34.0)
        if self.cap * self.capacity * 12000.0 > q_max:
            self.cap = max(self.min_load * 0.0, q_max / (self.capacity * 12000.0))
        # VSD speed: just enough for the lift, then vanes take over below that
        lift_ratio = max(0.2, self.lift / self.lift_d)
        speed_min = max(0.55, math.sqrt(lift_ratio) * 0.92)
        self.speed = max(speed_min, min(1.0, speed_min + (1.0 - speed_min) * max(0.0, self.cap - 0.5) / 0.5))
        self.prv = max(0.0, min(100.0, 100.0 * self.cap / max(0.2, self.speed ** 2)))
        # surge: high lift at low load
        surge_line = self.lift_d * (0.75 + 0.45 * self.cap)
        self.surging = self.lift > surge_line

    def _pct_fla(self) -> float:
        return 100.0 * self.kw / (self.kw_d * 1.08)

    def _thermal(self, h: float, t_room: float) -> None:
        load = self._core_thermal(h, t_room, coasting=self.state == "coastdown")
        # oil, discharge, level
        if self.running:
            self.oil_dp += (42.0 - self.oil_dp) * min(1.0, h / 10.0)
            sump_target = 135.0
            self.disch_t += (self.cond_sat + 10.0 + 8.0 * load - self.disch_t) * min(1.0, h / 60.0)
            lvl_sp = 50.0
            self.ref_level += (lvl_sp - self.ref_level) * min(1.0, h / (self.LEVEL_PULLDOWN_S / 3.0))
        else:
            target = 28.0 if self.state in ("prelube", "coastdown") else 0.0
            self.oil_dp += (target - self.oil_dp) * min(1.0, h / 8.0)
            sump_target = max(110.0, min(160.0, self.cond_sat + 18.0))
            self.disch_t += (self.cond_sat - self.disch_t) * min(1.0, h / 600.0)
            self.ref_level += (0.0 - self.ref_level) * min(1.0, h / 120.0)
            self.speed = 0.0 if not self.running else self.speed
            self.prv = 0.0
        self.oil_sump_t += (sump_target - self.oil_sump_t) * min(1.0, h / 900.0)

    # ----- published gateway values -------------------------------------

    def op_code(self) -> int:
        if not self.powered:
            return 13
        if self.state == "prelube" or self.state == "starting":
            return 7
        if self.state == "soft_stop":
            return 10
        if self.state == "coastdown":
            return 11
        if self.state == "running":
            if self.modrun:
                return 12
            if self.warning:
                return 9
            return 8
        if self.safety:
            return 5
        if self.cycling:
            return 4
        if self.inhibit and self.run_request:
            return 6
        if not self.panel_switch_run:
            return 1
        if self.panel_remote and not self.rem_run:
            return 2
        return 3 if self.warning else 0

    def points(self) -> Dict[str, Any]:
        running = self.running
        evap_p = r134a_psig(self.evap_sat)
        cond_p = r134a_psig(self.cond_sat)
        return {
            "rem_sp": float(self.rem_sp),
            "rem_cur_limit": float(self.rem_cur_limit),
            "rem_run": bool(self.rem_run),
            "lchwt": round(self.lchwt, 1),
            "echwt": round(self.echwt, 1),
            "evap_p": round(evap_p, 1),
            "cond_p": round(cond_p, 1),
            "oil_dp": round(self.oil_dp, 1),
            "ecwt": round(self.ecwt, 1),
            "lcwt": round(self.lcwt, 1),
            "pct_fla": round(self._pct_fla(), 1),
            "active_sp": round(self.setpoint, 1),
            "active_cur_limit": round(self.current_limit, 0),
            "evap_sat": round(self.evap_sat, 1),
            "cond_sat": round(self.cond_sat, 1),
            "disch_t": round(self.disch_t, 1),
            "oil_sump_t": round(self.oil_sump_t, 1),
            "ref_level": round(self.ref_level, 0),
            "run_hours": round(self.run_hours, 1),
            "starts": float(self.starts),
            "oil_sump_p": round(evap_p + 2.0, 1),
            "oil_pump_p": round(evap_p + 2.0 + self.oil_dp, 1),
            "anti_recycle": round(max(0.0, self.anti_recycle_s - self.since_start) / 60.0, 0)
            if self.since_start < 1e8 else 0.0,
            "prv": round(self.prv, 0),
            "ref_level_sp": 50.0,
            "kw": round(self.kw, 0),
            "kwh": round(self.kwh, 0),
            "oil_pump_hz": 55.0 if (running or self.state in ("prelube", "coastdown")) else 0.0,
            "vsd_hz": round(self.speed * 60.0, 1) if running else 0.0,
            "vsd_bus_v": 650.0 if self.powered else 0.0,
            "motor_run": running,
            "liq_line_sol": running,
            "chl_pump": self.run_request or self.state in ("prelube", "coastdown") or running,
            "panel_switch_run": self.panel_switch_run,
            "evap_fs": self.evap_fs,
            "cond_fs": self.cond_fs,
            "op_code": self.op_code(),
            "safety_code": self.safety,
            "cycling_code": self.cycling,
            "warning_code": self.warning,
            "control_source": 1 if self.panel_remote else 0,
            "modrun_code": self.modrun,
            "inhibit_code": self.inhibit,
            "internal_code": 0,
        }

    def status_text(self) -> str:
        code = self.op_code()
        txt = OPERATION_CODES.get(code, str(code))
        if self.safety:
            txt += f" · SAFETY {self.safety} {SAFETY_CODES.get(self.safety, '')}"
        elif self.cycling:
            txt += f" · CYCLING {self.cycling} {CYCLING_CODES.get(self.cycling, '')}"
        if self.warning:
            txt += f" · WARNING {self.warning} {WARNING_CODES.get(self.warning, '')}"
        if self.inhibit:
            txt += f" · INHIBIT {INHIBIT_CODES.get(self.inhibit, '')}"
        if self.modrun:
            txt += f" · {MODIFIED_RUN_CODES.get(self.modrun, '')}"
        return txt

    # ----- interface: generic status and page fields ----------------------

    def generic_state(self) -> str:
        if not self.powered:
            return "no_power"
        if self.state in ("prelube", "starting"):
            return "starting"
        if self.state == "running":
            return "running"
        if self.state in ("soft_stop", "coastdown"):
            return "stopping"
        if self.safety:
            return "lockout"
        if self.cycling:
            return "alarm"
        return "off" if self.panel_remote else "local"

    def codes(self) -> list:
        out = []
        for label, code, table in (("SAFETY", self.safety, SAFETY_CODES), ("CYCLING", self.cycling, CYCLING_CODES),
                                   ("WARNING", self.warning, WARNING_CODES)):
            if code:
                out.append(f"{label} {code} {table.get(code, '')}".strip())
        return out

    def view_fields(self) -> list:
        ar = max(0.0, self.anti_recycle_s - self.since_start) / 60.0 if self.since_start < 1e8 else 0.0
        out = [("Control", f"{'Remote (ISN)' if self.panel_remote else 'LOCAL'} · switch {'RUN' if self.panel_switch_run else 'STOP'}")]
        if ar > 0 and not self.running:
            out.append(("Anti-recycle", f"{ar:.0f} min"))
        return out + [("%FLA", f"{self._pct_fla():.0f} %"), ("Lift", f"{self.lift:.1f} °F"),
                ("PRV", f"{self.prv:.0f} %"), ("VSD", f"{self.speed * 60.0:.1f} Hz" if self.running else "0 Hz")]
