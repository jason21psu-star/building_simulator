"""Equipment model registry.  Add new equipment types here."""

from .ahu import Ahu
from .base import EquipmentModel, FaultDef, SignalDef
from .unit_heater import HotWaterUnitHeater
from .vfd_fan import VfdFan
from .weather_station import WeatherStation

MODELS = {
    HotWaterUnitHeater.TYPE: HotWaterUnitHeater,
    VfdFan.TYPE: VfdFan,
    WeatherStation.TYPE: WeatherStation,
    Ahu.TYPE: Ahu,
}

__all__ = ["EquipmentModel", "FaultDef", "SignalDef", "MODELS"]
