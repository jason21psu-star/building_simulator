"""Equipment model registry.  Add new equipment types here."""

from .ahu import Ahu
from .chw_plant import ChwPlant
from .base import EquipmentModel, FaultDef, SignalDef
from .unit_heater import HotWaterUnitHeater
from .vav import VavReheat
from .vfd_fan import VfdFan
from .weather_station import WeatherStation

MODELS = {
    HotWaterUnitHeater.TYPE: HotWaterUnitHeater,
    VfdFan.TYPE: VfdFan,
    WeatherStation.TYPE: WeatherStation,
    Ahu.TYPE: Ahu,
    VavReheat.TYPE: VavReheat,
    ChwPlant.TYPE: ChwPlant,
}

__all__ = ["EquipmentModel", "FaultDef", "SignalDef", "MODELS"]
