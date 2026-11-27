"""
Standalone drive (any make behind the drive interface) running a direct-drive plenum fan.

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
from .components.drives import build_drive, drive_class

_B = "binary"
_A = "analog"

FAN_DEFAULTS = {
    # fan / field (not drive parameters)
    "fan_load_pu": 0.85,          # load torque at 100 % speed, pu of motor rated torque
    "design_cfm": 10000.0,        # airflow at 100 % speed
    "design_sp": 2.5,             # in. w.c. fan static at 100 % speed
    "safety_di": 0,               # digital input the safety circuit (freezestat/smoke) is wired to, 0 = none
}
FAN_PARAM_INFO = {
    "fan_load_pu": ParamInfo("", "Fan load at 100 % speed", "Conditions", "pu", minimum=0.05, maximum=1.5),
    "line_voltage": ParamInfo("", "Supply voltage", "Conditions", "V", minimum=200, maximum=690),
    "ambient_temp": ParamInfo("", "Drive ambient", "Conditions", "°C", minimum=-10, maximum=60),
}
FIELD_FAULTS = [
    ("safety_circuit_open", "Safety circuit open (freezestat / smoke)", "Field wiring", "Opens the input set by safety_di"),
    ("coupling_failed", "Motor-fan coupling failed (motor spins, no air)", "Motor / fan", ""),
    ("bearing_worn", "Worn fan bearings (high load)", "Motor / fan", ""),
    ("motor_locked", "Fan wheel seized", "Motor / fan", ""),
    ("damper_closed", "Discharge damper closed", "Motor / fan", ""),
]


class VfdFan(EquipmentModel):
    """Standalone drive + motor + fan.  The drive make/model comes from params["drive_model"]."""

    TYPE = "vfd_fan"
    DESCRIPTION = "VFD with direct-drive plenum fan (ACH580, Z1000, ...)"
    MAX_SUBSTEP = 0.2
    CONDITIONS = ["fan_load_pu", "line_voltage", "ambient_temp"]
    DEFAULTS = {"drive_model": "ach580"}         # (per instance: the selected drive's parameters are added)

    def __init__(self, params=None, seed=None):
        params = dict(params or {})
        cls = drive_class(params.get("drive_model", "ach580"))
        self.drive_cls = cls
        self.DESCRIPTION = f"{cls.DISPLAY} with direct-drive plenum fan"
        self.DEFAULTS = {**cls.DEFAULTS, **FAN_DEFAULTS, "drive_model": cls.MODEL}
        self.PARAM_INFO = {**cls.PARAM_INFO, **FAN_PARAM_INFO}
        self.ACTIONS = list(cls.KEYPAD)
        proto = cls(cls.params_for(), set())
        self.INPUTS = proto.standalone_inputs()
        self.SIGNALS = proto.standalone_signals() + [
            SignalDef("airflow_cfm", _A, "cubicFeetPerMinute", "Fan airflow"),
            SignalDef("fan_sp", _A, "inchesOfWater", "Fan static pressure"),
            SignalDef("fan_rotating", _B, description="Fan wheel turning", default=False),
            SignalDef("drive_model", "text", description="Drive make / model", default=cls.DISPLAY),
        ]
        wire_name, wire_label = cls.SPEED_WIRE_FAULT
        self.FAULTS = [*[FaultDef(name, label, "Drive") for name, label in cls.INJECTABLE.items()],
                       FaultDef(wire_name, wire_label, "Field wiring"),
                       *[FaultDef(n, l, g, description=d) for n, l, g, d in FIELD_FAULTS]]
        self._wire_fault = wire_name
        super().__init__(params, seed)

    # ------------------------------------------------------------------

    def reset(self) -> None:
        self._inject: set = set()
        # the drive shares the model's parameter dict, so the web page / BACnet edit it live
        self.vfd = self.drive_cls(self.p, self._inject)

    def _sync_inject(self) -> None:
        self._inject.clear()
        self._inject.update(name for name in self.drive_cls.INJECTABLE if self.fault(name))

    def set_param(self, name, value):
        if name in self.drive_cls.DEFAULTS:
            self.p[name] = self.drive_cls.check_param(name, value)
            return
        super().set_param(name, value)

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
        v.read_standalone(self.inputs, int(self.p["safety_di"]), self.fault("safety_circuit_open"),
                          self.fault(self._wire_fault))
        v.step(h, self._load)

    def _publish(self) -> None:
        v, p, s = self.vfd, self.p, self.signals
        v.publish_standalone(s, self.rng)
        speed = v.speed_pu
        air = 0.0 if (self.fault("coupling_failed") or self.fault("motor_locked")) else speed
        if self.fault("damper_closed"):
            air *= 0.1
        s["airflow_cfm"] = round(p["design_cfm"] * air, 0)
        s["fan_sp"] = round(p["design_sp"] * (speed ** 2) * (1.6 if self.fault("damper_closed") else 1.0), 2)
        s["fan_rotating"] = speed > 0.03 and not self.fault("coupling_failed") and not self.fault("motor_locked")
        s["drive_model"] = self.drive_cls.DISPLAY

    def fault_text(self, code) -> str:
        return self.drive_cls.FAULT_TEXT.get(code, "") if hasattr(self.drive_cls, "FAULT_TEXT") else ""
