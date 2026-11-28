"""Chillers behind one interface (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 4)."""

from .base import GENERIC_FAULTS, READ_ROLES, WRITE_ROLES, Chiller, chiller_class  # noqa: F401
from . import york_yk  # noqa: F401  (registers "york_yk")
from . import trane_cvhe  # noqa: F401,E402  (registers "trane_cvhe", "trane_cvhe_modbus")
