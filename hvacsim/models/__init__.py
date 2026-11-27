"""Equipment model registry.  Add new equipment types here."""

from .ahu import Ahu
from .chw_plant import ChwPlant
from .hw_plant import HwPlant
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
    HwPlant.TYPE: HwPlant,
}

__all__ = ["EquipmentModel", "FaultDef", "SignalDef", "MODELS"]
