"""
ABB ACH580 behind the drive interface.

The drive's behaviour is unchanged in components/vfd.py (firmware manual
3AXD50000027537 Rev C); this class adds the interface: wiring roles,
application presets, common settings and the standalone (vfd_fan) points.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...base import SignalDef
from ..vfd import FAULT_TEXT, VFD_DEFAULTS, VFD_INJECTABLE, VFD_PARAM_INFO, Vfd
from .base import Drive, register

_B, _A = "binary", "analog"


@register
class Ach580(Vfd, Drive):
    MODEL = "ach580"
    MAKE = "ABB"
    DISPLAY = "ABB ACH580"
    SHORT = "ACH580"
    DEFAULTS = VFD_DEFAULTS
    PARAM_INFO = VFD_PARAM_INFO
    INJECTABLE = VFD_INJECTABLE
    FAULT_TEXT = FAULT_TEXT
    TERMINALS = {"di": [f"di{i}" for i in range(1, 7)], "ai": ["ai1"], "relay": ["ro1", "ro2", "ro3"], "ao": ["ao1"]}
    ROLE_TERMINALS = {"start": "di1", "safeties": "di4", "damper_interlock": None, "speed_ref": "ai1",
                      "running_out": "ro2", "fault_out": "ro3", "speed_fb": "ao1", "damper_out": "ro1"}
    # hardwired HVAC application (BO -> DI1, AO -> AI1, safety chain -> DI4 start interlock)
    PRESETS = {
        "hardwired": {
            "ext1_commands": "in1_start",
            "ext1_freq_ref1": "ai1_scaled",
            "start_interlock_1": "di4",
            "start_interlock_stop_mode": "coast",
            "ro1_source": "ready_run",
        },
    }
    COMMON_MAP = {"motor_amps": "motor_nom_current", "motor_kw": "motor_nom_power", "max_amps": "max_current",
                  "inertia_h": "inertia_h", "min_hz": "min_freq", "accel_s": "accel_time_1", "decel_s": "decel_time_1"}
    SPEED_WIRE_FAULT = ("ai1_wire_broken", "AI1 speed signal wire broken")

    # ---------------------------------------------------------------- roles
    def wire(self, start: bool = False, safeties: bool = True, damper_interlock: bool = True,
             speed_ref: float = 0.0) -> None:
        # the ACH580 has no separate damper interlock input in this wiring: the damper end switch
        # (when used) goes into the start interlock chain with the safeties
        self.inp.di = [bool(start), False, False, bool(safeties and damper_interlock), False, False]
        self.inp.ai1_pct = float(speed_ref)

    def role_out(self, role: str):
        p = self.p
        if role == "running_out":
            return self.relay(p["ro2_source"])
        if role == "fault_out":
            return self.relay(p["ro3_source"])
        if role == "damper_out":
            return self.relay(p["ro1_source"])
        if role == "speed_fb":
            return self.ao1_pct()
        raise KeyError(role)

    # ---------------------------------------------------------------- standalone drive (vfd_fan)
    def standalone_inputs(self) -> List[SignalDef]:
        return [
            # DI4 defaults closed: 20.41 Start interlock 1 = DI4 out of the box, normally jumpered or wired
            *[SignalDef(f"di{i}", _B, description=f"DI{i} terminal", default=(i == 4)) for i in range(1, 7)],
            SignalDef("ai1_pct", _A, "percent", "AI1 terminal, % of 10 V", 0.0),
            SignalDef("efb_run", _B, description="Fieldbus RUN/STOP CMD", default=False),
            SignalDef("efb_ref1_pct", _A, "percent", "Fieldbus INPUT REF 1", 0.0),
            SignalDef("efb_run_enable", _B, description="Fieldbus RUN ENA CMD", default=True),
            SignalDef("efb_fault_reset", _B, description="Fieldbus FAULT RESET", default=False),
            SignalDef("efb_idle_s", _A, "seconds", "Time since the drive last heard the fieldbus", 0.0),
        ]

    def standalone_signals(self) -> List[SignalDef]:
        return [
            SignalDef("output_freq", _A, "hertz", "Output frequency"),
            SignalDef("speed_pct", _A, "percent", "Output frequency, % of 46.02"),
            SignalDef("motor_rpm", _A, "revolutionsPerMinute", "Motor speed"),
            SignalDef("motor_current", _A, "amperes", "Motor current"),
            SignalDef("torque_pct", _A, "percent", "Motor torque"),
            SignalDef("power_kw", _A, "kilowatts", "Output power"),
            SignalDef("dc_bus_v", _A, "volts", "DC link voltage"),
            SignalDef("output_voltage", _A, "volts", "Output voltage"),
            SignalDef("drive_temp", _A, "degreesCelsius", "Drive temperature"),
            SignalDef("motor_temp", _A, "degreesCelsius", "Estimated motor temperature"),
            SignalDef("kwh", _A, "kilowattHours", "Energy, resettable"),
            SignalDef("run_hours", _A, "hours", "Run time, resettable"),
            SignalDef("fault_code", _A, "noUnits", "Active fault code (0 = none)"),
            SignalDef("last_fault", _A, "noUnits", "Last fault code"),
            SignalDef("prev_fault_1", _A, "noUnits", "Previous fault 1"),
            SignalDef("prev_fault_2", _A, "noUnits", "Previous fault 2"),
            SignalDef("ref_hz", _A, "hertz", "Frequency reference in use"),
            SignalDef("ai1_v", _A, "volts", "AI1 terminal voltage"),
            SignalDef("ao1_pct", _A, "percent", "AO1 output, % of 10 V"),
            SignalDef("running", _B, description="Drive modulating (RUN/STOP ACT)", default=False),
            SignalDef("started", _B, description="Start command accepted", default=False),
            SignalDef("fault_active", _B, description="Drive faulted", default=False),
            SignalDef("warning_active", _B, description="Drive warning (ALARM ACT)", default=False),
            SignalDef("ready", _B, description="Ready to run", default=False),
            SignalDef("at_setpoint", _B, description="At setpoint", default=False),
            SignalDef("in_auto", _B, description="Keypad in Auto", default=True),
            SignalDef("run_enable_act", _B, description="Run permissive present (20.40)", default=True),
            SignalDef("start_interlock_ok", _B, description="Start interlock 1 present (20.41)", default=True),
            SignalDef("ramp_in_zero", _B, description="Reference forced to zero by 28.76", default=False),
            SignalDef("zero_speed", _B, description="Zero speed (06.19 bit 0)", default=True),
            SignalDef("override_active", _B, description="Override (fire mode) active", default=False),
            SignalDef("forward", _B, description="Direction forward (FWD/REV ACT)", default=False),
            SignalDef("ro1", _B, description="RO1 contact closed", default=False),
            SignalDef("ro2", _B, description="RO2 contact closed", default=False),
            SignalDef("ro3", _B, description="RO3 contact closed", default=False),
            *[SignalDef(f"di{i}_status", _B, description=f"DI{i} state", default=False) for i in range(1, 7)],
            SignalDef("keypad_mode", "text", description="Off / Hand / Auto", default="auto"),
            SignalDef("status_text", "text", description="Drive status", default=""),
            SignalDef("warnings", "text", description="Active warnings", default=""),
            SignalDef("hand_ref", _A, "hertz", "Keypad (Hand) reference"),
        ]

    def read_standalone(self, inputs: Dict[str, Any], safety_terminal: int, safety_open: bool,
                        speed_wire_broken: bool) -> None:
        di = [bool(inputs[f"di{i}"]) for i in range(1, 7)]
        if 1 <= safety_terminal <= 6 and safety_open:
            di[safety_terminal - 1] = False
        self.inp.di = di
        self.inp.ai1_pct = 0.0 if speed_wire_broken else float(inputs["ai1_pct"])
        self.inp.efb_run = bool(inputs["efb_run"])
        self.inp.efb_ref1_pct = float(inputs["efb_ref1_pct"])
        self.inp.efb_run_enable = bool(inputs["efb_run_enable"])
        self.inp.efb_fault_reset = bool(inputs["efb_fault_reset"])
        self.inp.efb_idle_s = float(inputs["efb_idle_s"])

    def publish_standalone(self, s: Dict[str, Any], rng) -> None:
        p = self.p
        hist = self.fault_history + [0, 0, 0]
        s.update({
            "output_freq": round(self.freq, 1),
            "speed_pct": round(self.freq / p["freq_scaling"] * 100.0, 1),
            "motor_rpm": round(self.motor_rpm, 0),
            "motor_current": round(self.current + (rng.gauss(0, 0.03) if self.modulating else 0.0), 2),
            "torque_pct": round(self.torque_pu * 100.0, 1),
            "power_kw": round(self.power_kw, 2),
            "dc_bus_v": round(self.bus_v + rng.gauss(0, 0.8), 0),
            "output_voltage": round(self.output_voltage, 0),
            "drive_temp": round(self.drive_temp, 1),
            "motor_temp": round(self.motor_temp, 1),
            "kwh": round(self.kwh, 2),
            "run_hours": round(self.run_hours, 2),
            "fault_code": self.fault_code,
            "last_fault": hist[0],
            "prev_fault_1": hist[1],
            "prev_fault_2": hist[2],
            "ref_hz": round(self.ref_hz, 1),
            "ai1_v": round(self.inp.ai1_pct / 10.0, 2),
            "ao1_pct": round(self.ao1_pct(), 1),
            "running": self.modulating,
            "started": self.started,
            "fault_active": self.fault_code != 0,
            "warning_active": bool(self.warnings),
            "ready": self.ready,
            "at_setpoint": self.at_setpoint,
            "in_auto": self.mode == "auto",
            "run_enable_act": self._src(p["run_permissive"]),
            "start_interlock_ok": self._src(p["start_interlock_1"]),
            "ramp_in_zero": self.ramp_in_zero,
            "zero_speed": self.zero_speed,
            "override_active": self.override,
            "forward": self.modulating,
            "ro1": self.relay(p["ro1_source"]),
            "ro2": self.relay(p["ro2_source"]),
            "ro3": self.relay(p["ro3_source"]),
            "keypad_mode": self.mode,
            "status_text": self.status_text(),
            "warnings": "; ".join(self.warnings),
            "hand_ref": self.hand_ref,
        })
        for i in range(6):
            s[f"di{i + 1}_status"] = bool(self.inp.di[i])
