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

    # largest internal integration step (simulated seconds)
    MAX_SUBSTEP = 0.5

    def __init__(self, params: Optional[Dict[str, Any]] = None, seed: Optional[int] = None):
        params = params or {}
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"{self.TYPE}: unknown parameter(s) {sorted(unknown)}")
        self.p: Dict[str, Any] = {**self.DEFAULTS, **params}
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

    def set_param(self, name: str, value: Any) -> None:
        if name not in self.p:
            raise KeyError(f"{self.TYPE}: no parameter named {name!r}")
        self.p[name] = type(self.DEFAULTS[name])(value)

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
