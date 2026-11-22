"""
Weather station: outdoor air temperature and humidity for the whole host.

It's its own BACnet device that the ALC system reads (Mode B). The *true*
outdoor conditions are also shared with every other simulator on the host
(unit heaters, the AHU, later the chiller and boiler), so the physics
everywhere sees the same weather while the trainee's controllers see what the
station's sensors report, faults included.

Weather is fixed by default: the instructor sets OA temperature and RH on the
web page. An optional ramp rate makes changes happen gradually.
"""

from __future__ import annotations

from .. import psychro
from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef


class WeatherStation(EquipmentModel):
    TYPE = "weather_station"
    DESCRIPTION = "Weather station (OA temperature / humidity)"
    MAX_SUBSTEP = 1.0

    DEFAULTS = {
        "oat": 50.0,                 # °F, outdoor dry bulb (target)
        "oa_rh": 50.0,               # %, outdoor relative humidity (target)
        "elevation_ft": 0.0,         # site elevation (barometric pressure for psychrometrics)
        "ramp_rate": 0.0,            # °F and %RH per simulated minute toward the targets, 0 = immediate
        "temp_noise": 0.05,          # °F sensor noise
        "rh_noise": 0.3,             # %RH sensor noise
    }

    PARAM_INFO = {
        "oat": ParamInfo("", "OA temp", "Conditions", "°F", minimum=-60, maximum=130),
        "oa_rh": ParamInfo("", "OA RH", "Conditions", "%", minimum=0, maximum=100),
        "elevation_ft": ParamInfo("", "Elevation", "Conditions", "ft", minimum=-1000, maximum=15000),
        "ramp_rate": ParamInfo("", "Change rate (0 = step)", "Conditions", "/min", minimum=0, maximum=100),
    }
    CONDITIONS = ["oat", "oa_rh", "ramp_rate", "elevation_ft"]

    INPUTS = []

    SIGNALS = [
        # what the station's sensors report (faults applied) - this is what controllers see
        SignalDef("oa_temp", "analog", "degreesFahrenheit", "OA temperature (sensor)"),
        SignalDef("oa_rh", "analog", "percentRelativeHumidity", "OA relative humidity (sensor)"),
        SignalDef("oa_dewpoint", "analog", "degreesFahrenheit", "OA dew point (from sensors)"),
        SignalDef("oa_enthalpy", "analog", "btusPerPoundDryAir", "OA enthalpy (from sensors)"),
        SignalDef("oa_wetbulb", "analog", "degreesFahrenheit", "OA wet bulb (from sensors)"),
        SignalDef("oa_grains", "analog", "grainsOfWaterPerPoundDryAir", "OA humidity ratio (from sensors)"),
        SignalDef("baro_psia", "analog", "poundsForcePerSquareInch", "Barometric pressure"),
        # true conditions - what other simulators' physics use
        SignalDef("oa_temp_actual", "analog", "degreesFahrenheit", "True OA temperature"),
        SignalDef("oa_rh_actual", "analog", "percentRelativeHumidity", "True OA relative humidity"),
        SignalDef("oa_w_actual", "analog", "noUnits", "True OA humidity ratio, lb/lb"),
        SignalDef("oa_enthalpy_actual", "analog", "btusPerPoundDryAir", "True OA enthalpy"),
    ]

    FAULTS = [
        FaultDef("temp_sensor_offset", "Temperature sensor out of calibration", "Temperature sensor",
                 "number", 3.0, "°F", "Offset added to the temperature reading"),
        FaultDef("solar_error", "Radiation shield damaged (sun on sensor)", "Temperature sensor",
                 "number", 8.0, "°F", "Reading error in full sun"),
        FaultDef("temp_sensor_failed", "Temperature sensor failed", "Temperature sensor",
                 "number", -40.0, "°F", "Value the station reports"),
        FaultDef("rh_sensor_drift", "Humidity sensor drifted", "Humidity sensor",
                 "number", 10.0, "%RH", "Offset added to the RH reading"),
        FaultDef("rh_sensor_failed", "Humidity sensor failed", "Humidity sensor",
                 "number", 0.0, "%RH", "Value the station reports"),
        FaultDef("device_offline", "Station offline (no BACnet response)", "Communication"),
    ]

    def reset(self) -> None:
        self.t = float(self.p["oat"])
        self.rh = float(self.p["oa_rh"])

    def _step(self, h: float) -> None:
        rate = float(self.p["ramp_rate"])
        if rate <= 0:
            self.t, self.rh = float(self.p["oat"]), float(self.p["oa_rh"])
            return
        step = rate * h / 60.0
        for attr, target in (("t", float(self.p["oat"])), ("rh", float(self.p["oa_rh"]))):
            cur = getattr(self, attr)
            setattr(self, attr, min(target, cur + step) if target > cur else max(target, cur - step))

    def _publish(self) -> None:
        p = psychro.p_atm(self.p["elevation_ft"])
        true = psychro.state(self.t, self.rh, p)

        if self.fault("temp_sensor_failed"):
            t_read = self.fault_value("temp_sensor_failed")
        else:
            t_read = self.t + self.rng.gauss(0.0, self.p["temp_noise"])
            if self.fault("temp_sensor_offset"):
                t_read += self.fault_value("temp_sensor_offset")
            if self.fault("solar_error"):
                t_read += self.fault_value("solar_error")
        if self.fault("rh_sensor_failed"):
            rh_read = self.fault_value("rh_sensor_failed")
        else:
            rh_read = self.rh
            if self.fault("solar_error"):
                # the RH element shares the sun-heated shield: same moisture, warmer air -> lower RH
                rh_read = psychro.rh_from_w(self.t + self.fault_value("solar_error"), true["w"], p)
            rh_read += self.rng.gauss(0.0, self.p["rh_noise"])
            if self.fault("rh_sensor_drift"):
                rh_read += self.fault_value("rh_sensor_drift")
        rh_read = max(0.0, min(100.0, rh_read))
        # derived values are calculated by the station from its own (possibly wrong) sensors
        meas = psychro.state(max(-60.0, min(130.0, t_read)), max(0.5, rh_read), p)

        s = self.signals
        s["oa_temp"] = round(t_read, 2)
        s["oa_rh"] = round(rh_read, 1)
        s["oa_dewpoint"] = round(meas["dewpoint"], 1)
        s["oa_enthalpy"] = round(meas["h"], 2)
        s["oa_wetbulb"] = round(meas["wetbulb"], 1)
        s["oa_grains"] = round(meas["grains"], 1)
        s["baro_psia"] = round(p, 3)
        s["oa_temp_actual"] = round(self.t, 2)
        s["oa_rh_actual"] = round(self.rh, 1)
        s["oa_w_actual"] = round(true["w"], 5)
        s["oa_enthalpy_actual"] = round(true["h"], 2)
