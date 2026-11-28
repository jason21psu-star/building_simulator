"""
The chiller interface (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 4).

    ch = chiller_class("york_yk")(tag, cfg, faults)
    ch.powered = ...
    ch.bas_write("rem_run", True)                     # the vendor's own command objects
    ch.step(h, evap_gpm, cond_gpm, ewt, ecwt, t_room)
    ch.lchwt, ch.lcwt, ch.q (Btu/h), ch.kw, ch.running, ch.alarm, ch.evap_fs, ch.cond_fs
"""

from __future__ import annotations

from typing import Type

from ..vendor import VendorEquipment, family_class
from .core import ChillerCore

GENERIC_FAULTS = {
    "evap_fs_failed": "Chilled water flow switch failed open",
    "cond_fs_failed": "Condenser water flow switch failed open",
    "low_charge": "Low refrigerant charge",
    "cond_fouled": "Condenser tubes fouled (high approach)",
    "evap_fouled": "Evaporator tubes fouled",
    "starter_fault": "Starter / VSD fault (trips)",
    "comm_loss": "BAS interface offline",
}

WRITE_ROLES = ("run_cmd", "chws_sp", "demand_limit")
READ_ROLES = ("running", "alarm", "lchwt", "echwt", "lcwt", "ecwt", "pct_load", "kw", "ready", "local")


class Chiller(ChillerCore, VendorEquipment):
    FAMILY = "chiller"
    GENERIC_FAULTS = GENERIC_FAULTS
    WRITE_ROLES = WRITE_ROLES
    READ_ROLES = READ_ROLES

    def step(self, h: float, evap_gpm: float, cond_gpm: float, ewt: float, ecwt: float,
             t_room: float = 75.0) -> None:
        raise NotImplementedError

    @property
    def running(self) -> bool:
        raise NotImplementedError

    @property
    def active_setpoint(self) -> float:
        """The leaving chilled water setpoint the chiller is controlling to (remote or local)."""
        raise NotImplementedError

    @property
    def alarm(self) -> bool:
        """A shutdown that needs attention (latched safety / lockout)."""
        raise NotImplementedError


def chiller_class(model: str) -> Type[Chiller]:
    return family_class("chiller", model)  # type: ignore[return-value]
