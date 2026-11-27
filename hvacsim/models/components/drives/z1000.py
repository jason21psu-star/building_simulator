"""
Yaskawa Z1000 HVAC drive behind the drive interface.

Parameter numbers, settings and defaults follow the Z1000 Programming Manual
SIEP YAIZ1U 03C (chapter 1, parameter details) and the User Manual
TOEP C710616 45H (3.9 control terminals, 5.3 faults and alarms).  The BACnet
object list is in configs/templates/vfd_z1000_bacnet.yaml (Programming Manual
B.7).  Values not from the manuals are marked (A) and in docs/VFD_Z1000.md.

Terminals: S1-S7 multi-function inputs (H1-01..07), A1 / A2 analog inputs
(H3), MA/MB/MC fault relay, M1-M2 / M3-M4 / M5-M6 relays (H2-01..03), FM / AM
analog monitors (H4).  Default layout = HVAC Initialization (A1-03 = 3410):
S1 Forward run, S2 Reverse run, S3 Customer Safeties (B1), S4 BAS Interlock
(B2), S5 Emergency Override Fwd (AF), M5-M6 BAS Interlock Relay Contact (B2).

Keypad: HOA (HAND / OFF / AUTO); S5-04 = 1 powers up in AUTO.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Set

from ...base import ParamInfo, SignalDef
from .base import GENERIC_FAULTS, Drive, register

_B, _A = "binary", "analog"
S = [f"s{i}" for i in range(1, 8)]

FAULT_TEXT = {
    "oC": "Overcurrent", "GF": "Ground Fault", "ov": "Overvoltage", "Uv1": "DC Bus Undervoltage",
    "PF": "Input Phase Loss", "LF": "Output Phase Loss", "oH1": "Heatsink Overheat", "oL1": "Motor Overload",
    "oL2": "Drive Overload", "CE": "MEMOBUS/Modbus Communication Error",
    **{f"EF{i}": f"External Fault (input terminal S{i})" for i in range(1, 8)},
}
ALARM_TEXT = {
    "oH": "Heatsink Overheat", "FAn": "Internal Fan Fault", "CE": "Communication Error", "inTLK": "Interlock Open",
    "SAFE": "Customer Safety", "dnE": "Drive Disabled", "Uv": "Undervoltage", "bb": "Drive Baseblock",
    "ov": "Overvoltage", **{f"EF{i}": f"External Fault (input terminal S{i})" for i in range(1, 8)},
}

# H1 multi-function digital input settings simulated (Programming Manual table 1.28)
H1_CHOICES = ["F", "3", "4", "6", "14", "40", "41", "6A", "70", "AF", "B1", "B2",
              *[f"2{c}" for c in "0123456789ABCDEF"]]
# H2 multi-function output settings simulated (table 1.29); 1xx = inverse
H2_BASE = ["0", "1", "2", "6", "E", "10", "38", "B2"]
H2_CHOICES = H2_BASE + [f"1{c:0>2}" for c in H2_BASE]
H4_CHOICES = [0, 102, 103, 108]          # monitor U1-oo: 0 not used, 102 output freq, 103 output current, 108 output power

Z1000_DEFAULTS: Dict[str, Any] = {
    # b1 operation mode selection
    "b1_01": 1,                 # Frequency Reference Selection for AUTO Mode: 0 keypad, 1 terminals, 2 serial
    "b1_02": 1,                 # Run Command Selection for AUTO Mode: 1 terminals, 2 serial
    "b1_03": 1,                 # Stopping Method: 0 ramp, 1 coast
    "b1_17": 1,                 # Run Command at Power Up: 0 disregarded, 1 allowed
    # C1 accel / decel
    "c1_01": 30.0,              # Acceleration Time 1, s (0 -> E1-04)
    "c1_02": 30.0,              # Deceleration Time 1, s
    # d frequency references and limits
    "d1_04": 0.0,               # Frequency Reference 4, Hz (used by H5-04 = 4)
    "d2_01": 100.0,             # Frequency Reference Upper Limit, % of E1-04
    "d2_02": 0.0,               # Frequency Reference Lower Limit, % of E1-04
    # E motor
    "e1_04": 60.0,              # Maximum Output Frequency, Hz
    "e1_05": 460.0,             # Maximum Voltage, V
    "e2_01": 14.0,              # Motor Rated Current, A
    "e2_11": 7.5,               # Motor Rated Power, kW
    # H1 digital inputs (HVAC Initialization layout)
    "h1_01": "40", "h1_02": "41", "h1_03": "B1", "h1_04": "B2", "h1_05": "AF", "h1_06": "4", "h1_07": "6",
    # H2 relay outputs
    "h2_01": "0",               # M1-M2: During Run
    "h2_02": "1",               # M3-M4: Zero Speed
    "h2_03": "B2",              # M5-M6: BAS Interlock Relay Contact (HVAC Initialization)
    # H3 analog inputs
    "h3_01": 0,                 # A1 Signal Level: 0 0-10 V (zero limit), 1 0-10 V, 2 4-20 mA, 3 0-20 mA
    "h3_03": 100.0,             # A1 Gain, % of E1-04 at 10 V
    "h3_04": 0.0,               # A1 Bias, % of E1-04 at 0 V
    "h3_09": 2,                 # A2 Signal Level: 2 4-20 mA
    "h3_10": "0",               # A2 Function: 0 frequency bias (added), F not used
    "h3_11": 100.0,             # A2 Gain, %
    # H4 analog monitors
    "h4_01": 102, "h4_02": 100.0,   # FM: output frequency
    "h4_04": 103, "h4_05": 100.0,   # AM: output current
    # H5 serial communication (BACnet)
    "h5_04": 3,                 # Stopping Method after CE: 0 ramp, 1 coast, 2 fast stop, 3 alarm only, 4 run at d1-04
    "h5_05": 1,                 # CE Detection: 0 disabled, 1 enabled
    "h5_09": 2.0,               # CE Detection Time, s
    # L protection
    "l2_01": 2,                 # Momentary Power Loss: 0 disabled, 1 recover within L2-02, 2 recover while CPU has power
    "l3_02": 120.0,             # Stall Prevention Level during Accel, % of drive rated current
    "l3_04": 1,                 # Stall Prevention during Decel: 0 disabled (ov trips), 1 enabled
    "l5_01": 0,                 # Number of Auto Restart Attempts
    "l8_02": 95.0,              # Overheat Alarm Level, °C (A: default depends on o2-04)
    # S HVAC functions
    "s5_01": 0,                 # HAND Frequency Reference: 0 keypad, 1 terminals, 3 S5-05, 4 b1-01
    "s5_04": 1,                 # HAND Mode Behavior: 1 normal (powers up in AUTO)
    "s5_05": 0.0,               # HAND Frequency Reference 1, Hz
    "s6_01": 0.0,               # Emergency Override Speed, Hz
    "s6_02": 0,                 # Emergency Override Reference: 0 S6-01, 1 the AUTO reference
    # not drive parameters (simulator)
    "drive_rated_amps": 16.3,   # drive rated output current (n9-01 / o2-04) (A)
    "motor_rated_rpm": 1770.0,  # motor nameplate speed (A)
    "inertia_h": 6.0,           # s, motor + fan inertia constant
    "regen_absorb_pu": 0.12,
    "line_voltage": 480.0,
    "ambient_temp": 30.0,
    "comm_loss_mode": "any_message",   # what keeps CE away: any BACnet request to the drive
}

_ONOFF = [0, 1]
Z1000_PARAM_INFO: Dict[str, ParamInfo] = {
    "b1_01": ParamInfo("b1-01", "Frequency Ref Sel AUTO (0 keypad, 1 terminals, 2 serial)", "b Application", choices=[0, 1, 2]),
    "b1_02": ParamInfo("b1-02", "Run Command Sel AUTO (1 terminals, 2 serial)", "b Application", choices=[1, 2]),
    "b1_03": ParamInfo("b1-03", "Stopping Method (0 ramp, 1 coast)", "b Application", choices=[0, 1]),
    "b1_17": ParamInfo("b1-17", "Run Command at Power Up (0 disregard, 1 allow)", "b Application", choices=_ONOFF),
    "c1_01": ParamInfo("C1-01", "Acceleration Time 1", "C Tuning", "s", minimum=0.1, maximum=6000),
    "c1_02": ParamInfo("C1-02", "Deceleration Time 1", "C Tuning", "s", minimum=0.1, maximum=6000),
    "d1_04": ParamInfo("d1-04", "Frequency Reference 4", "d References", "Hz", minimum=0, maximum=240),
    "d2_01": ParamInfo("d2-01", "Frequency Ref Upper Limit", "d References", "%", minimum=0, maximum=110),
    "d2_02": ParamInfo("d2-02", "Frequency Ref Lower Limit", "d References", "%", minimum=0, maximum=110),
    "e1_04": ParamInfo("E1-04", "Maximum Output Frequency", "E Motor", "Hz", minimum=40, maximum=240),
    "e1_05": ParamInfo("E1-05", "Maximum Voltage", "E Motor", "V", minimum=0, maximum=733),
    "e2_01": ParamInfo("E2-01", "Motor Rated Current", "E Motor", "A", minimum=0.5, maximum=900),
    "e2_11": ParamInfo("E2-11", "Motor Rated Power", "E Motor", "kW", minimum=0, maximum=650),
    **{f"h1_0{i}": ParamInfo(f"H1-0{i}", f"Terminal S{i} Function", "H Terminals", choices=H1_CHOICES) for i in range(1, 8)},
    "h2_01": ParamInfo("H2-01", "Terminal M1-M2 Function", "H Terminals", choices=H2_CHOICES),
    "h2_02": ParamInfo("H2-02", "Terminal M3-M4 Function", "H Terminals", choices=H2_CHOICES),
    "h2_03": ParamInfo("H2-03", "Terminal M5-M6 Function", "H Terminals", choices=H2_CHOICES),
    "h3_01": ParamInfo("H3-01", "A1 Signal Level (0 0-10V lim, 1 0-10V, 2 4-20mA, 3 0-20mA)", "H Terminals", choices=[0, 1, 2, 3]),
    "h3_03": ParamInfo("H3-03", "A1 Gain", "H Terminals", "%", minimum=-999.9, maximum=999.9),
    "h3_04": ParamInfo("H3-04", "A1 Bias", "H Terminals", "%", minimum=-999.9, maximum=999.9),
    "h3_09": ParamInfo("H3-09", "A2 Signal Level (0 0-10V lim, 1 0-10V, 2 4-20mA, 3 0-20mA)", "H Terminals", choices=[0, 1, 2, 3]),
    "h3_10": ParamInfo("H3-10", "A2 Function (0 freq bias, F not used)", "H Terminals", choices=["0", "F"]),
    "h3_11": ParamInfo("H3-11", "A2 Gain", "H Terminals", "%", minimum=-999.9, maximum=999.9),
    "h4_01": ParamInfo("H4-01", "FM Monitor (102 freq, 103 current, 108 power)", "H Terminals", choices=H4_CHOICES),
    "h4_02": ParamInfo("H4-02", "FM Gain", "H Terminals", "%", minimum=-999.9, maximum=999.9),
    "h4_04": ParamInfo("H4-04", "AM Monitor (102 freq, 103 current, 108 power)", "H Terminals", choices=H4_CHOICES),
    "h4_05": ParamInfo("H4-05", "AM Gain", "H Terminals", "%", minimum=-999.9, maximum=999.9),
    "h5_04": ParamInfo("H5-04", "Stop after CE (0 ramp, 1 coast, 2 fast, 3 alarm, 4 run d1-04)", "H Terminals", choices=[0, 1, 2, 3, 4]),
    "h5_05": ParamInfo("H5-05", "CE Detection (0 off, 1 on)", "H Terminals", choices=_ONOFF),
    "h5_09": ParamInfo("H5-09", "CE Detection Time", "H Terminals", "s", minimum=0, maximum=10),
    "l2_01": ParamInfo("L2-01", "Momentary Power Loss (0 off, 1 within L2-02, 2 while CPU)", "L Protection", choices=[0, 1, 2]),
    "l3_02": ParamInfo("L3-02", "Stall Prevention Level Accel", "L Protection", "%", minimum=0, maximum=150),
    "l3_04": ParamInfo("L3-04", "Stall Prevention Decel (0 off, 1 on)", "L Protection", choices=_ONOFF),
    "l5_01": ParamInfo("L5-01", "Auto Restart Attempts", "L Protection", minimum=0, maximum=10),
    "l8_02": ParamInfo("L8-02", "Overheat Alarm Level", "L Protection", "°C", minimum=50, maximum=130),
    "s5_01": ParamInfo("S5-01", "HAND Ref (0 keypad, 1 terminals, 3 S5-05, 4 b1-01)", "S HVAC", choices=[0, 1, 3, 4]),
    "s5_04": ParamInfo("S5-04", "HAND Mode Behavior (1 normal)", "S HVAC", choices=[0, 1, 2]),
    "s5_05": ParamInfo("S5-05", "HAND Frequency Reference 1", "S HVAC", "Hz", minimum=0, maximum=240),
    "s6_01": ParamInfo("S6-01", "Emergency Override Speed", "S HVAC", "Hz", minimum=0, maximum=240),
    "s6_02": ParamInfo("S6-02", "Emergency Override Ref (0 S6-01, 1 AUTO ref)", "S HVAC", choices=_ONOFF),
}


@register
class Z1000(Drive):
    MODEL = "z1000"
    MAKE = "Yaskawa"
    DISPLAY = "Yaskawa Z1000"
    SHORT = "Z1000"
    DEFAULTS = Z1000_DEFAULTS
    PARAM_INFO = Z1000_PARAM_INFO
    INJECTABLE = {**GENERIC_FAULTS, "output_phase_loss": "Output phase loss (motor lead open)"}
    FAULT_TEXT = FAULT_TEXT
    TERMINALS = {"di": S, "ai": ["a1", "a2"], "relay": ["ma", "mb", "m1", "m3", "m5"], "ao": ["fm", "am"]}
    ROLE_TERMINALS = {"start": "S1 (H1-01 = 40)", "safeties": "S3 (H1-03 = B1)", "damper_interlock": "S4 (H1-04 = B2)",
                      "speed_ref": "A1", "running_out": "M1-M2 (H2-01 = 0)", "fault_out": "MB-MC",
                      "speed_fb": "FM (H4-01 = 102)", "damper_out": "M5-M6 (H2-03 = B2)"}
    PRESETS = {"hardwired": {}}             # the HVAC Initialization defaults are the hardwired layout
    SPEED_WIRE_FAULT = ("a1_wire_broken", "A1 speed signal wire broken")

    def __init__(self, params: Dict[str, Any], inject: Optional[Set[str]] = None):
        self.p = params
        self.inject: Set[str] = inject if inject is not None else set()
        self.mode = "auto"
        self.hand_ref = 30.0
        self.powered = True
        self.di: Dict[str, bool] = {t: False for t in S}
        self.di.update(s3=True, s4=True)    # safeties and BAS interlock wired closed
        self.a1_pct = 0.0                   # % of 10 V (or of 4-20 mA span)
        self.a2_pct = 0.0
        self.fb = {"run": False, "freq_hz": 0.0, "fault_reset": False, "ext_fault": False,
                   **{t: False for t in S[2:]}}          # BV7-BV11: MF Input 3-7 Cmd (virtual S3-S7)
        self.fb_idle_s = 0.0
        self.reset_state()

    @classmethod
    def map_common(cls, common: Dict[str, Any]) -> Dict[str, Any]:
        super().map_common(common)
        out: Dict[str, Any] = {}
        emax = cls.DEFAULTS["e1_04"]
        for k, v in common.items():
            if k == "motor_amps":
                out["e2_01"] = v
            elif k == "motor_kw":
                out["e2_11"] = v
            elif k == "max_amps":                 # same available current as the drive it replaces
                out["drive_rated_amps"] = round(float(v) / 1.2, 1)
            elif k == "inertia_h":
                out["inertia_h"] = v
            elif k == "min_hz":
                out["d2_02"] = round(100.0 * float(v) / emax, 2)
            elif k == "accel_s":
                out["c1_01"] = v
            elif k == "decel_s":
                out["c1_02"] = v
        return out

    def reset_state(self) -> None:
        p = self.p
        self.freq = 0.0
        self.shaft = 0.0
        self.modulating = False
        self.fault_code = ""
        self.fault_history: List[str] = []
        self.alarms: List[str] = []
        self.warnings: List[str] = []
        self.current = self.torque_pu = self.power_kw = 0.0
        self.bus_v = 1.35 * p["line_voltage"]
        self.drive_temp = self.motor_temp = p["ambient_temp"]
        self.kwh = self.run_hours = 0.0
        self.ref_hz = 0.0
        self.target = 0.0
        self.run_cmd = False
        self.baseblock = False
        self.override = False
        self.need_cycle = False
        self.ol1 = self.ol2 = 0.0
        self._off_s = 0.0
        self._was_modulating_at_loss = False
        self._last_reset = False
        self._last_fb_reset = False
        self._ce = False

    # ---------------------------------------------------------------- terminals by function
    def _terminals(self, code: str) -> List[str]:
        return [t for i, t in enumerate(S, start=1) if str(self.p[f"h1_0{i}"]).upper() == code]

    def _closed(self, t: str) -> bool:
        return bool(self.di.get(t)) or bool(self.fb.get(t, False))

    def _func(self, code: str, default: Optional[bool] = None) -> Optional[bool]:
        """State of a digital input function (any terminal set to it closed); default when unassigned."""
        ts = self._terminals(code)
        if not ts:
            return default
        return any(self._closed(t) for t in ts)

    # ---------------------------------------------------------------- roles
    def wire(self, start: bool = False, safeties: bool = True, damper_interlock: bool = True,
             speed_ref: float = 0.0) -> None:
        for t in S:
            self.di[t] = False
        for t in self._terminals("40"):
            self.di[t] = bool(start)
        for t in self._terminals("B1"):
            self.di[t] = bool(safeties)
        for t in self._terminals("B2"):
            self.di[t] = bool(damper_interlock)
        for code in ("6A", "70"):                  # drive enable inputs, when assigned, are jumpered
            for t in self._terminals(code):
                self.di[t] = True
        self.a1_pct = float(speed_ref)

    def role_out(self, role: str):
        p = self.p
        if role == "running_out":
            return self._h2(p["h2_01"])
        if role == "fault_out":
            return self.relay("mb")                 # MB-MC: N.C., opens on fault -> closed = healthy
        if role == "damper_out":
            return self._h2(p["h2_03"])
        if role == "speed_fb":
            return self.analog_out("fm")
        raise KeyError(role)

    # ---------------------------------------------------------------- keypad
    def keypad(self, key: str, value: Any = None) -> None:
        if key in ("hand", "off", "auto"):
            self.mode = key
        elif key == "reset":
            self.fault_reset()
        elif key == "hand_ref":
            self.hand_ref = max(0.0, min(float(value), self.p["e1_04"]))
        else:
            raise KeyError(key)

    def fault_reset(self) -> None:
        """Fault reset is ignored while a Run command is present (Programming Manual, setting 14)."""
        if self.fault_code and not self.run_cmd and not self._fault_cause_present():
            self.fault_code = ""

    def _fault_cause_present(self) -> bool:
        if "earth_leakage" in self.inject or "output_phase_loss" in self.inject:
            return True
        if self.fault_code.startswith("EF") and self._ext_fault_active():
            return True
        return self.drive_temp > self.p["l8_02"] + 10.0

    def _trip(self, code: str) -> None:
        if self.fault_code:
            return
        self.fault_code = code
        self.fault_history.insert(0, code)
        del self.fault_history[3:]

    def _alarm(self, code: str) -> None:
        if code not in self.alarms:
            self.alarms.append(code)

    # ---------------------------------------------------------------- analog in
    def _ai_frac(self, pct: float, level: int) -> float:
        x = max(0.0, min(100.0, pct)) / 100.0
        if level == 2:                              # 4-20 mA: below 4 mA reads 0 % (zero limit)
            return max(0.0, (x - 0.2) / 0.8)
        return x

    def _a1_hz(self) -> float:
        p = self.p
        frac = self._ai_frac(self.a1_pct, int(p["h3_01"]))
        hz = (p["h3_04"] + frac * p["h3_03"]) / 100.0 * p["e1_04"]
        if str(p["h3_10"]) == "0":                  # A2 frequency bias (added)
            hz += self._ai_frac(self.a2_pct, int(p["h3_09"])) * p["h3_11"] / 100.0 * p["e1_04"]
        if int(p["h3_01"]) == 0:
            hz = max(0.0, hz)
        return hz

    def _ext_fault_active(self) -> Optional[str]:
        """Terminal of an active external fault input (H1 = 20-2F), or None."""
        for i, t in enumerate(S, start=1):
            code = str(self.p[f"h1_0{i}"]).upper()
            if len(code) == 2 and code[0] == "2":
                x = int(code, 16) - 0x20
                normally_open = x % 2 == 0
                during_run = (x // 2) % 2 == 1
                closed = self._closed(t)
                trig = closed if normally_open else not closed
                if trig and (not during_run or self.modulating):
                    return t
        if self.fb.get("ext_fault"):
            return "s0"
        return None

    # ---------------------------------------------------------------- step
    def step(self, h: float, load_torque: Callable[[float], float]) -> None:
        p = self.p
        f_max = p["e1_04"]
        self.alarms = []
        if not self.powered:
            self._off_s += h
            if self._off_s <= h:
                self._was_modulating_at_loss = self.modulating
            s = self.shaft
            H = max(0.1, p["inertia_h"])
            self.shaft = max(0.0, s - (max(0.0, load_torque(s)) + 0.01) / (2 * H * max(s, 0.05)) * h)
            self.modulating = False
            self.freq = 0.0
            self.current = self.torque_pu = self.power_kw = 0.0
            self.bus_v = max(0.0, self.bus_v - self.bus_v * min(1.0, h / 2.0))
            self.drive_temp += (p["ambient_temp"] - self.drive_temp) * min(1.0, h / 900.0)
            self.motor_temp += (p["ambient_temp"] - self.motor_temp) * min(1.0, h / 1800.0)
            self.warnings = ["Uv Undervoltage"] if self._off_s < 2.0 else []
            return
        if self._off_s > 0:
            # power back: a short dip rides through (L2-01 = 1/2, speed search); a real outage reboots
            if self._off_s >= 2.0 or int(p["l2_01"]) == 0:
                if int(p["l2_01"]) == 0 and self._was_modulating_at_loss:
                    self._trip("Uv1")
                self.mode = "auto" if int(p["s5_04"]) in (1, 2) else self.mode
                if int(p["b1_17"]) == 0:
                    self.need_cycle = True
            self._off_s = 0.0

        # ---- run command / reference by mode ----
        override = bool(self._func("AF", False))
        rst = bool(self._func("14", False))
        if rst and not self._last_reset:
            self.fault_reset()
        self._last_reset = rst
        if self.fb["fault_reset"] and not self._last_fb_reset:
            self.fault_reset()
        self._last_fb_reset = bool(self.fb["fault_reset"])

        serial = int(p["b1_01"]) == 2 or int(p["b1_02"]) == 2
        if self.mode == "off":
            run, ref = False, 0.0
        elif self.mode == "hand":
            run = True
            sel = int(p["s5_01"])
            ref = {0: self.hand_ref, 1: self._a1_hz(), 3: p["s5_05"]}.get(sel, self._auto_ref())
        else:
            run = bool(self._func("40", False)) if int(p["b1_02"]) == 1 else bool(self.fb["run"])
            ref = self._auto_ref()
        # comm error (CE): the BAS stopped writing
        self._ce = False
        if self.mode == "auto" and serial and int(p["h5_05"]) == 1 and self.fb_idle_s > p["h5_09"]:
            self._ce = True
            act = int(p["h5_04"])
            if act in (0, 1, 2):
                self._trip("CE")
            else:
                self._alarm("CE")
                if act == 4:
                    ref = p["d1_04"]
        self.run_cmd = run                          # a Run command is present (fault reset is ignored then)
        if self.need_cycle:
            if not run:
                self.need_cycle = False
            run = False

        # ---- interlocks ----
        safeties_ok = bool(self._func("B1", True))
        bas_ok = bool(self._func("B2", True))
        enable_ok = bool(self._func("70", True))
        enable1 = self._func("6A", None)
        if enable1 is False:
            if self.run_cmd:
                self._alarm("dnE")
            if self.modulating or self.run_cmd:
                self.need_cycle = True              # 6A: a new run command is needed after enable
        if not enable_ok and self.run_cmd:
            self._alarm("dnE")                      # 70 Drive Enable 2: runs again as soon as it closes
        ef = self._ext_fault_active()
        if ef:
            code = str(self.p.get(f"h1_0{ef[1]}", "24")).upper() if ef != "s0" else "24"
            if code in ("2C", "2D", "2E", "2F"):
                self._alarm(f"EF{ef[1]}")
            else:
                self._trip(f"EF{ef[1]}" if ef != "s0" else "EF3")
        if not safeties_ok:
            self._alarm("SAFE")

        # ---- injected faults (while modulating, like the real drive) ----
        if self.modulating:
            if "overcurrent_trip" in self.inject:
                self._trip("oC")
            if "earth_leakage" in self.inject:
                self._trip("GF")
            if "output_phase_loss" in self.inject:
                self._trip("LF")
            if "input_phase_loss" in self.inject and self.torque_pu > 0.2:
                self._trip("PF")
            if "mains_undervoltage" in self.inject:
                self._trip("Uv1")
        if "cooling_fan_failed" in self.inject:
            self._alarm("FAn")

        faulted = bool(self.fault_code)
        self.override = override and not faulted and safeties_ok
        if self.override:
            run = True
            ref = p["s6_01"] if int(p["s6_02"]) == 0 else self._auto_ref()
        allowed = safeties_ok and (bas_ok or self.override) and enable_ok and (enable1 is not False)
        if run and not bas_ok and not self.override:
            self._alarm("inTLK")
        self.baseblock = run and not allowed and not faulted
        go = run and allowed and not faulted
        coast_stop = faulted or not safeties_ok or (run and not bas_ok and not self.override) \
            or (not go and int(p["b1_03"]) == 1)
        if self._ce and int(p["h5_04"]) == 1:
            coast_stop = True

        # ---- ramp ----
        lo, hi = p["d2_02"] / 100.0 * f_max, min(p["d2_01"] / 100.0, 1.0) * f_max
        target = max(lo, min(ref, hi)) if go else 0.0
        self.target = target
        self.ref_hz = ref if run else self.ref_hz
        s = self.shaft
        t_load = max(0.0, load_torque(s))
        H = max(0.1, p["inertia_h"])
        i_mag = 0.35
        i_max_pu = p["l3_02"] / 100.0 * p["drive_rated_amps"] / p["e2_01"]
        t_avail = math.sqrt(max(0.0, i_max_pu ** 2 - i_mag ** 2)) / math.sqrt(1 - i_mag ** 2)
        regen = 0.0
        t_motor = 0.0
        ramp_stop = (not go) and (not coast_stop) and self.modulating and self.freq > 0.5
        self.warnings = []
        if go or ramp_stop:
            if not self.modulating:
                self.freq = s * f_max               # speed search onto a spinning fan
            self.modulating = True
            acc = f_max / p["c1_01"] if p["c1_01"] > 0 else 1e6
            dec = f_max / p["c1_02"] if p["c1_02"] > 0 else 1e6
            df = min(target - self.freq, acc * h) if target >= self.freq else -min(self.freq - target, dec * h)
            t_acc = 2 * H * (df / f_max) / h
            if t_load + t_acc > t_avail:            # stall prevention (L3-01 / L3-02)
                t_acc = t_avail - t_load
                self.warnings.append("Stall prevention")
            if t_acc < -t_avail:
                t_acc = -t_avail
            s_new = max(0.0, s + t_acc * h / (2 * H))
            regen = max(0.0, 2 * H * s * (s - s_new) / h - t_load * s)
            if regen > p["regen_absorb_pu"]:
                if int(p["l3_04"]) == 1:            # stall prevention during decel: extend the ramp
                    max_rate = min((p["regen_absorb_pu"] + t_load * s) / (2 * H * max(s, 0.05)),
                                   (t_avail + t_load) / (2 * H))
                    s_new = max(s_new, s - max_rate * h, 0.0)
                    regen = min(regen, p["regen_absorb_pu"])
                    self.warnings.append("Decel extended (L3-04)")
            t_motor = t_load + 2 * H * (s_new - s) / h
            self.shaft = s_new
            self.freq = s_new * f_max
            if ramp_stop and self.freq <= 0.5:
                self.modulating = False
                self.freq = 0.0
        else:
            self.modulating = False
            self.freq = 0.0
            self.shaft = max(0.0, s - (t_load + 0.01) / (2 * H * max(s, 0.05)) * h)

        # ---- DC bus ----
        nominal_bus = 1.35 * p["line_voltage"]
        if regen > p["regen_absorb_pu"]:
            self.bus_v += (regen - p["regen_absorb_pu"]) * 2000.0 * h
        else:
            self.bus_v += (nominal_bus * (0.6 if "mains_undervoltage" in self.inject else 1.0) - self.bus_v) * min(1.0, h / 0.5)
        if self.bus_v > 1.3 * nominal_bus:
            self._trip("ov")
            self.modulating = False
            self.freq = 0.0
            t_motor = 0.0

        # ---- electrical ----
        if self.modulating:
            iq = abs(t_motor) * math.sqrt(1 - i_mag ** 2)
            i_pu = min(i_max_pu, math.sqrt(i_mag ** 2 + iq ** 2))
            self.torque_pu = t_motor
            mech = t_motor * (self.freq / f_max)
            self.power_kw = p["e2_11"] * mech / 0.92 if mech > 0 else p["e2_11"] * mech
        else:
            i_pu = 0.0
            self.torque_pu = self.power_kw = 0.0
        self.current = i_pu * p["e2_01"]

        # ---- overloads: oL1 motor (150 % for 60 s), oL2 drive (A: same curve on drive rated current) ----
        for attr, ratio, code in (("ol1", i_pu, "oL1"), ("ol2", self.current / p["drive_rated_amps"], "oL2")):
            acc = getattr(self, attr)
            excess = ratio * ratio - 1.1
            acc = max(0.0, acc + h * excess / (1.5 ** 2 - 1.1) / 60.0) if excess > 0 else max(0.0, acc - h / 600.0)
            setattr(self, attr, acc)
            if acc >= 1.0:
                self._trip(code)
                setattr(self, attr, 0.0)

        # ---- thermal ----
        rise = 2.6 if "cooling_fan_failed" in self.inject else 1.0
        dt_target = p["ambient_temp"] + rise * ((5.0 if self.modulating else 0.0) + 40.0 * i_pu ** 2)
        self.drive_temp += (dt_target - self.drive_temp) * min(1.0, h / 240.0)
        self.motor_temp += (p["ambient_temp"] + 75.0 * i_pu ** 2 - self.motor_temp) * min(1.0, h / 900.0)
        if self.drive_temp > p["l8_02"]:
            self._alarm("oH")
        if self.drive_temp > p["l8_02"] + 15.0:
            self._trip("oH1")

        if self.modulating:
            self.run_hours += h / 3600.0
            self.kwh += max(0.0, self.power_kw) * h / 3600.0
        if self.baseblock and not self.alarms:
            self._alarm("bb")
        self.warnings = [f"{a} {ALARM_TEXT.get(a, '')}".strip() for a in self.alarms] + self.warnings

    def _auto_ref(self) -> float:
        p = self.p
        b = int(p["b1_01"])
        if b == 1:
            return self._a1_hz()
        if b == 2:
            return float(self.fb["freq_hz"])
        return self.hand_ref

    # ---------------------------------------------------------------- outputs
    @property
    def speed_pu(self) -> float:
        return self.shaft

    @property
    def motor_rpm(self) -> float:
        p = self.p
        sync = 1800.0 * p["e1_04"] / 60.0
        slip = max(0.0, (1800.0 - p["motor_rated_rpm"]) / 1800.0)
        return self.shaft * sync * (1 - slip * max(0.0, min(1.5, self.torque_pu)))

    @property
    def output_voltage(self) -> float:
        if not self.modulating:
            return 0.0
        return self.p["e1_05"] * max(0.04, min(1.0, self.freq / self.p["e1_04"]))

    @property
    def zero_speed(self) -> bool:
        return not self.modulating or self.freq < 0.5

    @property
    def ready(self) -> bool:
        return self.powered and not self.fault_code

    def _h2(self, code) -> bool:
        if not self.powered:
            return False
        code = str(code).upper()
        inv = len(code) == 3 and code.startswith("1")
        base = (code[1:].lstrip("0") or "0") if inv else code
        val = {
            "0": self.modulating,
            "1": self.zero_speed,
            "2": self.modulating and abs(self.freq - self.target) < 2.0,     # L4-02 = 2.0 Hz (A)
            "6": self.ready and not self.baseblock,
            "E": bool(self.fault_code),
            "10": bool(self.alarms),
            "38": bool(self._func("6A", True)) and bool(self._func("70", True)),
            "B2": (self.run_cmd and not self.fault_code) or self.modulating or self.override,
        }.get(base, False)
        return (not val) if inv else val

    def relay(self, terminal: str) -> bool:
        """Contact closed?  ma (N.O., closes on fault), mb (N.C., opens on fault), m1 / m3 / m5."""
        if not self.powered:
            return False                            # no control power: every contact drops out
        faulted = bool(self.fault_code)
        if terminal == "ma":
            return faulted
        if terminal == "mb":
            return not faulted
        return self._h2(self.p[{"m1": "h2_01", "m3": "h2_02", "m5": "h2_03"}[terminal]])

    def analog_out(self, terminal: str) -> float:
        if not self.powered:
            return 0.0
        p = self.p
        sel, gain = (p["h4_01"], p["h4_02"]) if terminal == "fm" else (p["h4_04"], p["h4_05"])
        v = {102: self.freq / p["e1_04"], 103: self.current / p["drive_rated_amps"],
             108: self.power_kw / max(0.1, p["e2_11"])}.get(int(sel), 0.0)
        return max(0.0, min(100.0, v * gain))

    def status_text(self) -> str:
        if not self.powered:
            return "NO POWER"
        if self.fault_code:
            return f"FAULT {self.fault_code} {FAULT_TEXT.get(self.fault_code, '')}".strip()
        if self.override:
            return "EMERGENCY OVERRIDE"
        if self.modulating:
            return "RUNNING"
        if self.baseblock:
            return "BASEBLOCK"
        if self.run_cmd and self.mode != "off":
            return "RUN PENDING"
        return "STOPPED"

    # ---------------------------------------------------------------- standalone drive (vfd_fan)
    def standalone_inputs(self) -> List[SignalDef]:
        return [
            *[SignalDef(t, _B, description=f"S{t[1]} terminal", default=(t in ("s3", "s4"))) for t in S],
            SignalDef("a1_pct", _A, "percent", "A1 terminal, % of 10 V (H3-01)", 0.0),
            SignalDef("a2_pct", _A, "percent", "A2 terminal, % of 4-20 mA span (H3-09)", 0.0),
            SignalDef("fb_run", _B, description="BV1 RUN FWD", default=False),
            SignalDef("fb_freq_hz", _A, "hertz", "AV2 Frequency Command", 0.0),
            SignalDef("fb_fault_reset", _B, description="BV4 FAULT RESET", default=False),
            SignalDef("fb_ext_fault", _B, description="BV3 EXT FAULT", default=False),
            *[SignalDef(f"fb_{t}", _B, description=f"BV{4 + int(t[1])} MF Input {t[1]} Cmd", default=False) for t in S[2:]],
            SignalDef("efb_idle_s", _A, "seconds", "Time since the drive last heard the fieldbus", 0.0),
        ]

    def standalone_signals(self) -> List[SignalDef]:
        fault_bits = [("bit_oc_gf", "Overcurrent - Ground Fault"), ("bit_ov", "Main Circuit Overvoltage"),
                      ("bit_ol2", "Drive Overload"), ("bit_oh", "Drive Overheat"), ("bit_ef", "External Fault"),
                      ("bit_ol1", "Mtr Ovrld-OvrTorque"), ("bit_uv", "Main CKT Undervoltage"),
                      ("bit_lf", "Output Phase Loss"), ("bit_ce", "Communication Error")]
        return [
            SignalDef("output_freq", _A, "hertz", "Output frequency (AV10)"),
            SignalDef("freq_ref", _A, "hertz", "Frequency reference (AV9)"),
            SignalDef("output_voltage", _A, "volts", "Output voltage (AV11)"),
            SignalDef("output_current", _A, "amperes", "Output current (AV12)"),
            SignalDef("output_power_w", _A, "watts", "Output power (AV13)"),
            SignalDef("torque_ref_pct", _A, "percent", "Torque reference (AV14)"),
            SignalDef("dc_bus_v", _A, "volts", "DC bus voltage (AV18)"),
            SignalDef("a1_level", _A, "percent", "Analog input 1 level (AI1)"),
            SignalDef("a2_level", _A, "percent", "Analog input 2 level (AI2)"),
            SignalDef("rated_current", _A, "amperes", "Rated current n9-01 (AI9)"),
            SignalDef("motor_rpm", _A, "revolutionsPerMinute", "Motor speed"),
            SignalDef("power_kw", _A, "kilowatts", "Output power"),
            SignalDef("drive_temp", _A, "degreesCelsius", "Heatsink temperature"),
            SignalDef("motor_temp", _A, "degreesCelsius", "Estimated motor temperature"),
            SignalDef("kwh", _A, "kilowattHours", "Energy"),
            SignalDef("run_hours", _A, "hours", "Run time"),
            SignalDef("fm_pct", _A, "percent", "FM output, % of 10 V"),
            SignalDef("am_pct", _A, "percent", "AM output, % of 10 V"),
            SignalDef("running", _B, description="RUN-STOP (BV13)", default=False),
            SignalDef("reverse", _B, description="REV-FWD (BV14)", default=False),
            SignalDef("ready", _B, description="READY (BV15)", default=True),
            SignalDef("fault_active", _B, description="FAULT (BV16)", default=False),
            *[SignalDef(n, _B, description=f"{d} (fault bit)", default=False) for n, d in fault_bits],
            SignalDef("operating", _B, description="Operating (BV33)", default=False),
            SignalDef("freq_agree", _B, description="Frequency Agree (BV35)", default=False),
            SignalDef("baseblock", _B, description="Base Block (BV41)", default=False),
            SignalDef("ref_from_comm", _B, description="Frequency Reference Mode COM (BV42)", default=False),
            SignalDef("run_from_comm", _B, description="Run Command Mode COM (BV43)", default=False),
            SignalDef("comm_error", _B, description="Drive Comm Error (BV57)", default=False),
            SignalDef("in_auto", _B, description="HOA in AUTO", default=True),
            SignalDef("override_active", _B, description="Emergency override active", default=False),
            SignalDef("alarm_active", _B, description="Alarm (minor fault) present", default=False),
            SignalDef("ma", _B, description="MA-MC contact closed (N.O., fault)", default=False),
            SignalDef("mb", _B, description="MB-MC contact closed (N.C., opens on fault)", default=True),
            SignalDef("m1", _B, description="M1-M2 contact closed (H2-01)", default=False),
            SignalDef("m3", _B, description="M3-M4 contact closed (H2-02)", default=False),
            SignalDef("m5", _B, description="M5-M6 contact closed (H2-03)", default=False),
            *[SignalDef(f"{t}_status", _B, description=f"Input Terminal {t[1]} (BI{t[1]})", default=False) for t in S],
            SignalDef("fault_text", "text", description="Active fault code", default=""),
            SignalDef("last_fault_text", "text", description="Last fault code", default=""),
            SignalDef("keypad_mode", "text", description="HAND / OFF / AUTO", default="auto"),
            SignalDef("status_text", "text", description="Drive status", default=""),
            SignalDef("warnings", "text", description="Active alarms", default=""),
            SignalDef("hand_ref", _A, "hertz", "HAND reference (keypad)"),
        ]

    def read_standalone(self, inputs: Dict[str, Any], safety_terminal: int, safety_open: bool,
                        speed_wire_broken: bool) -> None:
        for t in S:
            self.di[t] = bool(inputs[t])
        if 1 <= safety_terminal <= 7 and safety_open:
            self.di[f"s{safety_terminal}"] = False
        self.a1_pct = 0.0 if speed_wire_broken else float(inputs["a1_pct"])
        self.a2_pct = float(inputs["a2_pct"])
        self.fb["run"] = bool(inputs["fb_run"])
        self.fb["freq_hz"] = float(inputs["fb_freq_hz"])
        self.fb["fault_reset"] = bool(inputs["fb_fault_reset"])
        self.fb["ext_fault"] = bool(inputs["fb_ext_fault"])
        for t in S[2:]:
            self.fb[t] = bool(inputs[f"fb_{t}"])
        self.fb_idle_s = float(inputs["efb_idle_s"])

    def publish_standalone(self, s: Dict[str, Any], rng) -> None:
        p = self.p
        f = self.fault_code
        s.update({
            "output_freq": round(self.freq, 2),
            "freq_ref": round(self.ref_hz, 2),
            "output_voltage": round(self.output_voltage, 1),
            "output_current": round(self.current + (rng.gauss(0, 0.03) if self.modulating else 0.0), 2),
            "output_power_w": round(self.power_kw * 1000.0, 0),
            "torque_ref_pct": round(self.torque_pu * 100.0, 1),
            "dc_bus_v": round(self.bus_v + rng.gauss(0, 0.8), 0),
            "a1_level": round(self._ai_frac(self.a1_pct, int(p["h3_01"])) * 100.0, 1),
            "a2_level": round(self._ai_frac(self.a2_pct, int(p["h3_09"])) * 100.0, 1),
            "rated_current": p["drive_rated_amps"],
            "motor_rpm": round(self.motor_rpm, 0),
            "power_kw": round(self.power_kw, 2),
            "drive_temp": round(self.drive_temp, 1),
            "motor_temp": round(self.motor_temp, 1),
            "kwh": round(self.kwh, 2),
            "run_hours": round(self.run_hours, 2),
            "fm_pct": round(self.analog_out("fm"), 1),
            "am_pct": round(self.analog_out("am"), 1),
            "running": self.modulating,
            "reverse": False,
            "ready": self.ready,
            "fault_active": bool(f),
            "bit_oc_gf": f in ("oC", "GF"),
            "bit_ov": f == "ov",
            "bit_ol2": f == "oL2",
            "bit_oh": f == "oH1",
            "bit_ef": f.startswith("EF"),
            "bit_ol1": f == "oL1",
            "bit_uv": f == "Uv1",
            "bit_lf": f == "LF",
            "bit_ce": f == "CE" or self._ce,
            "operating": self.modulating,
            "freq_agree": self._h2("2"),
            "baseblock": self.baseblock,
            "ref_from_comm": int(p["b1_01"]) == 2,
            "run_from_comm": int(p["b1_02"]) == 2,
            "comm_error": self._ce,
            "in_auto": self.mode == "auto",
            "override_active": self.override,
            "alarm_active": bool(self.alarms),
            "ma": self.relay("ma"),
            "mb": self.relay("mb"),
            "m1": self.relay("m1"),
            "m3": self.relay("m3"),
            "m5": self.relay("m5"),
            "fault_text": f,
            "last_fault_text": self.fault_history[0] if self.fault_history else "",
            "keypad_mode": self.mode,
            "status_text": self.status_text(),
            "warnings": "; ".join(self.warnings),
            "hand_ref": self.hand_ref,
        })
        for t in S:
            s[f"{t}_status"] = self._closed(t)
