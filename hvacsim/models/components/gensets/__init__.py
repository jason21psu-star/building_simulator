"""Generator sets behind one interface (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 6)."""

from .base import GENERIC_FAULTS, PLANT_EVENTS, READ_ROLES, WRITE_ROLES, GeneratorSet, genset_class  # noqa: F401
from .core import ENGINE_EVENTS  # noqa: F401
from . import cummins_pcc33  # noqa: F401  (registers "cummins_pcc33")
