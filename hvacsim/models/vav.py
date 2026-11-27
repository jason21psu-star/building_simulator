"""
Single-duct VAV box with hot water reheat and no fan (ALC vav_rh), with its zone.

  AHU duct ─► flow cross ─► damper (integrated actuator, 154 s) ─► HW reheat (2-way, 2-10 V, NC)
           ─► DAT ─► diffusers ─► zone   (zone sensor: temp, setpoint adjust, override button, RH)

When the box is linked to an AHU (`served_by` in the host config) the AHU
solves its duct static with this box's damper in it, then hands the box its
inlet static, airflow and supply air conditions; the zone's air returns to
the AHU.  Standalone, the box uses its own inlet static / supply air params.

Points (the same names in every VAV controller):
  written to the ALC controller: zone_temp_1, zone_humid_1, da_temp_1, airflow_1 (cfm),
                                 stpt_adj_1 (°F offset), override_1 (momentary button)
  written by the ALC controller: dmpr_1 (damper position %), rh_vlv_1 (% of 10 V, 2-10 V)
"""

from __future__ import annotations

import datetime as _dt
import math
import time
from typing import Any, Optional

from .. import psychro
from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef
from .components.airside import AIR_LB_PER_CFM_H, SENSIBLE, Actuator, WaterCoil, equal_percentage, signal_2_10

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ZONE_TYPES = ["interior", "north", "south", "east", "west", "conference"]
SOLAR_PEAK_HOUR = {"east": 9.0, "south": 12.5, "west": 16.0}
OVERRIDE_STEP_MIN = 30.0
OVERRIDE_MAX_MIN = 120.0
BUTTON_PULSE_S = 3.0          # real seconds the override contact stays closed


