"""
Variable frequency drive component, modelled on the ABB ACH580 (HVAC drive).

This is a building block, not a BACnet device by itself.  An equipment model
owns one (a standalone fan) or several (an AHU fan array), feeds it terminal
and fieldbus inputs plus the mechanical load, and publishes its outputs.

Parameter names carry ACH580 parameter numbers in PARAM_INFO so trainees see
the same numbers as on the real keypad.  Behaviour is simplified to what a
controls technician interacts with: control location, start/stop and
reference sources, permissives and interlocks, ramps and limits, relay and
analog outputs, fieldbus comm loss, override (fire mode), and faults.

Fault codes follow the ACH580 firmware manual numbering; verify against the
manual for the firmware in your drives.

Units: frequency Hz, current A, power kW, temperature C (as the drive shows).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from ..base import ParamInfo

DI_CHOICES = ["di1", "di2", "di3", "di4", "di5", "di6"]
RELAY_CHOICES = ["not_energized", "energized", "ready_run", "enabled", "started", "running",
                 "fault", "fault_inverted", "warning", "at_setpoint"]

# --- fault codes (ACH580 firmware manual numbering) ------------------------
FAULT_TEXT = {
    2310: "Overcurrent",
    2330: "Earth leakage",
    3130: "Input phase loss",
    3210: "DC link overvoltage",
    3220: "DC link undervoltage",
    4310: "Excess temperature",
    5091: "Safe torque off",
    6681: "EFB comm loss",
    7121: "Motor stall",
    9081: "External fault 1",
}

VFD_DEFAULTS: Dict[str, Any] = {
    # 99 Motor data (nameplate) - default: 10 HP, 460 V, 4 pole
    "motor_nom_current": 14.0,        # 99.06 A
    "motor_nom_voltage": 460.0,       # 99.07 V
    "motor_nom_freq": 60.0,           # 99.08 Hz
    "motor_nom_speed": 1770.0,        # 99.09 rpm
    "motor_nom_power": 7.5,           # 99.10 kW (10 HP)
    # 20 Start/stop/direction
    "ext1_commands": "in1_start",     # 20.01
    "run_enable_source": "selected",  # 20.12 run enable 1 source
    "run_permissive": "not_used",     # 20.40
    "start_interlock_1": "not_used",  # 20.41
    # 21 Start/stop mode
    "stop_mode": "coast",             # 21.03
    # 12 Standard AI
    "ai1_min_v": 0.0,                 # 12.17
    "ai1_max_v": 10.0,                # 12.18
    "ai1_scaled_min": 0.0,            # 12.19 Hz at AI1 min
    "ai1_scaled_max": 60.0,           # 12.20 Hz at AI1 max
    # 28 Frequency reference chain
    "ext1_freq_ref1": "ai1_scaled",   # 28.11
    "freq_ref_safe": 30.0,            # 28.41 Hz (comm loss safe speed)
    "accel_time_1": 30.0,             # 28.72 s, 0 -> 46.02
    "decel_time_1": 30.0,             # 28.73 s, 46.02 -> 0
    # 30 Limits
    "min_freq": 0.0,                  # 30.13 Hz
    "max_freq": 60.0,                 # 30.14 Hz
    "max_current": 19.6,              # 30.17 A
    "overvoltage_control": True,      # 30.30
    # 31 Fault functions
    "external_event_1": "not_used",   # 31.01 source (fault 9081 when the input opens)
    "stall_freq_limit": 15.0,         # 31.27 Hz
    "stall_time": 20.0,               # 31.28 s
    # 10 Standard DI, RO
    "ro1_source": "ready_run",        # 10.24
    "ro2_source": "running",          # 10.27
    "ro3_source": "fault_inverted",   # 10.30  ("Fault (-1)")
    # 13 Standard AO
    "ao1_source": "output_frequency",  # 13.12
    # 46 Monitoring/scaling settings
    "freq_scaling": 60.0,             # 46.02 Hz = 100 % reference / ramp time basis
    # 58 Embedded fieldbus
    "comm_loss_action": "no_action",  # 58.14
    "comm_loss_mode": "any_message",  # 58.15
    "comm_loss_time": 30.0,           # 58.16 s
    # 70 Override (fire mode)
    "override_enable": False,         # 70.02
    "override_source": "di6",         # 70.03 activation source
    "override_freq": 60.0,            # 70.05 Hz
    # mechanical / thermal (not drive parameters)
    "inertia_h": 6.0,                 # s, inertia constant of motor + fan
    "regen_absorb_pu": 0.12,          # pu power the drive/motor losses absorb when braking
    "line_voltage": 480.0,            # V supply
    "ambient_temp": 30.0,             # C, drive cabinet ambient
}

VFD_PARAM_INFO: Dict[str, ParamInfo] = {
    "motor_nom_current": ParamInfo("99.06", "Motor nominal current", "99 Motor data", "A", minimum=0.5, maximum=500),
    "motor_nom_voltage": ParamInfo("99.07", "Motor nominal voltage", "99 Motor data", "V", minimum=100, maximum=690),
    "motor_nom_freq": ParamInfo("99.08", "Motor nominal frequency", "99 Motor data", "Hz", minimum=10, maximum=500),
    "motor_nom_speed": ParamInfo("99.09", "Motor nominal speed", "99 Motor data", "rpm", minimum=100, maximum=30000),
    "motor_nom_power": ParamInfo("99.10", "Motor nominal power", "99 Motor data", "kW", minimum=0.1, maximum=500),
    "ext1_commands": ParamInfo("20.01", "Ext1 commands", "20 Start/stop/direction",
                               choices=["not_selected", "in1_start", "in1_start_in2_dir", "fieldbus"]),
    "run_enable_source": ParamInfo("20.12", "Run enable 1 source", "20 Start/stop/direction",
                                   choices=["selected", "not_selected", *DI_CHOICES, "fieldbus"]),
    "run_permissive": ParamInfo("20.40", "Run permissive", "20 Start/stop/direction", choices=["not_used", *DI_CHOICES]),
    "start_interlock_1": ParamInfo("20.41", "Start interlock 1", "20 Start/stop/direction",
                                   choices=["not_used", *DI_CHOICES]),
    "stop_mode": ParamInfo("21.03", "Stop mode", "21 Start/stop mode", choices=["coast", "ramp"]),
    "ai1_min_v": ParamInfo("12.17", "AI1 min", "12 Standard AI", "V", minimum=0, maximum=10),
    "ai1_max_v": ParamInfo("12.18", "AI1 max", "12 Standard AI", "V", minimum=0, maximum=10),
    "ai1_scaled_min": ParamInfo("12.19", "AI1 scaled at AI1 min", "12 Standard AI", "Hz", minimum=0, maximum=500),
    "ai1_scaled_max": ParamInfo("12.20", "AI1 scaled at AI1 max", "12 Standard AI", "Hz", minimum=0, maximum=500),
    "ext1_freq_ref1": ParamInfo("28.11", "Ext1 frequency ref1", "28 Frequency reference chain",
                                choices=["zero", "ai1_scaled", "efb_ref1", "panel"]),
    "freq_ref_safe": ParamInfo("28.41", "Frequency ref safe", "28 Frequency reference chain", "Hz", minimum=0, maximum=500),
    "accel_time_1": ParamInfo("28.72", "Freq acceleration time 1", "28 Frequency reference chain", "s", minimum=0.1, maximum=1800),
    "decel_time_1": ParamInfo("28.73", "Freq deceleration time 1", "28 Frequency reference chain", "s", minimum=0.1, maximum=1800),
    "min_freq": ParamInfo("30.13", "Minimum frequency", "30 Limits", "Hz", minimum=0, maximum=500),
    "max_freq": ParamInfo("30.14", "Maximum frequency", "30 Limits", "Hz", minimum=1, maximum=500),
    "max_current": ParamInfo("30.17", "Maximum current", "30 Limits", "A", minimum=0.1, maximum=1000),
    "overvoltage_control": ParamInfo("30.30", "Overvoltage control", "30 Limits", choices=[True, False]),
    "external_event_1": ParamInfo("31.01", "External event 1 source", "31 Fault functions", choices=["not_used", *DI_CHOICES]),
    "stall_freq_limit": ParamInfo("31.27", "Stall frequency limit", "31 Fault functions", "Hz", minimum=0.5, maximum=100),
    "stall_time": ParamInfo("31.28", "Stall time", "31 Fault functions", "s", minimum=1, maximum=300),
    "ro1_source": ParamInfo("10.24", "RO1 source", "10 Standard DI, RO", choices=RELAY_CHOICES),
    "ro2_source": ParamInfo("10.27", "RO2 source", "10 Standard DI, RO", choices=RELAY_CHOICES),
    "ro3_source": ParamInfo("10.30", "RO3 source", "10 Standard DI, RO", choices=RELAY_CHOICES),
    "ao1_source": ParamInfo("13.12", "AO1 source", "13 Standard AO",
                            choices=["zero", "output_frequency", "motor_current", "output_power"]),
    "freq_scaling": ParamInfo("46.02", "Frequency scaling", "46 Monitoring/scaling", "Hz", minimum=1, maximum=500),
    "comm_loss_action": ParamInfo("58.14", "Communication loss action", "58 Embedded fieldbus",
                                  choices=["no_action", "fault", "last_speed", "speed_ref_safe"]),
    "comm_loss_mode": ParamInfo("58.15", "Communication loss mode", "58 Embedded fieldbus",
                                choices=["any_message", "cw_ref"]),
    "comm_loss_time": ParamInfo("58.16", "Communication loss time", "58 Embedded fieldbus", "s", minimum=0.1, maximum=600),
    "override_enable": ParamInfo("70.02", "Override enable", "70 Override", choices=[True, False]),
    "override_source": ParamInfo("70.03", "Override activation source", "70 Override", choices=DI_CHOICES),
    "override_freq": ParamInfo("70.05", "Override frequency", "70 Override", "Hz", minimum=0, maximum=500),
}

# Faults an owner can inject (names are prefixed by the owner if needed)
VFD_INJECTABLE = {
    "sto_open": "Safe torque off circuit open",
    "overcurrent_trip": "Overcurrent trip (output short / ground)",
    "earth_leakage": "Motor insulation failure (earth leakage)",
    "input_phase_loss": "Input phase loss",
    "mains_undervoltage": "Low supply voltage",
    "cooling_fan_failed": "Drive cooling fan failed",
}


@dataclass
class VfdInputs:
    di: List[bool] = field(default_factory=lambda: [False] * 6)   # terminal states DI1..DI6
    ai1_pct: float = 0.0               # AI1 signal, % of 10 V
    efb_run: bool = False              # BV RUN/STOP CMD
    efb_ref1_pct: float = 0.0          # AV INPUT REF 1, % of 46.02
    efb_run_enable: bool = True        # BV RUN ENA CMD
    efb_fault_reset: bool = False      # BV FAULT RESET (rising edge)
    efb_idle_s: float = 0.0            # s since the drive last heard the fieldbus


class Vfd:
    """ACH580-style drive driving a motor and a mechanical load."""

    def __init__(self, params: Dict[str, Any], inject: Optional[Set[str]] = None):
        self.p = params                       # shared dict owned by the equipment model
        self.inject: Set[str] = inject if inject is not None else set()
        self.inp = VfdInputs()
        self.mode = "auto"                    # keypad: off / hand / auto
        self.hand_ref = 30.0                  # Hz, keypad reference in Hand
        self.reset_state()

    # ------------------------------------------------------------------

    def reset_state(self) -> None:
        self.freq = 0.0                 # output frequency, Hz
        self.modulating = False
        self.fault_code = 0
        self.fault_history: List[int] = []   # most recent first
        self.warnings: List[str] = []
        self.current = 0.0
        self.torque_pu = 0.0
        self.power_kw = 0.0
        self.bus_v = 1.35 * self.p["line_voltage"]
        self.shaft = 0.0                # motor shaft speed, pu
        self.drive_temp = self.p["ambient_temp"]
        self.motor_temp = self.p["ambient_temp"]
        self.kwh = 0.0
        self.run_hours = 0.0
        self.stall_timer = 0.0
        self.ref_hz = 0.0
        self.started = False
        self.ready = True
        self.at_setpoint = False
        self.override = False
        self._last_reset_bit = False
        self._load_pu = 0.0

    # ----- keypad -------------------------------------------------------

    def keypad(self, key: str, value: Any = None) -> None:
        if key in ("hand", "off", "auto"):
            self.mode = key
        elif key == "reset":
            self.fault_reset()
        elif key == "hand_ref":
            self.hand_ref = max(0.0, min(float(value), self.p["max_freq"]))
        else:
            raise KeyError(key)

    def fault_reset(self) -> None:
        if self.fault_code and not self._fault_cause_present():
            self.fault_code = 0

    # ----- helpers ------------------------------------------------------

    def _src(self, source: str, default: bool) -> bool:
        if source in ("not_used", "selected"):
            return default if source == "not_used" else True
        if source == "not_selected":
            return False
        if source == "fieldbus":
            return bool(self.inp.efb_run_enable)
        if source.startswith("di"):
            return bool(self.inp.di[int(source[2]) - 1])
        return default

    def _trip(self, code: int) -> None:
        if self.fault_code:
            return
        self.fault_code = code
        self.fault_history.insert(0, code)
        del self.fault_history[3:]

    def _fault_cause_present(self) -> bool:
        p = self.p
        if "sto_open" in self.inject or "earth_leakage" in self.inject:
            return True
        if p["external_event_1"] != "not_used" and not self._src(p["external_event_1"], True):
            return True
        if self.drive_temp > 105.0:
            return True
        return False

    def _ai1_hz(self) -> float:
        p = self.p
        volts = max(0.0, min(10.0, self.inp.ai1_pct / 10.0))
        span = p["ai1_max_v"] - p["ai1_min_v"]
        if span <= 0:
            return p["ai1_scaled_min"]
        frac = (volts - p["ai1_min_v"]) / span
        frac = max(0.0, min(1.0, frac))
        return p["ai1_scaled_min"] + frac * (p["ai1_scaled_max"] - p["ai1_scaled_min"])

    def _uses_fieldbus(self) -> bool:
        return self.mode == "auto" and (self.p["ext1_commands"] == "fieldbus" or self.p["ext1_freq_ref1"] == "efb_ref1")

    # ----- main step ----------------------------------------------------

    def step(self, h: float, load_torque: Callable[[float], float]) -> None:
        """
        Advance h seconds.  load_torque(speed_pu) returns the load torque in
        per-unit of motor rated torque at that speed (fan: ~k*s^2).
        """
        p = self.p
        f_nom = p["motor_nom_freq"]
        self.warnings = []

        # --- fault reset from fieldbus (rising edge) ---
        if self.inp.efb_fault_reset and not self._last_reset_bit:
            self.fault_reset()
        self._last_reset_bit = bool(self.inp.efb_fault_reset)

        # --- override (fire mode) ---
        self.override = bool(p["override_enable"]) and self._src(p["override_source"], False)

        # --- supply / STO ---
        nominal_bus = 1.35 * p["line_voltage"]
        mains_low = "mains_undervoltage" in self.inject
        sto_ok = "sto_open" not in self.inject

        # --- start command and reference ---
        comm_lost = False
        if self.override:
            start, ref = True, p["override_freq"]
        elif self.mode == "off":
            start, ref = False, 0.0
        elif self.mode == "hand":
            start, ref = True, self.hand_ref
        else:
            cmd = p["ext1_commands"]
            if cmd in ("in1_start", "in1_start_in2_dir"):
                start = bool(self.inp.di[0])
            elif cmd == "fieldbus":
                start = bool(self.inp.efb_run)
            else:
                start = False
            src = p["ext1_freq_ref1"]
            if src == "ai1_scaled":
                ref = self._ai1_hz()
            elif src == "efb_ref1":
                ref = self.inp.efb_ref1_pct / 100.0 * p["freq_scaling"]
            elif src == "panel":
                ref = self.hand_ref
            else:
                ref = 0.0
            # fieldbus communication loss supervision
            if self._uses_fieldbus() and p["comm_loss_action"] != "no_action" \
                    and self.inp.efb_idle_s > p["comm_loss_time"]:
                comm_lost = True
                action = p["comm_loss_action"]
                self.warnings.append("EFB communication loss")
                if action == "fault":
                    self._trip(6681)
                elif action == "speed_ref_safe":
                    ref = p["freq_ref_safe"]
                    start = True
                # last_speed: keep last start command and current reference
                if action == "last_speed":
                    start = self.started
                    ref = self.ref_hz if self.started else ref

        # --- enables / permissives / interlocks ---
        run_enable = self._src(p["run_enable_source"], True)
        permissive = self._src(p["run_permissive"], True)
        interlock = self._src(p["start_interlock_1"], True)
        ext_event_ok = p["external_event_1"] == "not_used" or self._src(p["external_event_1"], True)
        if not ext_event_ok and not self.override:
            self._trip(9081)
        if not run_enable:
            self.warnings.append("Run enable missing")
        if not permissive:
            self.warnings.append("Run permissive missing")
        if not interlock:
            self.warnings.append("Start interlock 1 open")
        if not sto_ok:
            if self.modulating and not self.override:
                self._trip(5091)
            self.warnings.append("A5A0 Safe torque off")

        # --- injected trips (only when modulating, as on the real drive) ---
        if self.modulating:
            if "overcurrent_trip" in self.inject:
                self._trip(2310)
            if "earth_leakage" in self.inject:
                self._trip(2330)
            if "input_phase_loss" in self.inject and self._load_pu > 0.2:
                self._trip(3130)
            if mains_low:
                self._trip(3220)

        allowed = (run_enable and permissive and interlock and sto_ok and not mains_low) or (self.override and sto_ok)
        if self.override:
            self.warnings.append("Override (fire mode) active")
        faulted = self.fault_code != 0 and not self.override
        self.ready = not faulted and run_enable and sto_ok and not mains_low
        self.started = start and not faulted
        run = self.started and allowed

        # --- frequency target and ramp ---
        if run:
            target = max(p["min_freq"], min(ref, p["max_freq"]))
        else:
            target = 0.0
        coast = (not run and (p["stop_mode"] == "coast" or faulted or not allowed))
        self.ref_hz = ref if start else self.ref_hz

        s = self.shaft
        t_load = max(0.0, load_torque(s))
        H = max(0.1, p["inertia_h"])
        i_mag = 0.35
        i_max_pu = p["max_current"] / p["motor_nom_current"]
        t_avail = math.sqrt(max(0.0, i_max_pu ** 2 - i_mag ** 2)) / math.sqrt(1 - i_mag ** 2)
        regen = 0.0
        t_motor = 0.0

        ramp_stop = (not run) and (not coast) and self.modulating and self.freq > 0.5
        if run or ramp_stop:
            if not self.modulating:
                self.freq = s * f_nom          # flying start onto a spinning fan
            self.modulating = True
            fs = p["freq_scaling"]
            if target >= self.freq:
                df = min(target - self.freq, fs / p["accel_time_1"] * h)
            else:
                df = -min(self.freq - target, fs / p["decel_time_1"] * h)
            t_acc = 2 * H * (df / f_nom) / h   # pu torque for this speed change
            if t_load + t_acc > t_avail:       # current limit: load first, then acceleration
                t_acc = t_avail - t_load
                self.warnings.append("Current limit")
            if t_acc < -t_avail:               # braking torque is current limited too
                t_acc = -t_avail
            s_new = max(0.0, s + t_acc * h / (2 * H))
            # braking power returned to the DC link
            regen = max(0.0, 2 * H * s * (s - s_new) / h - t_load * s)
            if regen > p["regen_absorb_pu"] and p["overvoltage_control"]:
                # overvoltage controller: extend deceleration so the bus stays in range
                max_rate = min((p["regen_absorb_pu"] + t_load * s) / (2 * H * max(s, 0.05)),
                               (t_avail + t_load) / (2 * H))
                s_new = max(s_new, s - max_rate * h, 0.0)
                regen = min(regen, p["regen_absorb_pu"])
                self.warnings.append("Overvoltage control active (decel extended)")
            t_motor = t_load + 2 * H * (s_new - s) / h
            self.shaft = s_new
            self.freq = s_new * f_nom
            if ramp_stop and self.freq <= 0.5:
                self.modulating = False
                self.freq = 0.0
        else:
            # output off: the load coasts down on its own torque
            self.modulating = False
            self.freq = 0.0
            self.shaft = max(0.0, s - (t_load + 0.01) / (2 * H * max(s, 0.05)) * h)

        # --- DC bus ---
        bus_target = nominal_bus * (0.6 if mains_low else 1.0)
        if regen > p["regen_absorb_pu"]:
            self.bus_v += (regen - p["regen_absorb_pu"]) * 2000.0 * h
        else:
            self.bus_v += (bus_target - self.bus_v) * min(1.0, h / 0.5)
        if self.bus_v > 1.3 * 1.35 * p["line_voltage"]:
            self._trip(3210)
            self.modulating = False
            self.freq = 0.0

        # --- motor stall ---
        if self.modulating and "Current limit" in self.warnings and self.freq < p["stall_freq_limit"]:
            self.stall_timer += h
            if self.stall_timer >= p["stall_time"]:
                self._trip(7121)
        else:
            self.stall_timer = 0.0

        # --- electrical quantities ---
        self._load_pu = t_load
        if self.modulating:
            iq = abs(t_motor) * math.sqrt(1 - i_mag ** 2)
            i_pu = min(i_max_pu, math.sqrt(i_mag ** 2 + iq ** 2))
            self.torque_pu = t_motor
            mech_pu = t_motor * (self.freq / f_nom)
            self.power_kw = p["motor_nom_power"] * mech_pu / 0.92 if mech_pu > 0 else p["motor_nom_power"] * mech_pu
        else:
            i_pu = 0.0
            self.torque_pu = 0.0
            self.power_kw = 0.0
        self.current = i_pu * p["motor_nom_current"]
        self.at_setpoint = self.modulating and abs(self.freq - target) < 0.5

        # --- thermal ---
        rise_scale = 2.6 if "cooling_fan_failed" in self.inject else 1.0
        drive_target = p["ambient_temp"] + rise_scale * ((5.0 if self.modulating else 0.0) + 40.0 * i_pu ** 2)
        self.drive_temp += (drive_target - self.drive_temp) * min(1.0, h / 240.0)
        motor_target = p["ambient_temp"] + 75.0 * i_pu ** 2
        self.motor_temp += (motor_target - self.motor_temp) * min(1.0, h / 900.0)
        if self.drive_temp > 100.0:
            self.warnings.append("Excess temperature")
        if self.drive_temp > 110.0:
            self._trip(4310)

        # --- totals ---
        if self.modulating:
            self.run_hours += h / 3600.0
            self.kwh += max(0.0, self.power_kw) * h / 3600.0

    # ----- outputs ------------------------------------------------------

    @property
    def speed_pu(self) -> float:
        """Motor shaft speed in per-unit (the load sees this)."""
        return self.shaft

    @property
    def motor_rpm(self) -> float:
        p = self.p
        poles = max(2, round(120 * p["motor_nom_freq"] / p["motor_nom_speed"] / 2) * 2)
        sync = 120 * p["motor_nom_freq"] / poles
        slip = (sync - p["motor_nom_speed"]) / sync
        return self.speed_pu * sync * (1 - slip * max(0.0, min(1.5, self.torque_pu)))

    @property
    def output_voltage(self) -> float:
        if not self.modulating:
            return 0.0
        p = self.p
        return p["motor_nom_voltage"] * max(0.04, min(1.0, self.freq / p["motor_nom_freq"]))

    def relay(self, source: str) -> bool:
        faulted = self.fault_code != 0
        return {
            "not_energized": False,
            "energized": True,
            "ready_run": self.ready,
            "enabled": self.ready and self._src(self.p["run_enable_source"], True),
            "started": self.started,
            "running": self.modulating,
            "fault": faulted,
            "fault_inverted": not faulted,
            "warning": bool(self.warnings),
            "at_setpoint": self.at_setpoint,
        }[source]

    def ao1_pct(self) -> float:
        p = self.p
        src = p["ao1_source"]
        if src == "output_frequency":
            v = self.freq / p["freq_scaling"]
        elif src == "motor_current":
            v = self.current / p["motor_nom_current"]
        elif src == "output_power":
            v = self.power_kw / p["motor_nom_power"]
        else:
            v = 0.0
        return max(0.0, min(100.0, v * 100.0))

    def status_text(self) -> str:
        if self.fault_code and not self.override:
            return f"FAULT {self.fault_code} {FAULT_TEXT.get(self.fault_code, '')}".strip()
        if self.override:
            return "OVERRIDE"
        if self.modulating:
            return "RUNNING"
        if self.started:
            return "START PENDING"
        return "STOPPED"
