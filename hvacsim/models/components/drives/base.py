"""
The drive interface (docs/DRIVE_INTERFACE_DESIGN.md, section 3).

Equipment models use drives only through this surface, so a pump, a fan array
or a standalone drive can be an ABB ACH580, a Yaskawa Z1000, or any drive
registered later, by configuration alone.

    drive = build_drive("z1000", common={...}, preset="hardwired", overrides={...}, inject=set())
    drive.powered = ...
    drive.wire(start=..., safeties=..., speed_ref=...)       # roles, not terminals
    drive.step(h, load_torque)
    drive.speed_pu, drive.freq, drive.current, drive.power_kw ...
    drive.role_out("fault_out")                               # closed = healthy
    drive.role_out("speed_fb")                                # % of span
    drive.keypad("hand") / ("hand_ref", 40.0)
    drive.status_line()                                       # web page status string
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ...base import ParamInfo, SignalDef, coerce

# wiring roles (equipment side) - each drive maps them to its own terminals
ROLES_IN = ("start", "safeties", "damper_interlock", "speed_ref")
ROLES_OUT = ("running_out", "fault_out", "speed_fb", "damper_out")

# instructor faults every drive must support (it may add its own)
GENERIC_FAULTS = {
    "overcurrent_trip": "Overcurrent trip (output short / ground)",
    "earth_leakage": "Motor insulation failure (earth leakage)",
    "input_phase_loss": "Input phase loss",
    "mains_undervoltage": "Low supply voltage",
    "cooling_fan_failed": "Drive cooling fan failed",
}

# drive-neutral settings an equipment model states (section 5 of the design)
COMMON_KEYS = ("motor_amps", "motor_kw", "max_amps", "inertia_h", "min_hz", "accel_s", "decel_s")


class Drive:
    """Interface + shared helpers.  Concrete drives subclass this (alongside their own engine)."""

    MODEL = ""
    MAKE = ""
    DISPLAY = ""
    SHORT = ""                                  # e.g. "ACH580" - used in parameter group labels
    DEFAULTS: Dict[str, Any] = {}
    PARAM_INFO: Dict[str, ParamInfo] = {}
    INJECTABLE: Dict[str, str] = dict(GENERIC_FAULTS)
    PRESETS: Dict[str, Dict[str, Any]] = {}     # application presets, e.g. "hardwired"
    COMMON_MAP: Dict[str, str] = {}             # common key -> parameter (when a plain copy)
    KEYPAD = ["hand", "off", "auto", "reset", "hand_ref"]
    TERMINALS: Dict[str, List[str]] = {}        # di / ai / relay / ao terminal names
    ROLE_TERMINALS: Dict[str, Optional[str]] = {}

    # ---------------------------------------------------------------- construction
    @classmethod
    def params_for(cls, common: Optional[Dict[str, Any]] = None, preset: Optional[str] = None,
                   overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Parameter set: manual defaults, then the application preset, the common settings, the overrides."""
        dp = dict(cls.DEFAULTS)
        if preset:
            if preset not in cls.PRESETS:
                raise ValueError(f"{cls.DISPLAY}: no preset {preset!r} ({list(cls.PRESETS)})")
            dp.update(cls.PRESETS[preset])
        dp.update(cls.map_common(common or {}))
        for k, v in (overrides or {}).items():
            if k == "model":
                continue
            if k not in cls.DEFAULTS:
                raise ValueError(f"{cls.DISPLAY}: no drive parameter {k!r} - check the parameter names for this drive")
            dp[k] = v
        for k in list(dp):
            if k in cls.DEFAULTS:
                dp[k] = coerce(dp[k], cls.DEFAULTS[k])
        return dp

    @classmethod
    def map_common(cls, common: Dict[str, Any]) -> Dict[str, Any]:
        unknown = set(common) - set(COMMON_KEYS)
        if unknown:
            raise ValueError(f"unknown common drive setting(s) {sorted(unknown)}")
        return {cls.COMMON_MAP[k]: v for k, v in common.items() if k in cls.COMMON_MAP}

    @classmethod
    def check_param(cls, key: str, value: Any) -> Any:
        if key not in cls.DEFAULTS:
            raise KeyError(f"{cls.DISPLAY}: no drive parameter {key!r}")
        value = coerce(value, cls.DEFAULTS[key])
        info = cls.PARAM_INFO.get(key)
        if info and info.choices and value not in info.choices:
            raise ValueError(f"{key}: {value!r} is not one of {info.choices}")
        if info and info.minimum is not None and value < info.minimum:
            raise ValueError(f"{key}: minimum is {info.minimum}")
        if info and info.maximum is not None and value > info.maximum:
            raise ValueError(f"{key}: maximum is {info.maximum}")
        return value

    # ---------------------------------------------------------------- equipment side
    def wire(self, start: bool = False, safeties: bool = True, damper_interlock: bool = True,
             speed_ref: float = 0.0) -> None:
        raise NotImplementedError

    def role_out(self, role: str):
        raise NotImplementedError

    def status_line(self) -> str:
        """mode|status|Hz|A|warnings|hand ref - what the equipment pages show for a drive."""
        return (f"{self.mode}|{self.status_text()}|{self.freq:.1f}|{self.current:.1f}|"
                f"{'; '.join(self.warnings)}|{self.hand_ref:.0f}")

    @property
    def fault_active(self) -> bool:
        return bool(self.fault_code)

    # ---------------------------------------------------------------- standalone drive (vfd_fan)
    def standalone_inputs(self) -> List[SignalDef]:
        raise NotImplementedError

    def standalone_signals(self) -> List[SignalDef]:
        raise NotImplementedError

    def read_standalone(self, inputs: Dict[str, Any], safety_terminal: int, safety_open: bool,
                        speed_wire_broken: bool) -> None:
        raise NotImplementedError

    def publish_standalone(self, s: Dict[str, Any], rng) -> None:
        raise NotImplementedError

    SPEED_WIRE_FAULT: Tuple[str, str] = ("speed_wire_broken", "Speed signal wire broken")


# ------------------------------------------------------------------------- registry

_REGISTRY: Dict[str, type] = {}


def register(cls: type) -> type:
    _REGISTRY[cls.MODEL] = cls
    return cls


def drive_class(model: Optional[str]) -> type:
    name = str(model or "ach580").strip().lower()
    if name not in _REGISTRY:
        raise ValueError(f"unknown drive model {model!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def drive_models() -> List[str]:
    return sorted(_REGISTRY)


def build_drive(model: Optional[str], common: Optional[Dict[str, Any]] = None, preset: Optional[str] = None,
                overrides: Optional[Dict[str, Any]] = None, inject: Optional[Set[str]] = None):
    cls = drive_class((overrides or {}).get("model") or model)
    return cls(cls.params_for(common, preset, overrides), inject if inject is not None else set())


def param_items(prefix: str, label: str, drive) -> List[tuple]:
    """(dotted name, ParamInfo, value) rows for an equipment page."""
    cls = type(drive)
    return [(f"{prefix}.{k}", ParamInfo(i.id, i.label, f"{label} drive ({cls.SHORT})", i.units, i.choices,
                                         i.minimum, i.maximum), drive.p[k])
            for k, i in cls.PARAM_INFO.items()]


def motor_for_hp(hp: float, inertia: float) -> Dict[str, Any]:
    """Common motor settings for a 460 V motor of `hp` (the plants' sizing rule)."""
    amps = round(1.28 * hp + 1.0, 0)
    return {"motor_amps": amps, "motor_kw": round(hp * 0.746, 1), "max_amps": round(1.4 * amps, 0), "inertia_h": inertia}


LoadFn = Callable[[float], float]
