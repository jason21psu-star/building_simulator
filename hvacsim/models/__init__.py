"""Equipment model registry.  Add new equipment types here."""

from .base import EquipmentModel, FaultDef, SignalDef
from .unit_heater import HotWaterUnitHeater

MODELS = {
    HotWaterUnitHeater.TYPE: HotWaterUnitHeater,
}

__all__ = ["EquipmentModel", "FaultDef", "SignalDef", "MODELS"]
