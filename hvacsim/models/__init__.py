"""Equipment model registry.  Add new equipment types here."""

from .base import EquipmentModel, FaultDef, SignalDef
from .unit_heater import HotWaterUnitHeater
from .vfd_fan import VfdFan

MODELS = {
    HotWaterUnitHeater.TYPE: HotWaterUnitHeater,
    VfdFan.TYPE: VfdFan,
}

__all__ = ["EquipmentModel", "FaultDef", "SignalDef", "MODELS"]
