"""
Base class for equipment process models.

A model knows nothing about BACnet.  It exposes:

  INPUTS   - values written *into* the equipment (e.g. a valve command relay)
  SIGNALS  - values the equipment produces (temperatures, amps, statuses)
  FAULTS   - things an instructor can break
  DEFAULTS - tunable physical parameters (overridable from the YAML config)

The BACnet layer maps INPUTS and SIGNALS to BACnet objects using the YAML
config, so adding/removing points (change orders) never touches model code.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SignalDef:
    name: str
    kind: str  # "analog" or "binary"
    units: str = "noUnits"  # BACnet EngineeringUnits name
    description: str = ""
    default: Any = 0.0


@dataclass
class FaultDef:
    name: str
    label: str
    group: str
    kind: str = "bool"  # "bool" or "number"
    default: Optional[float] = None
    units: str = ""
    description: str = ""


@dataclass
class ParamInfo:
    """Describes a parameter for the web page (e.g. a drive keypad parameter)."""
    id: str            # e.g. "28.72" for a drive parameter, or "" for a plain setting
    label: str
    group: str = ""
    units: str = ""
    choices: Optional[List[str]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


def coerce(value: Any, like: Any) -> Any:
    """Convert a value (often a string from the web page or YAML) to the type of `like`."""
    if isinstance(like, bool):
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on", "active"):
                return True
            if v in ("0", "false", "no", "off", "inactive", ""):
                return False
            raise ValueError(f"not a boolean: {value!r}")
        return bool(value)
    if isinstance(like, int) and not isinstance(like, bool):
        return int(float(value))
    if isinstance(like, float):
        return float(value)
    return type(like)(value)


@dataclass
class FaultState:
    active: bool = False
    value: Optional[float] = None


class EquipmentModel:
    TYPE: str = ""
    DESCRIPTION: str = ""
    DEFAULTS: Dict[str, Any] = {}
    INPUTS: List[SignalDef] = []
    SIGNALS: List[SignalDef] = []
    FAULTS: List[FaultDef] = []
    PARAM_INFO: Dict[str, ParamInfo] = {}      # parameters shown/editable on the web page
    CONDITIONS: List[str] = []                 # parameters shown in the "Conditions" panel
    ACTIONS: List[str] = []                    # web page buttons, e.g. keypad Hand/Off/Auto

    # largest internal integration step (simulated seconds)
    MAX_SUBSTEP = 0.5

    def __init__(self, params: Optional[Dict[str, Any]] = None, seed: Optional[int] = None):
        params = params or {}
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"{self.TYPE}: unknown parameter(s) {sorted(unknown)}")
        self.p: Dict[str, Any] = dict(self.DEFAULTS)
        for k, v in params.items():
            self.p[k] = self._checked(k, v)
        self.rng = random.Random(seed)
        self.sim_time = 0.0
        self.inputs: Dict[str, Any] = {d.name: d.default for d in self.INPUTS}
        self.signals: Dict[str, Any] = {d.name: d.default for d in self.SIGNALS}
        self.faults: Dict[str, FaultState] = {
            f.name: FaultState(False, f.default) for f in self.FAULTS
        }
        self.reset()
        self._publish()

    # ----- metadata helpers -------------------------------------------------

    @classmethod
    def signal_def(cls, name: str) -> SignalDef:
        for d in list(cls.SIGNALS) + list(cls.INPUTS):
            if d.name == name:
                return d
        raise KeyError(f"{cls.TYPE}: no signal or input named {name!r}")

    # ----- runtime API ------------------------------------------------------

    def set_input(self, name: str, value: Any) -> None:
        if name not in self.inputs:
            raise KeyError(f"{self.TYPE}: no input named {name!r}")
        self.inputs[name] = value

    def _checked(self, name: str, value: Any) -> Any:
        value = coerce(value, self.DEFAULTS[name])
        info = self.PARAM_INFO.get(name)
        if info is not None:
            if info.choices and value not in info.choices:
                raise ValueError(f"{name}: {value!r} is not one of {info.choices}")
            if info.minimum is not None and value < info.minimum:
                raise ValueError(f"{name}: minimum is {info.minimum}")
            if info.maximum is not None and value > info.maximum:
                raise ValueError(f"{name}: maximum is {info.maximum}")
        return value

    def set_param(self, name: str, value: Any) -> None:
        if name not in self.p:
            raise KeyError(f"{self.TYPE}: no parameter named {name!r}")
        self.p[name] = self._checked(name, value)

    def action(self, name: str, value: Any = None) -> None:
        """Web page button (e.g. keypad key).  Override in subclasses."""
        raise KeyError(f"{self.TYPE}: no action named {name!r}")

    def set_fault(self, name: str, active: bool, value: Optional[float] = None) -> None:
        if name not in self.faults:
            raise KeyError(f"{self.TYPE}: no fault named {name!r}")
        state = self.faults[name]
        state.active = bool(active)
        if value is not None:
            state.value = float(value)

    def clear_faults(self) -> None:
        for f in self.FAULTS:
            self.faults[f.name] = FaultState(False, f.default)

    def fault(self, name: str) -> bool:
        return self.faults[name].active

    def fault_value(self, name: str) -> float:
        return self.faults[name].value

    def step(self, dt: float) -> None:
        """Advance the model dt simulated seconds."""
        remaining = dt
        while remaining > 1e-9:
            h = min(self.MAX_SUBSTEP, remaining)
            self._step(h)
            self.sim_time += h
            remaining -= h
        self._publish()

    # ----- subclass hooks ---------------------------------------------------

    def reset(self) -> None:
        """Initialise internal state."""

    def _step(self, h: float) -> None:
        raise NotImplementedError

    def _publish(self) -> None:
        """Copy internal state into self.signals."""
