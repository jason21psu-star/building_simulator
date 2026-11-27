"""Variable frequency drives behind one interface (docs/DRIVE_INTERFACE_DESIGN.md)."""

from .base import (COMMON_KEYS, GENERIC_FAULTS, ROLES_IN, ROLES_OUT, Drive, build_drive, drive_class,  # noqa: F401
                   drive_models, motor_for_hp, param_items, register)
from . import ach580  # noqa: F401  (registers "ach580")
