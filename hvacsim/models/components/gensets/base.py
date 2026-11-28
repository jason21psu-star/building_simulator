"""
The generator set interface (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 6).

The generator plant runs the paralleling, the bus and the ATSs.  It uses a
genset only through this surface:

    gs = genset_class("cummins_pcc33")(tag, cfg, faults, rng)
    gs.master_call = ...                 # load demand from the paralleling master
    gs.step(h, t_amb_c, bus_f)           # bus_f None when the bus is dead
    gs.kw, gs.kvar = ...                 # the plant's load share
    gs.cb_closed = ...                   # the plant closes / opens the genset breaker
    gs.plant_event("overload")           # the plant's protection; the genset maps it to its own code
    gs.at_rated, gs.running, gs.capacity(), gs.shutdown, gs.in_auto, gs.start_request
    gs.modbus_registers(pf) / gs.modbus_write(reg, value)     (PROTOCOL "modbus_tcp")
"""

from __future__ import annotations

from typing import Dict, Type

from ..vendor import VendorEquipment, family_class
from .core import ENGINE_EVENTS, RATED_RPM, GensetCore  # noqa: F401

GENERIC_FAULTS = {
    "fail_to_crank": "Fail to crank",
    "fail_to_start": "Cranks, will not fire",
    "high_coolant": "High coolant temperature",
    "low_oil": "Low oil pressure",
    "overspeed": "Overspeed on start",
    "high_ac_volts": "High AC voltage",
    "low_ac_volts": "Low AC voltage",
    "high_current": "Overcurrent",
    "low_battery": "Low battery voltage",
    "weak_battery": "Weak battery: slow crank",
    "low_gas": "Low gas supply pressure",
    "fail_to_sync": "Fails to synchronize",
    "cb_fail_close": "Genset breaker fails to close",
    "charger_fail": "Battery charger AC failure",
    "comm_loss": "Communication loss (no response)",
}

# protection the paralleling plant applies; each genset maps them to its own fault codes
PLANT_EVENTS = ("overload", "underfrequency", "cb_fail_to_close", "fail_to_sync")

WRITE_ROLES = ("remote_start", "fault_reset")
READ_ROLES = ("running", "at_rated", "kw", "hz", "volts", "shutdown", "warning", "in_auto", "fault_code")


class GeneratorSet(GensetCore, VendorEquipment):
    FAMILY = "genset"
    GENERIC_FAULTS = GENERIC_FAULTS
    WRITE_ROLES = WRITE_ROLES
    READ_ROLES = READ_ROLES
    PROTOCOL = "modbus_tcp"
    EVENT_CODES: Dict[str, int] = {}         # engine and plant event -> the vendor's fault code

    @classmethod
    def check(cls) -> None:
        super().check()
        for e in ENGINE_EVENTS + PLANT_EVENTS:
            if e not in cls.EVENT_CODES:
                raise TypeError(f"{cls.DISPLAY}: event {e!r} has no fault code")

    @classmethod
    def role_register(cls, role: str):
        """The register (as the manual numbers it) behind a BAS role, or None."""
        key = cls.ROLES.get(role)
        return next((o[1] for o in cls.OBJECTS if o[3] == key), None) if key else None

    # ----- paralleling hooks (the plant's side)
    master_call = False
    load_demand_stopped = False
    cb_closed = False

    def plant_event(self, name: str) -> None:
        raise NotImplementedError

    def plant_event_clear(self, name: str) -> None:
        raise NotImplementedError

    def plant_event_active(self, name: str) -> bool:
        raise NotImplementedError

    @property
    def at_rated(self) -> bool:
        raise NotImplementedError

    @property
    def start_request(self) -> bool:
        raise NotImplementedError

    @property
    def in_auto(self) -> bool:
        raise NotImplementedError

    def capacity(self) -> float:
        raise NotImplementedError

    def step(self, h: float, t_amb: float, bus_f) -> None:
        raise NotImplementedError

    def fault_reset(self) -> bool:
        raise NotImplementedError

    starts = 0                               # start attempts

    def display_code(self) -> int:
        """The fault code the page shows: the active shutdown, else the highest active warning, else 0."""
        raise NotImplementedError

    def switch_text(self) -> str:
        """The control's mode selector as its panel shows it (Off / Auto / Manual ...)."""
        raise NotImplementedError

    @classmethod
    def decode(cls, role: str, regs: Dict[int, int]):
        """A read role's value from registers read over Modbus (the BAS's view)."""
        raise NotImplementedError

    def role(self, role: str, points=None):
        return self.decode(role, self.modbus_registers(1.0))

    def modbus_registers(self, pf: float) -> Dict[int, int]:
        raise NotImplementedError

    def modbus_write(self, reg: int, value: int) -> bool:
        raise NotImplementedError


def genset_class(model: str) -> Type[GeneratorSet]:
    return family_class("genset", model)  # type: ignore[return-value]
