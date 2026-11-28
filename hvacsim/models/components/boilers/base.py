"""
The boiler interface (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 5).

    b = boiler_class("lochinvar_crest")(tag, cfg, faults, rng, t_init=...)
    b.powered, b.louver_ok, b.gas_ok, b.comm_ok = ...
    b.bas_write("bms_enable", True)                   # the vendor's own command objects
    t_leaving = b.step(h, gpm, ewt, t_room)
    b.q (Btu/h to water), b.gas_mbh, b.flame, b.rate, b.alarm, b.pump_request, b.flow_switch()

BMS control modes: "rate" (the BAS writes enable + a firing rate) or "setpoint"
(enable + a supply setpoint; the boiler's own controller modulates).  Each
vendor lists the modes it supports in BMS_MODES; the plant setting
boiler_bms_mode picks one.
"""

from __future__ import annotations

from typing import Tuple, Type

from ..vendor import VendorEquipment, family_class
from .core import BoilerCore

GENERIC_FAULTS = {
    "ignition_fail": "No ignition (locks out after its tries)",
    "flame_unstable": "Unstable flame / dirty flame sensor",
    "high_limit_trip": "Manual reset high limit tripped",
    "lwco_trip": "Low water cutoff tripped",
    "flow_sw_open": "Flow switch failed open",
    "flow_sw_stuck": "Flow switch stuck closed (fires without flow)",
    "flue_blocked": "Blocked flue / condensate drain",
    "gas_valve": "Gas valve fault",
    "hx_scaled": "Scaled heat exchanger (high flue temp, low efficiency)",
    "supply_sensor_open": "Supply sensor open",
    "supply_sensor_short": "Supply sensor shorted",
    "return_sensor_open": "Return sensor open",
    "return_sensor_short": "Return sensor shorted",
    "comm_loss": "BMS interface offline",
}

WRITE_ROLES = ("enable",)                 # plus rate_cmd / sp_cmd for the modes the vendor supports
READ_ROLES = ("firing", "flame", "supply_t", "return_t", "alarm", "pump_request", "flow_switch", "local")
MODE_ROLE = {"rate": "rate_cmd", "setpoint": "sp_cmd"}


class Boiler(BoilerCore, VendorEquipment):
    FAMILY = "boiler"
    GENERIC_FAULTS = GENERIC_FAULTS
    WRITE_ROLES = WRITE_ROLES
    READ_ROLES = READ_ROLES
    BMS_MODES: Tuple[str, ...] = ("rate",)

    louver_ok = True
    gas_ok = True

    @classmethod
    def check(cls) -> None:
        super().check()
        if not cls.BMS_MODES or any(m not in MODE_ROLE for m in cls.BMS_MODES):
            raise TypeError(f"{cls.DISPLAY}: BMS_MODES must be from {list(MODE_ROLE)}")
        for m in cls.BMS_MODES:
            k = cls.ROLES.get(MODE_ROLE[m])
            if k is None or k not in cls.cmd_keys():
                raise TypeError(f"{cls.DISPLAY}: mode {m!r} needs role {MODE_ROLE[m]!r} on a command object")

    @classmethod
    def check_mode(cls, mode: str) -> str:
        mode = str(mode or "rate").lower()
        if mode not in cls.BMS_MODES:
            raise ValueError(f"{cls.DISPLAY} does not support BMS mode {mode!r} (supports {list(cls.BMS_MODES)})")
        return mode

    def step(self, h: float, gpm: float, ewt: float, t_room: float = 75.0) -> float:
        raise NotImplementedError

    @property
    def alarm(self) -> bool:
        raise NotImplementedError

    @property
    def pump_request(self) -> bool:
        raise NotImplementedError

    def flow_switch(self) -> bool:
        """The boiler's own flow switch, as its control sees it."""
        raise NotImplementedError


def boiler_class(model: str) -> Type[Boiler]:
    return family_class("boiler", model)  # type: ignore[return-value]