class VavReheat(EquipmentModel):
    TYPE = "vav_rh"
    DESCRIPTION = "Single-duct VAV, hot water reheat, no fan"
    MAX_SUBSTEP = 0.5

    DEFAULTS = {
        "box_max_cfm": 1200.0,         # box airflow at design (sizes the damper and the zone loads)
        "zone_type": "interior",
        "area_sqft": 1000.0,
        "people": 7.0,
        "reheat_gpm": 2.0,
        "duct_position": 0.6,          # 0 = at the AHU, 0.75 = at the static sensor, 1 = end of the duct
        "stroke_s": 154.0,             # damper actuator, 0-100 %
        "stpt_adj_range": 3.0,         # ± °F the occupant's setpoint adjust can move (1-5)
        # standalone conditions (ignored when served by an AHU)
        "inlet_static": 1.5,
        "sa_temp": 55.0,
        "sa_rh": 85.0,
        "hw_supply_temp": 140.0,
        "hw_dp_pct": 100.0,             # HW branch DP, % of design (set by a hw_plant)
        "oat": 50.0,                   # follows the weather station
        "occ_days": "mon-fri",
        "occ_start": 6.0,
        "occ_end": 18.0,
        "clock_start": "",
        "initial_zone_temp": 72.0,
        "sensor_noise": 0.05,
    }
    PARAM_INFO = {
        "stpt_adj_range": ParamInfo("", "Setpoint adjust range (±)", "Zone sensor", "°F", minimum=1, maximum=5),
        "zone_type": ParamInfo("", "Zone type", "Zone", choices=ZONE_TYPES),
        "inlet_static": ParamInfo("", "Inlet static (standalone)", "Conditions", "in. w.c.", minimum=0, maximum=5),
        "sa_temp": ParamInfo("", "Supply air temp (standalone)", "Conditions", "°F", minimum=35, maximum=120),
        "oat": ParamInfo("", "OA temp", "Conditions", "°F", minimum=-40, maximum=130),
    }
    CONDITIONS = ["oat", "inlet_static", "sa_temp"]
    WEATHER_PARAMS = {"oat": "oa_temp_actual"}
    ACTIONS = ["stpt_adj", "override_push"]

    INPUTS = [
        SignalDef("dmpr_1", "analog", "percent", "Damper position command, %", 0.0),
        SignalDef("rh_vlv_1", "analog", "percent", "Reheat valve signal, % of 10 V (2-10 V)", 0.0),
    ]
    SIGNALS = [
        # written into the ALC controller
        SignalDef("zone_temp_1", "analog", "degreesFahrenheit", "Zone temperature"),
        SignalDef("zone_humid_1", "analog", "percentRelativeHumidity", "Zone humidity"),
        SignalDef("da_temp_1", "analog", "degreesFahrenheit", "Discharge air temperature"),
        SignalDef("airflow_1", "analog", "cubicFeetPerMinute", "Airflow (flow cross)"),
        SignalDef("stpt_adj_1", "analog", "degreesFahrenheit", "Setpoint adjust offset"),
        SignalDef("override_1", "binary", description="Override button (momentary)", default=False),
        # true values
        SignalDef("airflow_actual", "analog", "cubicFeetPerMinute", "True airflow"),
        SignalDef("damper_pos", "analog", "percent", "Damper blade position"),
        SignalDef("valve_pos", "analog", "percent", "Reheat valve position"),
        SignalDef("inlet_static", "analog", "inchesOfWater", "Inlet static pressure"),
        SignalDef("supply_temp_in", "analog", "degreesFahrenheit", "Supply air entering the box"),
        SignalDef("da_temp_actual", "analog", "degreesFahrenheit", "True discharge air temp"),
        SignalDef("reheat_mbh", "analog", "noUnits", "Reheat output, MBH"),
        SignalDef("reheat_gpm", "analog", "usGallonsPerMinute", "Reheat water flow"),
        SignalDef("zone_temp_actual", "analog", "degreesFahrenheit", "True zone temperature"),
        SignalDef("zone_rh_actual", "analog", "percentRelativeHumidity", "True zone RH"),
        SignalDef("zone_co2", "analog", "partsPerMillion", "Zone CO2"),
        SignalDef("zone_load", "analog", "btusPerHour", "Zone sensible load (+ = heating the room)"),
        SignalDef("occupants", "analog", "noUnits", "People in the zone"),
        SignalDef("override_remaining", "analog", "minutes", "Override time remaining (button timer)"),
        SignalDef("oat", "analog", "degreesFahrenheit", "OA temp in use"),
        SignalDef("occupied", "binary", description="Scheduled occupancy or override", default=False),
        SignalDef("sim_clock", "text", description="Simulated date/time", default=""),
    ]

    FAULTS = [
        FaultDef("damper_stuck", "Damper stuck", "Damper", "number", 30.0, "%", "Stuck position"),
        FaultDef("actuator_failed", "Damper actuator failed (doesn't move)", "Damper"),
        FaultDef("linkage_slipped", "Damper linkage slipped (half travel)", "Damper"),
        FaultDef("flow_tube_pinched", "Flow sensor tube pinched (reads low)", "Airflow sensor"),
        FaultDef("flow_tubes_reversed", "Flow sensor tubes reversed (reads 0)", "Airflow sensor"),
        FaultDef("flow_probe_dirty", "Flow probe dirty", "Airflow sensor", "number", -20.0, "%", "Reading error"),
        FaultDef("valve_stuck", "Reheat valve stuck", "Reheat", "number", 0.0, "%", "Stuck position"),
        FaultDef("valve_actuator_failed", "Reheat valve actuator failed (springs closed)", "Reheat"),
        FaultDef("valve_leaking", "Reheat valve leaking through", "Reheat", "number", 15.0, "%", "Leak flow"),
        FaultDef("coil_air_bound", "Reheat coil air-bound (no heat)", "Reheat"),
        FaultDef("high_load", "Extra zone load (door open, equipment)", "Zone", "number", 8000.0, "Btu/h",
                 "+ heats the room, - cools it"),
        FaultDef("sensor_in_sun", "Zone sensor in direct sun", "Zone sensor", "number", 4.0, "°F"),
        FaultDef("zone_sensor_offset", "Zone sensor out of calibration", "Zone sensor", "number", 2.0, "°F"),
        FaultDef("zone_sensor_failed", "Zone sensor failed", "Zone sensor", "number", -40.0, "°F"),
        FaultDef("humidity_sensor_drift", "Zone humidity sensor drifted", "Zone sensor", "number", 10.0, "%RH"),
        FaultDef("dat_sensor_offset", "DAT sensor out of calibration", "Discharge sensor", "number", 5.0, "°F"),
        FaultDef("device_offline", "VAV controller link offline (no BACnet response)", "Communication"),
    ]

    # ------------------------------------------------------------------

    def __init__(self, params=None, seed=None):
        self.supply = None          # the AHU model serving this box (set by the host)
        super().__init__(params, seed)

    def reset(self) -> None:
        p = self.p
        self.damper = Actuator(stroke_s=p["stroke_s"], spring_pos=0.0)
        self.valve = Actuator(stroke_s=90.0, spring_pos=0.0)
        self.coil = WaterCoil(cfm_d=0.4 * p["box_max_cfm"], gpm_d=p["reheat_gpm"], ewt_d=140.0,
                              eat_d=55.0, lat_d=95.0, capacitance=6.0)     # small coil: < 1 gal of water
        self.coil.lwt = p["initial_zone_temp"]
        pa = psychro.p_atm(0)
        self.tz = p["initial_zone_temp"]
        self.wz = psychro.w_from_rh(self.tz, 45.0, pa)
        self.co2 = 450.0
        self.q = 0.0                 # cfm through the box (set by the AHU when linked)
        self.p_in = 0.0
        self.t_dat = self.tz
        self.t_in = self.tz
        self.override_min = 0.0
        self._button_until = 0.0
        self.stpt_adj = 0.0
        self._occ = False
        self._load = 0.0
        self._people = 0.0
        start = p["clock_start"]
        self.clock0 = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M") if start else _dt.datetime.now().replace(microsecond=0)
        # box resistance: fully open the box passes 1.6 x design at 1.0 in. inlet;
        # 0.25 in. at design flow downstream (reheat coil, duct, diffusers)
        mx = p["box_max_cfm"]
        self.k_down = 0.25 / mx ** 2
        k_total_open = 1.0 / (1.6 * mx) ** 2
        self.c_open = 1.0 / math.sqrt(max(k_total_open - self.k_down, 1e-12))

    # ----- used by the serving AHU ----------------------------------------

    @property
    def blade(self) -> float:
        pos = self.damper.pos
        return pos * 0.5 if self.fault("linkage_slipped") else pos

    def flow_at(self, p_in: float) -> float:
        """Box airflow (cfm) at an inlet static (in. w.c.) with the current damper position."""
        if p_in <= 0:
            return 0.0
        b = self.blade
        frac = 0.01 + 0.99 * b ** 2.2              # single-blade round damper, 1 % leakage
        c = self.c_open * frac
        return math.sqrt(p_in / (self.k_down + 1.0 / (c * c)))

    def inlet_factor(self) -> float:
        """Inlet static relative to the duct static sensor, by position along the duct."""
        x = self.p["duct_position"]
        return 1.0 - 0.3 * (x - 0.75) / 0.25 if x > 0.75 else 1.0

    def clock(self) -> _dt.datetime:
        if self.supply is not None:
            return self.supply.clock()
        return self.clock0 + _dt.timedelta(seconds=self.sim_time)

    # ----- occupant actions ---------------------------------------------------

    def action(self, name: str, value: Any = None) -> None:
        if name == "stpt_adj":
            rng = float(self.p["stpt_adj_range"])
            self.stpt_adj = max(-rng, min(rng, float(value)))
        elif name == "override_push":
            self.override_min = min(OVERRIDE_MAX_MIN, self.override_min + OVERRIDE_STEP_MIN)
            self._button_until = time.monotonic() + BUTTON_PULSE_S
        else:
            raise KeyError(name)
        self._publish()

    # ----- schedule / loads ---------------------------------------------------

    def _scheduled(self, now: _dt.datetime) -> bool:
        days = str(self.p["occ_days"]).lower()
        if self.supply is not None:
            days = str(self.supply.p["occ_days"]).lower()
            start, end = self.supply.p["occ_start"], self.supply.p["occ_end"]
        else:
            start, end = self.p["occ_start"], self.p["occ_end"]
        if "-" in days:
            a, b = days.split("-")
            allowed = WEEKDAYS[WEEKDAYS.index(a):WEEKDAYS.index(b) + 1]
        else:
            allowed = [d.strip() for d in days.split(",")]
        hour = now.hour + now.minute / 60.0
        return WEEKDAYS[now.weekday()] in allowed and start <= hour < end

    def _people_now(self, now: _dt.datetime, scheduled: bool) -> float:
        p = self.p
        if self.p["zone_type"] == "conference":
            hour = now.hour + now.minute / 60.0
            meeting = scheduled and (9.0 <= hour < 11.0 or 13.0 <= hour < 15.5)
            if meeting:
                return p["people"]
            return 2.0 if self.override_min > 0 else 0.0
        if scheduled:
            return p["people"]
        return 1.0 if self.override_min > 0 else 0.0

    def _solar(self, now: _dt.datetime) -> float:
        zt = self.p["zone_type"]
        area = self.p["area_sqft"]
        hour = now.hour + now.minute / 60.0
        if zt not in ("north", "south", "east", "west") or not 6.0 <= hour <= 19.0:
            return 0.0
        if zt == "north":
            return 3.0 * area * math.sin((hour - 6.0) / 13.0 * math.pi)
        peak = SOLAR_PEAK_HOUR[zt]
        x = (hour - peak) / 4.0
        return 12.0 * area * max(0.0, math.cos(x * math.pi / 2.0)) if abs(x) < 1.0 else 0.0

    # ----- step -----------------------------------------------------------------

    def _step(self, h: float) -> None:
        p = self.p
        pa = psychro.p_atm(0)
        now = self.clock()
        if self.override_min > 0:
            self.override_min = max(0.0, self.override_min - h / 60.0)
        scheduled = self._scheduled(now)
        self._occ = scheduled or self.override_min > 0
        self._people = self._people_now(now, scheduled)

        # damper: integrated actuator (no spring return); fails in place
        cmd = max(0.0, min(100.0, float(self.inputs["dmpr_1"]))) / 100.0
        stuck = self.fault_value("damper_stuck") / 100.0 if self.fault("damper_stuck") else None
        if self.fault("actuator_failed"):
            stuck = self.damper.pos
        self.damper.stroke_s = p["stroke_s"]
        self.damper.step(h, cmd, True, stuck)

        # reheat valve: 2-10 V, spring closed
        vstuck = self.fault_value("valve_stuck") / 100.0 if self.fault("valve_stuck") else None
        vpos = self.valve.step(h, signal_2_10(float(self.inputs["rh_vlv_1"])), not self.fault("valve_actuator_failed"), vstuck)

        # supply air and inlet static
        ahu = self.supply
        if ahu is not None:
            self.p_in = getattr(self, "_p_in_from_ahu", 0.0)
            self.q = getattr(self, "_q_from_ahu", 0.0)
            t_sa, w_sa = ahu.t_sa, ahu.w_sa
            co2_sa = getattr(ahu, "co2_sa", 450.0)
            hw_src = p if getattr(self, "hw_plant", None) is not None else ahu.p
            hw_t = hw_src["hw_supply_temp"]
            hw_ok = not ahu.fault("hw_plant_off")
        else:
            self.p_in = p["inlet_static"]
            self.q = self.flow_at(self.p_in)
            t_sa = p["sa_temp"]
            w_sa = psychro.w_from_rh(t_sa, p["sa_rh"], pa)
            co2_sa = 450.0
            hw_t = p["hw_supply_temp"]
            hw_ok = True
        q = self.q
        self.t_in = t_sa if q > 5 else self.t_in + (self.tz - self.t_in) * min(1.0, h / 300.0)

        # reheat coil
        leak = self.fault_value("valve_leaking") / 100.0 if self.fault("valve_leaking") else 0.0
        frac = max(equal_percentage(vpos), leak)
        gpm = p["reheat_gpm"] * frac * math.sqrt(max(0.0, p["hw_dp_pct"]) / 100.0) if hw_ok else 0.0
        if self.fault("coil_air_bound"):
            gpm *= 0.01                     # air in the coil: almost no circulation
        t_dat, w_dat = self.coil.step(h, max(q, 0.0), self.t_in, w_sa, hw_t, gpm, pa)
        if q < 5:
            t_dat = self.t_dat + (t_dat - self.t_dat) * min(1.0, h / 120.0)
        self.t_dat = t_dat

        # zone heat balance
        area = p["area_sqft"]
        occ = self._occ
        lights = area * (1.0 if occ else 0.2) * 3.412
        equip = area * (1.5 if occ else 0.5) * 3.412
        people_s = 250.0 * self._people
        env_ua = 0.12 * area if p["zone_type"] in ("north", "south", "east", "west") else 0.0
        env = env_ua * (p["oat"] - self.tz)
        solar = self._solar(now)
        extra = self.fault_value("high_load") if self.fault("high_load") else 0.0
        supply = SENSIBLE * q * (t_dat - self.tz)
        self._load = lights + equip + people_s + env + solar + extra
        C_z = 2.0 * area                                         # Btu/°F (air + furnishings)
        self.tz += (self._load + supply) / C_z * h / 3600.0

        # moisture and CO2
        m_air = area * 9.0 * 0.075
        latent_lb_h = 0.19 * self._people
        self.wz += (AIR_LB_PER_CFM_H * q * (w_dat - self.wz) + latent_lb_h) / m_air * h / 3600.0
        self.wz = max(0.0005, min(self.wz, psychro.w_sat(self.tz, pa)))
        vol = area * 9.0
        self.co2 += (q * (co2_sa - self.co2) + self._people * 0.0105 * 1e6
                     + 0.1 * vol / 60.0 * (420.0 - self.co2)) / vol * h / 60.0

    # ----- publish --------------------------------------------------------------

    def _publish(self) -> None:
        if not hasattr(self, "damper"):
            return
        p, s, pa = self.p, self.signals, psychro.p_atm(0)
        n = p["sensor_noise"]
        rh = psychro.rh_from_w(self.tz, self.wz, pa)
        now = self.clock()
        # zone sensor
        if self.fault("zone_sensor_failed"):
            zt = self.fault_value("zone_sensor_failed")
        else:
            zt = self.tz + self.rng.gauss(0, n)
            if self.fault("zone_sensor_offset"):
                zt += self.fault_value("zone_sensor_offset")
            if self.fault("sensor_in_sun") and self._solar(now) > 0:
                zt += self.fault_value("sensor_in_sun")
        s["zone_temp_1"] = round(zt, 2)
        s["zone_humid_1"] = round(min(100.0, rh + self.rng.gauss(0, 0.3)
                                      + (self.fault_value("humidity_sensor_drift") if self.fault("humidity_sensor_drift") else 0.0)), 1)
        s["da_temp_1"] = round(self.t_dat + self.rng.gauss(0, n)
                               + (self.fault_value("dat_sensor_offset") if self.fault("dat_sensor_offset") else 0.0), 2)
        flow = self.q
        if self.fault("flow_tubes_reversed"):
            meas = 0.0
        else:
            meas = flow * (1.0 + self.rng.gauss(0, 0.01))
            if self.fault("flow_tube_pinched"):
                meas *= 0.3
            if self.fault("flow_probe_dirty"):
                meas *= 1.0 + self.fault_value("flow_probe_dirty") / 100.0
        s["airflow_1"] = round(max(0.0, meas), 0)
        s["stpt_adj_1"] = round(self.stpt_adj, 1)
        s["override_1"] = time.monotonic() < self._button_until
        # true values
        s["airflow_actual"] = round(flow, 0)
        s["damper_pos"] = round(100.0 * self.blade, 1)
        s["valve_pos"] = round(100.0 * self.valve.pos, 1)
        s["inlet_static"] = round(self.p_in, 3)
        s["supply_temp_in"] = round(self.t_in, 2)
        s["da_temp_actual"] = round(self.t_dat, 2)
        s["reheat_mbh"] = round(max(0.0, self.coil.q) / 1000.0, 2)
        s["reheat_gpm"] = round(self.coil.flow_gpm, 2)
        s["zone_temp_actual"] = round(self.tz, 2)
        s["zone_rh_actual"] = round(rh, 1)
        s["zone_co2"] = round(self.co2, 0)
        s["zone_load"] = round(self._load, 0)
        s["occupants"] = round(self._people, 0)
        s["override_remaining"] = round(self.override_min, 1)
        s["oat"] = round(p["oat"], 1)
        s["occupied"] = self._occ
        s["sim_clock"] = now.strftime("%a %Y-%m-%d %H:%M")
