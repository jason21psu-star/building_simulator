"""Boilers behind one interface (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 5)."""

from .base import GENERIC_FAULTS, MODE_ROLE, READ_ROLES, WRITE_ROLES, Boiler, boiler_class  # noqa: F401
from . import lochinvar_crest  # noqa: F401  (registers "lochinvar_crest")
