"""
Standalone ACH580-style drive running a direct-drive plenum fan.

The drive (components/vfd.py) is the part an AHU fan array reuses; this model
adds the motor/fan load and the field wiring around one drive so it can be
used on its own for drive start-up and troubleshooting training.

Control is decided by the drive's own parameters, exactly like the real one:

  hardwired:  20.01 = in1_start, 28.11 = ai1_scaled
              ALC BO -> DI1 terminal, ALC AO 0-10 V -> AI1 terminal,
              RO2 (running) / RO3 (fault, inverted) / AO1 -> ALC inputs
  fieldbus:   20.01 = fieldbus, 28.11 = efb_ref1
              ALC writes BV RUN/STOP CMD and AV INPUT REF 1 over BACnet
"""

from __future__ import annotations

from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef
from .components.vfd import FAULT_TEXT, VFD_DEFAULTS, VFD_INJECTABLE, VFD_PARAM_INFO, Vfd

_B = "binary"
_A = "analog"


class VfdFan(EquipmentModel):
    TYPE = "vfd_fan"
    DESCRIPTION = "ACH580-style VFD with direct-drive plenum fan"
    MAX_SUBSTEP = 0.2

    DEFAULTS = {
        **VFD_DEFAULTS,
        # fan / field (not drive parameters)
        "fan_load_pu": 0.85,          # load torque at 100 % speed, pu of motor rated torque
        "design_cfm": 10000.0,        # airflow at 100 % speed
        "design_sp": 2.5,             # in. w.c. fan static at 100 % speed
        "safety_di": 0,               # DI the safety circuit (freezestat/smoke) is wired to, 0 = none
    }

    PARAM_INFO = {
        **VFD_PARAM_INFO,
        "fan_load_pu": ParamInfo("", "Fan load at 100 % speed", "Conditions", "pu", minimum=0.05, maximum=1.5),
        "line_voltage": ParamInfo("", "Supply voltage", "Conditions", "V", minimum=200, maximum=690),
        "ambient_temp": ParamInfo("", "Drive ambient", "Conditions", "°C", minimum=-10, maximum=60),
    }
    CONDITIONS = ["fan_load_pu", "line_voltage", "ambient_temp"]
    ACTIONS = ["hand", "off", "auto", "reset", "hand_ref"]

    INPUTS = [
        # DI4 defaults closed: 20.41 Start interlock 1 = DI4 out of the box, normally jumpered or wired
        *[SignalDef(f"di{i}", _B, description=f"DI{i} terminal", default=(i == 4)) for i in range(1, 7)],
        SignalDef("ai1_pct", _A, "percent", "AI1 terminal, % of 10 V", 0.0),
        SignalDef("efb_run", _B, description="Fieldbus RUN/STOP CMD", default=False),
        SignalDef("efb_ref1_pct", _A, "percent", "Fieldbus INPUT REF 1", 0.0),
        SignalDef("efb_run_enable", _B, description="Fieldbus RUN ENA CMD", default=True),
        SignalDef("efb_fault_reset", _B, description="Fieldbus FAULT RESET", default=False),
        SignalDef("efb_idle_s", _A, "seconds", "Time since the drive last heard the fieldbus", 0.0),
    ]

    SIGNALS = [
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
        SignalDef("airflow_cfm", _A, "cubicFeetPerMinute", "Fan airflow"),
        SignalDef("fan_sp", _A, "inchesOfWater", "Fan static pressure"),
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
        SignalDef("fan_rotating", _B, description="Fan wheel turning", default=False),
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

    FAULTS = [
        *[FaultDef(name, label, "Drive") for name, label in VFD_INJECTABLE.items()],
        FaultDef("safety_circuit_open", "Safety circuit open (freezestat / smoke)", "Field wiring",
                 description="Opens the DI set by safety_di"),
        FaultDef("ai1_wire_broken", "AI1 speed signal wire broken", "Field wiring"),
        FaultDef("coupling_failed", "Motor-fan coupling failed (motor spins, no air)", "Motor / fan"),
        FaultDef("bearing_worn", "Worn fan bearings (high load)", "Motor / fan"),
        FaultDef("motor_locked", "Fan wheel seized", "Motor / fan"),
        FaultDef("damper_closed", "Discharge damper closed", "Motor / fan"),
    ]

    # ------------------------------------------------------------------

    def reset(self) -> None:
        self._inject: set = set()
        self.vfd = Vfd(self.p, self._inject)

    def _sync_inject(self) -> None:
        self._inject.clear()
        self._inject.update(name for name in VFD_INJECTABLE if self.fault(name))

    def action(self, name, value=None):
        self._sync_inject()
        self.vfd.keypad(name, value)
        self._publish()

    def _load(self, s: float) -> float:
        k = self.p["fan_load_pu"]
        if self.fault("coupling_failed"):
            return 0.03 * s
        if self.fault("motor_locked"):
            return 4.0
        if self.fault("bearing_worn"):
            k *= 1.25
        if self.fault("damper_closed"):
            k *= 0.55
        return k * s * s

    def _step(self, h: float) -> None:
        v = self.vfd
        self._sync_inject()
        v.powered = self.power_ok                         # ATS power (generator plant)

        di = [bool(self.inputs[f"di{i}"]) for i in range(1, 7)]
        sdi = int(self.p["safety_di"])
        if 1 <= sdi <= 6 and self.fault("safety_circuit_open"):
            di[sdi - 1] = False
        v.inp.di = di
        v.inp.ai1_pct = 0.0 if self.fault("ai1_wire_broken") else float(self.inputs["ai1_pct"])
        v.inp.efb_run = bool(self.inputs["efb_run"])
        v.inp.efb_ref1_pct = float(self.inputs["efb_ref1_pct"])
        v.inp.efb_run_enable = bool(self.inputs["efb_run_enable"])
        v.inp.efb_fault_reset = bool(self.inputs["efb_fault_reset"])
        v.inp.efb_idle_s = float(self.inputs["efb_idle_s"])
        v.step(h, self._load)

    def _publish(self) -> None:
        v, p, s = self.vfd, self.p, self.signals
        hist = v.fault_history + [0, 0, 0]
        speed = v.speed_pu
        air = 0.0 if (self.fault("coupling_failed") or self.fault("motor_locked")) else speed
        if self.fault("damper_closed"):
            air *= 0.1
        s.update({
            "output_freq": round(v.freq, 1),
            "speed_pct": round(v.freq / p["freq_scaling"] * 100.0, 1),
            "motor_rpm": round(v.motor_rpm, 0),
            "motor_current": round(v.current + (self.rng.gauss(0, 0.03) if v.modulating else 0.0), 2),
            "torque_pct": round(v.torque_pu * 100.0, 1),
            "power_kw": round(v.power_kw, 2),
            "dc_bus_v": round(v.bus_v + self.rng.gauss(0, 0.8), 0),
            "output_voltage": round(v.output_voltage, 0),
            "drive_temp": round(v.drive_temp, 1),
            "motor_temp": round(v.motor_temp, 1),
            "kwh": round(v.kwh, 2),
            "run_hours": round(v.run_hours, 2),
            "fault_code": v.fault_code,
            "last_fault": hist[0],
            "prev_fault_1": hist[1],
            "prev_fault_2": hist[2],
            "ref_hz": round(v.ref_hz, 1),
            "ai1_v": round(v.inp.ai1_pct / 10.0, 2),
            "ao1_pct": round(v.ao1_pct(), 1),
            "airflow_cfm": round(p["design_cfm"] * air, 0),
            "fan_sp": round(p["design_sp"] * (speed ** 2) * (1.6 if self.fault("damper_closed") else 1.0), 2),
            "running": v.modulating,
            "started": v.started,
            "fault_active": v.fault_code != 0,
            "warning_active": bool(v.warnings),
            "ready": v.ready,
            "at_setpoint": v.at_setpoint,
            "in_auto": v.mode == "auto",
            "run_enable_act": v._src(p["run_permissive"]),
            "start_interlock_ok": v._src(p["start_interlock_1"]),
            "ramp_in_zero": v.ramp_in_zero,
            "zero_speed": v.zero_speed,
            "override_active": v.override,
            "fan_rotating": speed > 0.03 and not self.fault("coupling_failed") and not self.fault("motor_locked"),
            "forward": v.modulating,
            "ro1": v.relay(p["ro1_source"]),
            "ro2": v.relay(p["ro2_source"]),
            "ro3": v.relay(p["ro3_source"]),
            "keypad_mode": v.mode,
            "status_text": v.status_text(),
            "warnings": "; ".join(v.warnings),
            "hand_ref": v.hand_ref,
        })
        for i in range(6):
            s[f"di{i + 1}_status"] = bool(v.inp.di[i])

    @staticmethod
    def fault_text(code: int) -> str:
        return FAULT_TEXT.get(int(code), "")
