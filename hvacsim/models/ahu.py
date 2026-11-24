"""
VAV air handler with economizer, 4-fan supply array and return fan
(ALC vav_ahu_econ, see docs/AHU_DESIGN.md).

Air path:
  OA dmpr(NC) ─┬─ prefilter ─ preheat(HW, NO) ─ MAT/freezestat ─ SF-1..4 (barometric dampers)
               │   ─ CHW(NC) ─ HW(NC) ─ steam humidifier ─ SA RH/SAT/smoke ─ high-static ─ duct ─ VAV ─ zones
  RA dmpr(NO) ─┘
  EA dmpr(NC) ─── return plenum ─ RF ─ RA smoke/CO2/RH/RAT ─ zones

Every hardware point the ALC controller sees is a model input (commands) or
signal (sensor values written into the controller).  The five drives are
ACH580-style Vfd components wired hardwired: BO -> DI1, AO -> AI1 (0-10 V),
safety chain -> DI4 (20.41 start interlock 1), RO3 -> fault BI, AO1 ->
feedback AI, CT on the motor leads -> amps AI.

Contact convention for safety BIs written to the controller: True = contact
closed = NORMAL (fail-safe: an open circuit reads as an alarm).  Drive fault
BIs follow RO3 "Fault (-1)": True = closed = healthy.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, List, Tuple

from .. import psychro
from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef, coerce
from .components.airside import (
    AIR_LB_PER_CFM_H, SENSIBLE, Actuator, SteamHumidifier, WaterCoil, damper_fraction,
    equal_percentage, signal_2_10, ssq, ssqrt,
)
from .components.vfd import FAULT_TEXT, VFD_DEFAULTS, VFD_PARAM_INFO, Vfd

SF = ["sf1", "sf2", "sf3", "sf4"]
DRIVES = SF + ["rf"]
DRIVE_LABEL = {"sf1": "SF-1", "sf2": "SF-2", "sf3": "SF-3", "sf4": "SF-4", "rf": "RF"}
DRIVE_FAULTS = {
    "overcurrent_trip": "Drive overcurrent trip",
    "cooling_fan_failed": "Drive cooling fan failed",
    "coupling_failed": "Motor-fan coupling failed",
    "bearing_worn": "Worn fan bearings",
}

# hardwired ACH580 settings for every AHU drive (on top of the manual defaults)
DRIVE_BASE = {
    "ext1_commands": "in1_start",
    "ext1_freq_ref1": "ai1_scaled",
    "start_interlock_1": "di4",
    "start_interlock_stop_mode": "coast",
    "ro1_source": "ready_run",
    "min_freq": 12.0,
    "accel_time_1": 30.0,
    "decel_time_1": 30.0,
}
SF_DRIVE = {"motor_nom_current": 14.0, "motor_nom_power": 7.5, "max_current": 19.6, "inertia_h": 5.0}
RF_DRIVE = {"motor_nom_current": 21.0, "motor_nom_power": 11.2, "max_current": 29.4, "inertia_h": 6.0}

_A = "analog"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Ahu(EquipmentModel):
    TYPE = "ahu_vav_econ"
    DESCRIPTION = "VAV AHU, economizer, 4-fan supply array, return fan"
    MAX_SUBSTEP = 0.25

    DEFAULTS: Dict[str, Any] = {
        # plant (until boiler/chiller simulators exist)
        "hw_supply_temp": 140.0,
        "chw_supply_temp": 44.0,
        "loop_dp_pct": 100.0,          # loop differential pressure, % of design
        # weather (follows the host weather station when there is one)
        "oat": 50.0,
        "oa_w": 0.0038,
        # design
        "design_cfm": 24000.0,
        "fan_design_cfm": 8000.0,      # each supply fan at 4.5 in. w.c., full speed
        "rf_design_cfm": 22000.0,
        "vav_min_static": 0.75,        # in. w.c. VAV inlet static needed at full design flow
        "high_static_setpoint": 4.0,   # in. w.c. at fan discharge (manual reset switch)
        "freezestat_setpoint": 38.0,   # °F (manual reset)
        "hum_high_limit": 75.0,        # %RH supply (auto reset, 5 % differential)
        "hum_capacity": 250.0,         # lb/h steam
        "stratification": 0.25,        # fraction of OA-to-mixed difference reaching the coldest spot
        "filter_loading": 0.2,         # 0 clean .. 1 change filter
        # building / zones (aggregate until the VAV simulators exist)
        "occ_days": "mon-fri",
        "occ_start": 6.0,
        "occ_end": 18.0,
        "occ_demand": 70.0,            # % of design airflow, occupied (plus midday peak)
        "occ_peak": 15.0,              # extra % at 14:00
        "unocc_demand": 25.0,
        "demand_override": -1.0,       # % of design; -1 = follow the schedule
        "load_scale": 100.0,           # % of design building loads
        "people": 120.0,
        "zone_heat_sp": 70.0,
        "zone_heat_sp_unocc": 60.0,
        "initial_zone_temp": 72.0,
        "clock_start": "",             # "YYYY-mm-dd HH:MM" sim clock start; blank = host local time
        "sensor_noise": 0.05,
        # drive parameter overrides (dicts of ACH580 parameters)
        "sf_drive": {},
        "rf_drive": {},
    }

    PARAM_INFO = {
        "hw_supply_temp": ParamInfo("", "HW supply", "Conditions", "°F", minimum=40, maximum=250),
        "chw_supply_temp": ParamInfo("", "CHW supply", "Conditions", "°F", minimum=30, maximum=80),
        "loop_dp_pct": ParamInfo("", "Loop dP", "Conditions", "%", minimum=0, maximum=200),
        "demand_override": ParamInfo("", "VAV demand (-1 = schedule)", "Conditions", "%", minimum=-1, maximum=110),
        "load_scale": ParamInfo("", "Building load", "Conditions", "%", minimum=0, maximum=200),
        "filter_loading": ParamInfo("", "Filter loading (0-1)", "Conditions", "", minimum=0, maximum=1),
        "oat": ParamInfo("", "OA temp", "Conditions", "°F", minimum=-40, maximum=130),
    }
    CONDITIONS = ["oat", "hw_supply_temp", "chw_supply_temp", "loop_dp_pct", "demand_override",
                  "load_scale", "filter_loading"]
    WEATHER_PARAMS = {"oat": "oa_temp_actual", "oa_w": "oa_w_actual"}
    ACTIONS = ["frz_reset", "hs_reset", "coil_repair"] + [f"{d}.{k}" for d in DRIVES
                                                         for k in ("hand", "off", "auto", "reset", "hand_ref")]

    _AO = ["ma_dmpr", "ph_vlv", "clg_vlv", "htg_vlv", "hum_vlv", "rf_vfd_spd"] + [f"{s}_vfd_spd" for s in SF]
    _BO = ["hum_enable", "rf_ss"] + [f"{s}_ss" for s in SF]
    INPUTS = ([SignalDef(n, "analog", "percent", f"{n} signal, % of 10 V", 0.0) for n in _AO]
              + [SignalDef(n, "binary", description=f"{n} relay", default=False) for n in _BO])

    SIGNALS = [
        # --- sensors written into the ALC controller ---
        SignalDef("ma_temp", _A, "degreesFahrenheit", "Mixed air temp (averaging, after preheat)"),
        SignalDef("pf_dp", _A, "inchesOfWater", "Prefilter differential pressure"),
        SignalDef("ra_co2", _A, "partsPerMillion", "Return air CO2"),
        SignalDef("ra_rh", _A, "percentRelativeHumidity", "Return air humidity"),
        SignalDef("ra_temp", _A, "degreesFahrenheit", "Return air temp"),
        SignalDef("sa_rh", _A, "percentRelativeHumidity", "Supply air humidity"),
        SignalDef("sa_static", _A, "inchesOfWater", "Supply duct static pressure"),
        SignalDef("sa_temp", _A, "degreesFahrenheit", "Supply air temp"),
        *[SignalDef(f"{d}_amps", _A, "amperes", f"{DRIVE_LABEL[d]} CT amps (0-30 A)") for d in DRIVES],
        *[SignalDef(f"{d}_vfd_fb", _A, "percent", f"{DRIVE_LABEL[d]} VFD feedback (AO1, % of 10 V)") for d in DRIVES],
        SignalDef("frz_stat", "binary", description="Freezestat contact (closed = normal)", default=True),
        SignalDef("hi_static", "binary", description="High static switch contact (closed = normal)", default=True),
        SignalDef("ra_smoke", "binary", description="RA smoke detector contact (closed = normal)", default=True),
        SignalDef("sa_smoke", "binary", description="SA smoke detector contact (closed = normal)", default=True),
        *[SignalDef(f"{d}_vfd_flt", "binary", description=f"{DRIVE_LABEL[d]} VFD fault (RO3 closed = healthy)",
                    default=True) for d in DRIVES],
        # --- true values (monitor points / troubleshooting) ---
        SignalDef("supply_cfm", _A, "cubicFeetPerMinute", "Supply airflow"),
        SignalDef("return_cfm", _A, "cubicFeetPerMinute", "Return airflow"),
        SignalDef("oa_cfm", _A, "cubicFeetPerMinute", "Outdoor airflow"),
        SignalDef("ea_cfm", _A, "cubicFeetPerMinute", "Exhaust airflow"),
        SignalDef("oa_fraction", _A, "percent", "Outdoor air fraction"),
        SignalDef("oa_dmpr_pos", _A, "percent", "OA damper position"),
        SignalDef("ra_dmpr_pos", _A, "percent", "RA damper position"),
        SignalDef("ea_dmpr_pos", _A, "percent", "EA damper position"),
        SignalDef("ph_vlv_pos", _A, "percent", "Preheat valve position"),
        SignalDef("clg_vlv_pos", _A, "percent", "Cooling valve position"),
        SignalDef("htg_vlv_pos", _A, "percent", "Heating valve position"),
        SignalDef("hum_vlv_pos", _A, "percent", "Humidifier valve position"),
        SignalDef("mixed_temp", _A, "degreesFahrenheit", "True mixed air temp (before preheat)"),
        SignalDef("ph_lat", _A, "degreesFahrenheit", "Preheat leaving air (true)"),
        SignalDef("coldest_temp", _A, "degreesFahrenheit", "Coldest spot at the freezestat"),
        SignalDef("fan_lat", _A, "degreesFahrenheit", "Fan leaving air (fan heat)"),
        SignalDef("cc_lat", _A, "degreesFahrenheit", "Cooling coil leaving air"),
        SignalDef("hc_lat", _A, "degreesFahrenheit", "Heating coil leaving air"),
        SignalDef("sa_temp_actual", _A, "degreesFahrenheit", "True supply air temp"),
        SignalDef("sa_dewpoint", _A, "degreesFahrenheit", "Supply dew point"),
        SignalDef("ph_lwt", _A, "degreesFahrenheit", "Preheat coil water temp"),
        SignalDef("cc_lwt", _A, "degreesFahrenheit", "Cooling coil leaving water"),
        SignalDef("hc_lwt", _A, "degreesFahrenheit", "Heating coil water temp"),
        SignalDef("ph_gpm", _A, "usGallonsPerMinute", "Preheat flow"),
        SignalDef("cc_gpm", _A, "usGallonsPerMinute", "Cooling coil flow"),
        SignalDef("hc_gpm", _A, "usGallonsPerMinute", "Heating coil flow"),
        SignalDef("cc_tons", _A, "tonsRefrigeration", "Cooling coil load"),
        SignalDef("ph_mbh", _A, "noUnits", "Preheat load, MBH"),
        SignalDef("hc_mbh", _A, "noUnits", "Heating coil load, MBH"),
        SignalDef("steam_lb_h", _A, "poundsMassPerHour", "Steam injected"),
        SignalDef("fan_dp", _A, "inchesOfWater", "Supply fan total pressure"),
        SignalDef("discharge_static", _A, "inchesOfWater", "Fan discharge static (high-static switch)"),
        SignalDef("sa_static_actual", _A, "inchesOfWater", "True duct static at sensor"),
        SignalDef("mix_plenum_p", _A, "inchesOfWater", "Mixing plenum static"),
        SignalDef("return_plenum_p", _A, "inchesOfWater", "Return plenum static"),
        SignalDef("building_p", _A, "inchesOfWater", "Building pressure"),
        SignalDef("pf_dp_actual", _A, "inchesOfWater", "True prefilter dP"),
        SignalDef("vav_demand_cfm", _A, "cubicFeetPerMinute", "Aggregate VAV airflow demand (boxes not simulated)"),
        SignalDef("vav_open_pct", _A, "percent", "Aggregate VAV damper opening"),
        SignalDef("boxes_cfm", _A, "cubicFeetPerMinute", "Airflow through simulated VAV boxes"),
        SignalDef("boxes", _A, "noUnits", "Simulated VAV boxes served"),
        SignalDef("zone_temp", _A, "degreesFahrenheit", "Aggregate zone temp"),
        SignalDef("zone_rh", _A, "percentRelativeHumidity", "Aggregate zone RH"),
        SignalDef("zone_co2", _A, "partsPerMillion", "Aggregate zone CO2"),
        SignalDef("ra_temp_actual", _A, "degreesFahrenheit", "True return air temp"),
        SignalDef("oat", _A, "degreesFahrenheit", "OA temp in use"),
        *[SignalDef(f"{d}_freq", _A, "hertz", f"{DRIVE_LABEL[d]} output frequency") for d in DRIVES],
        *[SignalDef(f"{s}_cfm", _A, "cubicFeetPerMinute", f"{DRIVE_LABEL[s]} airflow") for s in SF],
        *[SignalDef(f"{s}_bd_pos", _A, "percent", f"{DRIVE_LABEL[s]} barometric damper") for s in SF],
        SignalDef("occupied", "binary", description="Schedule occupied", default=False),
        SignalDef("starved", "binary", description="VAV boxes starved (static too low)", default=False),
        SignalDef("safety_chain_ok", "binary", description="Safety chain to DI4 closed", default=True),
        SignalDef("frz_tripped", "binary", description="Freezestat tripped (latched)", default=False),
        SignalDef("hs_tripped", "binary", description="High static tripped (latched)", default=False),
        SignalDef("hum_hl_tripped", "binary", description="Humidifier high limit tripped", default=False),
        SignalDef("duct_wetting", "binary", description="Steam not absorbed (wet duct)", default=False),
        SignalDef("ph_frozen", "binary", description="Preheat coil frozen / damaged", default=False),
        SignalDef("cc_frozen", "binary", description="Cooling coil frozen / damaged", default=False),
        SignalDef("hc_frozen", "binary", description="Heating coil frozen / damaged", default=False),
        SignalDef("sim_clock", "text", description="Simulated date/time", default=""),
        SignalDef("drives", "text", description="Drive status (json-like)", default=""),
    ]

    FAULTS = [
        # dampers
        FaultDef("oa_actuator_failed", "OA damper actuator failed (springs closed)", "Dampers"),
        FaultDef("ra_actuator_failed", "RA damper actuator failed (springs open)", "Dampers"),
        FaultDef("ea_actuator_failed", "EA damper actuator failed (springs closed)", "Dampers"),
        FaultDef("oa_damper_stuck", "OA damper stuck", "Dampers", "number", 20.0, "%", "Stuck position"),
        FaultDef("oa_linkage_slipped", "OA damper linkage slipped (half travel)", "Dampers"),
        FaultDef("ma_wire_broken", "Mixed air damper signal wire broken", "Dampers"),
        # filter
        FaultDef("filter_blown", "Prefilter blown out", "Filter"),
        # coils
        FaultDef("ph_valve_stuck", "Preheat valve stuck", "Preheat", "number", 0.0, "%", "Stuck position"),
        FaultDef("ph_actuator_failed", "Preheat actuator failed (springs OPEN)", "Preheat"),
        FaultDef("ph_wire_broken", "Preheat signal wire broken", "Preheat"),
        FaultDef("hw_plant_off", "Hot water plant off (no flow)", "Plant"),
        FaultDef("chw_plant_off", "Chilled water plant off (no flow)", "Plant"),
        FaultDef("chw_warm", "Warm chilled water", "Plant", "number", 55.0, "°F", "CHW supply temperature"),
        FaultDef("clg_valve_stuck", "Cooling valve stuck", "Cooling coil", "number", 0.0, "%", "Stuck position"),
        FaultDef("clg_actuator_failed", "Cooling actuator failed (springs closed)", "Cooling coil"),
        FaultDef("htg_valve_stuck", "Heating valve stuck", "Heating coil", "number", 0.0, "%", "Stuck position"),
        FaultDef("htg_actuator_failed", "Heating actuator failed (springs closed)", "Heating coil"),
        # humidifier
        FaultDef("steam_off", "No steam", "Humidifier"),
        FaultDef("hum_valve_stuck_open", "Steam valve stuck open", "Humidifier"),
        FaultDef("hum_hl_failed", "High-limit humidistat failed (never trips)", "Humidifier"),
        FaultDef("hum_hl_drift", "High-limit humidistat drifted", "Humidifier", "number", 15.0, "%RH", "Offset"),
        # safeties / duct
        FaultDef("frz_trip", "Freezestat tripped (forced)", "Safeties"),
        FaultDef("frz_failed", "Freezestat failed (never trips)", "Safeties"),
        FaultDef("hs_failed", "High static switch failed (never trips)", "Safeties"),
        FaultDef("ra_smoke_alarm", "Return air smoke detector in alarm", "Safeties"),
        FaultDef("sa_smoke_alarm", "Supply air smoke detector in alarm", "Safeties"),
        FaultDef("poor_mixing", "Poor mixing (stratification)", "Safeties"),
        FaultDef("fire_damper_closed", "Fire damper closed in the duct", "Duct"),
        FaultDef("duct_leak", "Duct leak upstream of the static sensor", "Duct"),
        # fan array
        *[FaultDef(f"{s}_bd_stuck_open", f"{DRIVE_LABEL[s]} barometric damper stuck open", "Fan array") for s in SF],
        *[FaultDef(f"{s}_bd_stuck_closed", f"{DRIVE_LABEL[s]} barometric damper stuck closed", "Fan array") for s in SF],
        *[FaultDef(f"{d}_{k}", f"{DRIVE_LABEL[d]}: {txt}", f"{DRIVE_LABEL[d]} drive")
          for d in DRIVES for k, txt in DRIVE_FAULTS.items()],
        # sensors
        FaultDef("sat_offset", "SAT sensor offset", "Sensors", "number", 4.0, "°F"),
        FaultDef("mat_offset", "MAT sensor offset", "Sensors", "number", -4.0, "°F"),
        FaultDef("rat_offset", "RAT sensor offset", "Sensors", "number", 3.0, "°F"),
        FaultDef("static_tube_off", "Static pressure tube disconnected (reads 0)", "Sensors"),
        FaultDef("pf_tubes_reversed", "Filter dP tubes reversed", "Sensors"),
        FaultDef("co2_failed", "CO2 sensor failed", "Sensors", "number", 0.0, "ppm"),
        FaultDef("sa_rh_failed", "SA RH sensor failed", "Sensors", "number", 0.0, "%RH"),
        FaultDef("ra_rh_drift", "RA RH sensor drifted", "Sensors", "number", 10.0, "%RH"),
        FaultDef("device_offline", "AHU simulator offline (no BACnet response)", "Communication"),
    ]

    # ------------------------------------------------------------------ setup

    def reset(self) -> None:
        p = self.p
        self.drive_p: Dict[str, Dict[str, Any]] = {}
        self.inject: Dict[str, set] = {}
        self.drives: Dict[str, Vfd] = {}
        for d in DRIVES:
            dp = dict(VFD_DEFAULTS)
            dp.update(DRIVE_BASE)
            dp.update(RF_DRIVE if d == "rf" else SF_DRIVE)
            dp.update(p["rf_drive"] if d == "rf" else p["sf_drive"])
            for k in list(dp):
                if k in VFD_DEFAULTS:
                    dp[k] = coerce(dp[k], VFD_DEFAULTS[k])
            self.drive_p[d] = dp
            self.inject[d] = set()
            self.drives[d] = Vfd(dp, self.inject[d])
        self.act = {
            "oa": Actuator(150.0, 0.0), "ra": Actuator(150.0, 0.0), "ea": Actuator(150.0, 0.0),
            "ph": Actuator(90.0, 1.0),       # preheat valve fails OPEN
            "clg": Actuator(90.0, 0.0), "htg": Actuator(90.0, 0.0), "hum": Actuator(60.0, 0.0),
        }
        D = p["design_cfm"]
        self.ph = WaterCoil(cfm_d=D, gpm_d=26.0, ewt_d=140.0, eat_d=35.0, lat_d=50.0, capacitance=300.0)
        self.cc = WaterCoil(cfm_d=D, gpm_d=180.0, ewt_d=44.0, eat_d=80.0, lat_d=53.0, ew_d=0.0112,
                            capacitance=900.0, cooling=True)
        self.hc = WaterCoil(cfm_d=D * 0.5, gpm_d=26.0, ewt_d=140.0, eat_d=55.0, lat_d=85.0, capacitance=300.0)
        self.hum = SteamHumidifier(p["hum_capacity"])
        for coil in (self.ph, self.cc, self.hc):
            coil.lwt = p["initial_zone_temp"]          # water in the coils starts at room temperature
        # flows / pressures
        self.q_s = 0.0
        self.q_r = 0.0
        self.q_oa = self.q_ra = self.q_ea = 0.0
        self.q_fan = {s: 0.0 for s in SF}
        self.p_fan = 0.0
        self.p_mp = 0.0
        self.p_rp = 0.0
        self.p_b = 0.0
        self.bd_pos = {s: 0.0 for s in SF}
        self.q_vav = 0.0
        self.q_boxes = 0.0
        self.co2_sa = 450.0
        self.p_sp = 0.0
        # VAV aggregate: conductance cfm / sqrt(in. w.c.)
        self.c_open = D / math.sqrt(p["vav_min_static"])
        self.c_vav = self.c_open
        # zone
        pa = psychro.p_atm(0)
        self.tz = p["initial_zone_temp"]
        self.wz = psychro.w_from_rh(self.tz, 45.0, pa)
        self.co2 = 450.0
        # air states along the unit (for display)
        self.t_mix = self.t_ph = self.t_fan = self.t_cc = self.t_hc = self.t_sa = self.tz
        self.w_sa = self.wz
        self.t_cold = self.tz
        # safeties
        self.frz_latched = False
        self.hs_latched = False
        self.hl_tripped = False
        # clock
        start = p["clock_start"]
        self.clock0 = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M") if start else _dt.datetime.now().replace(microsecond=0)
        self._occ = False
        self._demand = 0.0

    # ------------------------------------------------------------- parameters

    def param_items(self) -> List[tuple]:
        items = super().param_items()
        for d in DRIVES:
            for k, info in VFD_PARAM_INFO.items():
                items.append((f"{d}.{k}", ParamInfo(info.id, info.label, f"{DRIVE_LABEL[d]} drive (ACH580)",
                                                     info.units, info.choices, info.minimum, info.maximum),
                              self.drive_p[d][k]))
        return items

    def param_label(self, name: str) -> str:
        if "." in name:
            d, k = name.split(".", 1)
            info = VFD_PARAM_INFO.get(k)
            return f"{DRIVE_LABEL.get(d, d)} {info.id} {info.label}" if info else name
        return super().param_label(name)

    def set_param(self, name: str, value: Any) -> None:
        if "." in name:
            d, k = name.split(".", 1)
            if d not in self.drive_p or k not in VFD_DEFAULTS:
                raise KeyError(f"no drive parameter {name!r}")
            value = coerce(value, VFD_DEFAULTS[k])
            info = VFD_PARAM_INFO.get(k)
            if info and info.choices and value not in info.choices:
                raise ValueError(f"{name}: {value!r} is not one of {info.choices}")
            if info and info.minimum is not None and value < info.minimum:
                raise ValueError(f"{name}: minimum is {info.minimum}")
            if info and info.maximum is not None and value > info.maximum:
                raise ValueError(f"{name}: maximum is {info.maximum}")
            self.drive_p[d][k] = value
            return
        super().set_param(name, value)

    def action(self, name: str, value: Any = None) -> None:
        if name == "frz_reset":
            if self.t_cold > self.p["freezestat_setpoint"] + 2.0 and not self.fault("frz_trip"):
                self.frz_latched = False
            else:
                raise ValueError("freezestat is still cold - it won't reset")
        elif name == "hs_reset":
            if self.p_dis < self.p["high_static_setpoint"] - 0.2:
                self.hs_latched = False
            else:
                raise ValueError("duct pressure is still above the high static setting")
        elif name == "coil_repair":
            for c in (self.ph, self.cc, self.hc):
                c.repair()
        elif "." in name:
            d, key = name.split(".", 1)
            if d not in self.drives:
                raise KeyError(name)
            self._sync_inject()
            self.drives[d].keypad(key, value)
        else:
            raise KeyError(name)
        self._publish()

    # ------------------------------------------------------------- helpers

    def aggregate_share(self) -> float:
        """Share of the design airflow still served by the aggregate VAV model (the rest is simulated boxes)."""
        boxes = getattr(self, "boxes", [])
        covered = sum(b.p["box_max_cfm"] for b in boxes)
        return max(0.0, (self.p["design_cfm"] - covered) / self.p["design_cfm"])

    def return_air(self) -> tuple:
        """Return air (temp, humidity ratio, CO2): airflow-weighted mix of the aggregate zone and the simulated zones."""
        plenum = 1.5 if self._occ else 0.5
        parts = [(max(self.q_vav, 0.0), self.tz, self.wz, self.co2)]
        for b in getattr(self, "boxes", []):
            parts.append((max(b.q, 0.0), b.tz, b.wz, b.co2))
        total = sum(q for q, *_ in parts)
        if total < 10.0:
            share = self.aggregate_share()
            boxes = getattr(self, "boxes", [])
            if share > 0 or not boxes:
                return self.tz + plenum, self.wz, self.co2
            n = len(boxes)
            return (sum(b.tz for b in boxes) / n + plenum, sum(b.wz for b in boxes) / n, sum(b.co2 for b in boxes) / n)
        t = sum(q * tz for q, tz, _, _ in parts) / total
        w = sum(q * wz for q, _, wz, _ in parts) / total
        c = sum(q * co2 for q, _, _, co2 in parts) / total
        return t + plenum, w, c

    @property
    def p_dis(self) -> float:
        return getattr(self, "_p_dis", 0.0)

    def clock(self) -> _dt.datetime:
        return self.clock0 + _dt.timedelta(seconds=self.sim_time)

    def _occupied(self, now: _dt.datetime) -> bool:
        days = str(self.p["occ_days"]).lower()
        if "-" in days:
            a, b = days.split("-")
            allowed = WEEKDAYS[WEEKDAYS.index(a):WEEKDAYS.index(b) + 1]
        else:
            allowed = [x.strip() for x in days.split(",")]
        hour = now.hour + now.minute / 60.0
        return WEEKDAYS[now.weekday()] in allowed and self.p["occ_start"] <= hour < self.p["occ_end"]

    def _sync_inject(self) -> None:
        for d in DRIVES:
            inj = self.inject[d]
            inj.clear()
            for k in ("overcurrent_trip", "cooling_fan_failed"):
                if self.fault(f"{d}_{k}"):
                    inj.add(k)

    # ----------------------------------------------------------- airflow

    def _fan_flow(self, s: str, P: float, speed: float) -> float:
        """Supply fan s flow (cfm) at plenum pressure rise P, including its barometric damper."""
        Qf = 2.0 * self.p["fan_design_cfm"]            # free delivery at full speed
        shut = 6.0                                       # in. w.c. shutoff at full speed
        if self.fault(f"{s}_coupling_failed"):
            speed = 0.0
        fan_p = shut * speed * speed
        if self.fault(f"{s}_bd_stuck_closed"):
            return 0.0
        if P < fan_p:
            return Qf * math.sqrt(max(0.0, speed * speed - P / shut))
        if self.fault(f"{s}_bd_stuck_open"):
            return -1900.0 * math.sqrt(P - fan_p)       # backflow through the idle / weak fan
        return 0.0

    def _solve_supply(self) -> None:
        """
        Fan array against the duct: aggregate VAV boxes plus any simulated VAV boxes
        served by this AHU.  One bisection on the duct static at the sensor: the
        boxes' flow at that static sets the airflow, the airflow sets the fan
        pressure needed, and the fans' delivery at that pressure must match.
        """
        p = self.p
        D = p["design_cfm"]
        speeds = {s: self.drives[s].speed_pu for s in SF}
        k_filter = (0.35 * (1.0 + 2.0 * p["filter_loading"]) if not self.fault("filter_blown") else 0.05) / D ** 2
        k_coils = 1.45 / D ** 2                         # preheat, cooling, heating, humidifier, casing
        k_duct = 1.0 / D ** 2                           # fan discharge to the static sensor
        k_int = k_filter + k_coils + k_duct
        fire = 0.05 if self.fault("fire_damper_closed") else 1.0
        c_agg = self.c_vav * fire
        c_leak = 4000.0 if self.fault("duct_leak") else 0.0
        suction = max(0.0, -self.p_mp)
        self._k_filter = k_filter
        boxes = getattr(self, "boxes", [])
        duct_loss_prev = k_duct * self.q_s * self.q_s

        def box_inlet(b, psp: float) -> float:
            x = b.p["duct_position"]
            if x < 0.75:                                  # upstream of the sensor: part of the duct loss still ahead
                return psp + (0.75 - x) / 0.75 * duct_loss_prev
            return psp * b.inlet_factor()

        def flows(psp: float) -> float:
            if psp <= 0:
                return 0.0
            q = (c_agg + c_leak) * math.sqrt(psp)
            for b in boxes:
                q += fire * b.flow_at(box_inlet(b, psp))
            return q

        def fans_at(P: float) -> float:
            return sum(self._fan_flow(s, P, speeds[s]) for s in SF)

        hi = max([6.0 * sp * sp for sp in speeds.values()] + [0.0])
        psp = 0.0
        if hi > 0.01:
            lo, top = 0.0, hi
            for _ in range(40):
                mid = (lo + top) / 2.0
                q = flows(mid)
                if fans_at(suction + k_int * q * q + mid) > q:
                    lo = mid
                else:
                    top = mid
            psp = (lo + top) / 2.0
        Q = flows(psp)
        P = suction + k_int * Q * Q + psp if Q > 0 else 0.0
        self.p_fan = P
        for s in SF:
            self.q_fan[s] = self._fan_flow(s, P, speeds[s]) if Q > 0 else 0.0
        self.q_s = Q
        self.p_sp = psp
        self._p_dis = psp + k_duct * Q * Q
        self.q_vav = c_agg * math.sqrt(psp) if psp > 0 else 0.0
        self.q_boxes = 0.0
        for b in boxes:
            pin = box_inlet(b, psp) if Q > 0 else 0.0
            b._p_in_from_ahu = pin
            b._q_from_ahu = fire * b.flow_at(pin)
            self.q_boxes += b._q_from_ahu

    def _solve_return(self) -> None:
        """Return fan, dampers and building pressure for the current supply flow."""
        p = self.p
        s_r = self.drives["rf"].speed_pu if not self.fault("rf_coupling_failed") else 0.0
        Qf_r, shut_r = 2.0 * p["rf_design_cfm"], 2.4
        k_rd = 1.0 / p["rf_design_cfm"] ** 2
        c_oa = 43800.0 * damper_fraction(self.oa_pos)
        c_ra = 34800.0 * damper_fraction(self.ra_pos)
        c_ea = 36500.0 * damper_fraction(self.ea_pos)
        c_b = 9000.0
        Qs = self.q_s

        def resid(Qr: float) -> Tuple[float, tuple]:
            Qexf = Qs - Qr
            Pb = ssq(Qexf / c_b)
            fan = shut_r * (s_r * s_r - ssq(Qr / Qf_r) / 1.0) if s_r > 0 else -shut_r * ssq(Qr / Qf_r)
            Prp = Pb - k_rd * ssq(Qr) + fan
            Qea = c_ea * ssqrt(Prp)
            Qra = Qr - Qea
            Pmp = Prp - ssq(Qra / c_ra)
            Qoa = c_oa * ssqrt(-Pmp)
            return Qoa + Qra - Qs, (Pb, Prp, Pmp, Qoa, Qra, Qea)

        lo, hi = -0.5 * Qs - 5000.0, Qs + 20000.0
        r_lo, _ = resid(lo)
        for _ in range(60):
            mid = (lo + hi) / 2.0
            r, _ = resid(mid)
            if (r > 0) == (r_lo > 0):
                lo, r_lo = mid, r
            else:
                hi = mid
        Qr = (lo + hi) / 2.0
        _, (Pb, Prp, Pmp, Qoa, Qra, Qea) = resid(Qr)
        self.q_r, self.p_b, self.p_rp = Qr, Pb, Prp
        self.p_mp = 0.7 * self.p_mp + 0.3 * Pmp       # relaxed: feeds back into the supply solve
        self.q_oa, self.q_ra, self.q_ea = Qoa, Qra, Qea

    # ------------------------------------------------------------- step

    def _step(self, h: float) -> None:
        p = self.p
        pa = psychro.p_atm(0)
        now = self.clock()
        self._occ = self._occupied(now)
        self._sync_inject()

        # ---------------- safeties (contacts) ----------------
        frz_ok = not (self.frz_latched or self.fault("frz_trip"))
        hs_ok = not self.hs_latched
        ra_smk_ok = not self.fault("ra_smoke_alarm")
        sa_smk_ok = not self.fault("sa_smoke_alarm")
        chain = frz_ok and hs_ok and ra_smk_ok and sa_smk_ok

        # ---------------- drives ----------------
        def load_fn(d: str):
            if d == "rf":
                k = 0.69 * (1.25 if self.fault("rf_bearing_worn") else 1.0)
                if self.fault("rf_coupling_failed"):
                    return lambda s: 0.03 * s
                q_ratio = (self.q_r / max(1.0, 2.0 * p["rf_design_cfm"] * max(self.drives["rf"].speed_pu, 0.05)))
                return lambda s: k * s * s * (0.4 + 1.2 * max(0.0, min(1.0, q_ratio)))
            k = 0.87 * (1.25 if self.fault(f"{d}_bearing_worn") else 1.0)
            if self.fault(f"{d}_coupling_failed"):
                return lambda s: 0.03 * s
            phi = self.q_fan[d] / max(1.0, 2.0 * p["fan_design_cfm"] * max(self.drives[d].speed_pu, 0.05))
            return lambda s: k * s * s * (0.4 + 1.2 * max(0.0, min(1.0, phi)))

        for d in DRIVES:
            v = self.drives[d]
            v.inp.di = [bool(self.inputs[f"{d}_ss"]), False, False, chain, False, False]
            v.inp.ai1_pct = float(self.inputs[f"{d}_vfd_spd"])
            v.step(h, load_fn(d))

        # ---------------- actuators ----------------
        ma_sig = 0.0 if self.fault("ma_wire_broken") else float(self.inputs["ma_dmpr"])
        ma_cmd = signal_2_10(ma_sig)
        stuck_oa = self.fault_value("oa_damper_stuck") / 100.0 if self.fault("oa_damper_stuck") else None
        oa_act = self.act["oa"].step(h, ma_cmd, not self.fault("oa_actuator_failed"), stuck_oa)
        self.oa_pos = oa_act * (0.5 if self.fault("oa_linkage_slipped") else 1.0)
        self.ra_pos = 1.0 - self.act["ra"].step(h, ma_cmd, not self.fault("ra_actuator_failed"))
        self.ea_pos = self.act["ea"].step(h, ma_cmd, not self.fault("ea_actuator_failed"))

        def valve(name: str, fault_prefix: str, inp: str, wire_fault: str = "") -> float:
            sig = 0.0 if (wire_fault and self.fault(wire_fault)) else float(self.inputs[inp])
            stuck = self.fault_value(f"{fault_prefix}_valve_stuck") / 100.0 if self.fault(f"{fault_prefix}_valve_stuck") else None
            return self.act[name].step(h, signal_2_10(sig), not self.fault(f"{fault_prefix}_actuator_failed"), stuck)

        ph_pos = valve("ph", "ph", "ph_vlv", "ph_wire_broken")
        clg_pos = valve("clg", "clg", "clg_vlv")
        htg_pos = valve("htg", "htg", "htg_vlv")
        # humidifier valve: powered through the enable relay and the high-limit humidistat
        hum_powered = bool(self.inputs["hum_enable"]) and not self.hl_tripped
        hum_pos = self.act["hum"].step(h, signal_2_10(float(self.inputs["hum_vlv"])), hum_powered,
                                       1.0 if self.fault("hum_valve_stuck_open") else None)
        self.valve_pos = {"ph": ph_pos, "clg": clg_pos, "htg": htg_pos, "hum": hum_pos}

        # ---------------- VAV aggregate ----------------
        if p["demand_override"] >= 0:
            demand_pct = p["demand_override"]
        elif self._occ:
            hour = now.hour + now.minute / 60.0
            demand_pct = p["occ_demand"] + p["occ_peak"] * max(0.0, math.cos((hour - 14.0) / 8.0 * math.pi))
        else:
            demand_pct = p["unocc_demand"]
        share = self.aggregate_share()
        self._demand = p["design_cfm"] * share * demand_pct / 100.0
        c_open = self.c_open * share
        p_sp = getattr(self, "p_sp", 0.0)
        c_target = self._demand / math.sqrt(max(p_sp, 0.02))
        c_target = max(0.05 * c_open, min(c_open, c_target))
        self.c_vav += (c_target - self.c_vav) * min(1.0, h / 15.0)    # boxes re-position over ~15 s

        # ---------------- airflow ----------------
        self._solve_supply()
        self._solve_return()
        Qs = self.q_s

        # ---------------- air conditions through the unit ----------------
        t_oa, w_oa = p["oat"], p["oa_w"]
        t_ra, w_ra, co2_ra = self.return_air()            # zones + plenum gain from lights
        qoa, qra = max(0.0, self.q_oa), max(0.0, self.q_ra)
        if Qs > 100.0 and qoa + qra > 1.0:
            t_mix = (qoa * t_oa + qra * t_ra) / (qoa + qra)
            w_mix = (qoa * w_oa + qra * w_ra) / (qoa + qra)
            q_coil = Qs
        else:
            # fans off: wind/stack draft through the OA damper keeps pulling outdoor air into the
            # unit (that's how coils freeze when the OA damper doesn't close)
            share = min(1.0, damper_fraction(self.oa_pos) * 1.2)
            target = self.tz + (t_oa - self.tz) * share
            t_mix = self.t_mix + (target - self.t_mix) * min(1.0, h / 300.0)
            w_mix = self.wz + (w_oa - self.wz) * share
            q_coil = 2000.0 * damper_fraction(self.oa_pos)
        hw_ok = not self.fault("hw_plant_off")
        chw_ok = not self.fault("chw_plant_off")
        dp_fac = math.sqrt(max(0.0, p["loop_dp_pct"]) / 100.0)
        ph_gpm = 26.0 * equal_percentage(ph_pos) * dp_fac if hw_ok else 0.0
        hc_gpm = 26.0 * equal_percentage(htg_pos) * dp_fac if hw_ok else 0.0
        cc_gpm = 180.0 * equal_percentage(clg_pos) * dp_fac if chw_ok else 0.0
        chw_t = self.fault_value("chw_warm") if self.fault("chw_warm") else p["chw_supply_temp"]

        t1, w1 = self.ph.step(h, q_coil, t_mix, w_mix, p["hw_supply_temp"], ph_gpm, pa)
        # fan heat (motor and fan in the air stream)
        fan_kw = sum(max(0.0, self.drives[s].power_kw) for s in SF)
        t2 = t1 + (fan_kw * 3412.0 / (SENSIBLE * Qs) if Qs > 100 else 0.0)
        t3, w3 = self.cc.step(h, q_coil, t2, w1, chw_t, cc_gpm, pa)
        t4, w4 = self.hc.step(h, q_coil, t3, w3, p["hw_supply_temp"], hc_gpm, pa)
        t5, w5 = self.hum.step(Qs, t4, w4, hum_pos, not self.fault("steam_off"), pa)
        if Qs < 100:
            # no airflow: the air in the unit slowly takes the coil/room temperatures
            t5 = self.t_sa + (t4 - self.t_sa) * min(1.0, h / 120.0)
        self.t_mix, self.t_ph, self.t_fan, self.t_cc, self.t_hc, self.t_sa, self.w_sa = t_mix, t1, t2, t3, t4, t5, w5

        # coldest spot at the freezestat: unmixed outdoor air streaks through
        strat = 0.6 if self.fault("poor_mixing") else p["stratification"]
        oa_frac = qoa / max(Qs, 1.0) if Qs > 100 else 0.0
        self.t_cold = t1 - strat * max(0.0, t_mix - t_oa) * min(1.0, oa_frac * 2.0)

        # ---------------- safeties (detection) ----------------
        if not self.fault("frz_failed") and self.t_cold < p["freezestat_setpoint"]:
            self.frz_latched = True
        if not self.fault("hs_failed") and self._p_dis > p["high_static_setpoint"]:
            self.hs_latched = True
        rh_sa = psychro.rh_from_w(t5, w5, pa)
        hl_sp = p["hum_high_limit"] + (self.fault_value("hum_hl_drift") if self.fault("hum_hl_drift") else 0.0)
        if self.fault("hum_hl_failed"):
            self.hl_tripped = False
        elif rh_sa > hl_sp:
            self.hl_tripped = True
        elif rh_sa < hl_sp - 5.0:
            self.hl_tripped = False

        # ---------------- building (aggregate zones) ----------------
        share = self.aggregate_share()
        scale = p["load_scale"] / 100.0 * share
        occ = self._occ
        people = p["people"] * share if occ else 0.0
        internal = scale * ((410000.0 if occ else 80000.0) + 250.0 * people)      # Btu/h lights/equip + people
        env = 3000.0 * share * (t_oa - self.tz)
        supply = SENSIBLE * self.q_vav * (t5 - self.tz)
        heat_sp = p["zone_heat_sp"] if occ else p["zone_heat_sp_unocc"]
        net = internal + env + supply
        C_z = 30000.0 * max(share, 0.05)
        tz_new = self.tz + net / C_z * h / 3600.0
        if tz_new < heat_sp:
            tz_new = max(tz_new, min(heat_sp, self.tz + 0.02 * h))   # VAV reheat / perimeter heat holds heating setpoint
        self.tz = tz_new
        m_zone = 18000.0 * max(share, 0.05)               # lb dry air in the aggregate zones
        latent_lb_h = scale * people * 0.2
        infil = max(0.0, -self.q_ea) + max(0.0, -self.p_b) * 0.0
        dw = (AIR_LB_PER_CFM_H * self.q_vav * (w5 - self.wz) + latent_lb_h
              + AIR_LB_PER_CFM_H * infil * (w_oa - self.wz)) / m_zone
        self.wz = max(0.0005, self.wz + dw * h / 3600.0)
        w_satz = psychro.w_sat(self.tz, pa)
        self.wz = min(self.wz, w_satz)
        vol = 600000.0 * max(share, 0.05)                 # ft3
        co2_sa = (qoa * 420.0 + qra * co2_ra) / max(qoa + qra, 1.0) if Qs > 100 else co2_ra
        self.co2_sa = co2_sa                              # simulated VAV zones receive this
        gen = people * 0.0105                             # cfm CO2
        dco2 = (self.q_vav * (co2_sa - self.co2) + gen * 1e6 + 300.0 * share * (420.0 - self.co2)) / vol
        self.co2 += dco2 * h / 60.0

    # ------------------------------------------------------------- publish

    def _noise(self, sd: float) -> float:
        return self.rng.gauss(0.0, sd)

    def _publish(self) -> None:
        if not hasattr(self, "drives"):
            return
        p, s, pa = self.p, self.signals, psychro.p_atm(0)
        n = p["sensor_noise"]
        Qs = self.q_s
        pf_dp = self._k_filter * Qs * Qs if hasattr(self, "_k_filter") else 0.0
        t_ra, w_ra, co2_ra = self.return_air()
        rh_sa = psychro.rh_from_w(self.t_sa, self.w_sa, pa)
        rh_ra = psychro.rh_from_w(t_ra, w_ra, pa)

        # sensors (faults applied) -> ALC
        s["ma_temp"] = round(self.t_ph + self._noise(n) + (self.fault_value("mat_offset") if self.fault("mat_offset") else 0.0), 2)
        s["pf_dp"] = round((-pf_dp if self.fault("pf_tubes_reversed") else pf_dp) + self._noise(0.003), 3)
        s["ra_co2"] = round(self.fault_value("co2_failed") if self.fault("co2_failed") else co2_ra + self._noise(5), 0)
        s["ra_rh"] = round(min(100.0, rh_ra + self._noise(0.3) + (self.fault_value("ra_rh_drift") if self.fault("ra_rh_drift") else 0.0)), 1)
        s["ra_temp"] = round(t_ra + self._noise(n) + (self.fault_value("rat_offset") if self.fault("rat_offset") else 0.0), 2)
        s["sa_rh"] = round(self.fault_value("sa_rh_failed") if self.fault("sa_rh_failed") else min(100.0, rh_sa + self._noise(0.3)), 1)
        s["sa_static"] = round(0.0 if self.fault("static_tube_off") else max(-0.05, getattr(self, "p_sp", 0.0) + self._noise(0.005)), 3)
        s["sa_temp"] = round(self.t_sa + self._noise(n) + (self.fault_value("sat_offset") if self.fault("sat_offset") else 0.0), 2)
        drives_txt = []
        for d in DRIVES:
            v = self.drives[d]
            s[f"{d}_amps"] = round(min(30.0, max(0.0, v.current + (self._noise(0.04) if v.modulating else 0.0))), 2)
            s[f"{d}_vfd_fb"] = round(v.ao1_pct(), 1)
            s[f"{d}_vfd_flt"] = v.relay(self.drive_p[d]["ro3_source"])
            s[f"{d}_freq"] = round(v.freq, 1)
            drives_txt.append(f"{d}|{v.mode}|{v.status_text()}|{v.freq:.1f}|{v.current:.1f}|{'; '.join(v.warnings)}|{v.hand_ref:.0f}")
        s["drives"] = "\n".join(drives_txt)
        s["frz_stat"] = not (self.frz_latched or self.fault("frz_trip"))
        s["hi_static"] = not self.hs_latched
        s["ra_smoke"] = not self.fault("ra_smoke_alarm")
        s["sa_smoke"] = not self.fault("sa_smoke_alarm")

        # true values
        s["supply_cfm"] = round(Qs, 0)
        s["return_cfm"] = round(self.q_r, 0)
        s["oa_cfm"] = round(self.q_oa, 0)
        s["ea_cfm"] = round(self.q_ea, 0)
        s["oa_fraction"] = round(100.0 * max(0.0, self.q_oa) / Qs, 1) if Qs > 100 else 0.0
        s["oa_dmpr_pos"] = round(100.0 * getattr(self, "oa_pos", 0.0), 1)
        s["ra_dmpr_pos"] = round(100.0 * getattr(self, "ra_pos", 1.0), 1)
        s["ea_dmpr_pos"] = round(100.0 * getattr(self, "ea_pos", 0.0), 1)
        vp = getattr(self, "valve_pos", {"ph": 0, "clg": 0, "htg": 0, "hum": 0})
        s["ph_vlv_pos"] = round(100.0 * vp["ph"], 1)
        s["clg_vlv_pos"] = round(100.0 * vp["clg"], 1)
        s["htg_vlv_pos"] = round(100.0 * vp["htg"], 1)
        s["hum_vlv_pos"] = round(100.0 * vp["hum"], 1)
        s["mixed_temp"] = round(self.t_mix, 2)
        s["ph_lat"] = round(self.t_ph, 2)
        s["coldest_temp"] = round(self.t_cold, 1)
        s["fan_lat"] = round(self.t_fan, 2)
        s["cc_lat"] = round(self.t_cc, 2)
        s["hc_lat"] = round(self.t_hc, 2)
        s["sa_temp_actual"] = round(self.t_sa, 2)
        s["sa_dewpoint"] = round(psychro.dewpoint(self.t_sa, self.w_sa, pa), 1)
        s["ph_lwt"] = round(self.ph.lwt, 1)
        s["cc_lwt"] = round(self.cc.lwt, 1)
        s["hc_lwt"] = round(self.hc.lwt, 1)
        s["ph_gpm"] = round(self.ph.flow_gpm, 1)
        s["cc_gpm"] = round(self.cc.flow_gpm, 1)
        s["hc_gpm"] = round(self.hc.flow_gpm, 1)
        s["cc_tons"] = round(max(0.0, -self.cc.q) / 12000.0, 1)
        s["ph_mbh"] = round(max(0.0, self.ph.q) / 1000.0, 1)
        s["hc_mbh"] = round(max(0.0, self.hc.q) / 1000.0, 1)
        s["steam_lb_h"] = round(self.hum.steam, 1)
        s["fan_dp"] = round(self.p_fan, 2)
        s["discharge_static"] = round(self.p_dis, 2)
        s["sa_static_actual"] = round(getattr(self, "p_sp", 0.0), 3)
        s["mix_plenum_p"] = round(self.p_mp, 3)
        s["return_plenum_p"] = round(self.p_rp, 3)
        s["building_p"] = round(self.p_b, 3)
        s["pf_dp_actual"] = round(pf_dp, 3)
        s["vav_demand_cfm"] = round(self._demand, 0)
        s["vav_open_pct"] = round(100.0 * self.c_vav / max(self.c_open * self.aggregate_share(), 1.0), 1)
        s["boxes_cfm"] = round(getattr(self, "q_boxes", 0.0), 0)
        s["boxes"] = len(getattr(self, "boxes", []))
        s["zone_temp"] = round(self.tz, 2)
        s["zone_rh"] = round(psychro.rh_from_w(self.tz, self.wz, pa), 1)
        s["zone_co2"] = round(self.co2, 0)
        s["ra_temp_actual"] = round(t_ra, 2)
        s["oat"] = round(p["oat"], 1)
        for sf in SF:
            s[f"{sf}_cfm"] = round(self.q_fan[sf], 0)
            # barometric damper: open when the fan pushes air, closed under backpressure
            q = self.q_fan[sf]
            if self.fault(f"{sf}_bd_stuck_open"):
                pos = 100.0
            elif self.fault(f"{sf}_bd_stuck_closed"):
                pos = 0.0
            else:
                pos = min(100.0, max(0.0, q / p["fan_design_cfm"] * 100.0))
            s[f"{sf}_bd_pos"] = round(pos, 0)
        s["occupied"] = self._occ
        s["starved"] = bool(Qs > 100 and self.q_vav < 0.95 * self._demand)
        s["safety_chain_ok"] = bool(s["frz_stat"] and s["hi_static"] and s["ra_smoke"] and s["sa_smoke"])
        s["frz_tripped"] = self.frz_latched or self.fault("frz_trip")
        s["hs_tripped"] = self.hs_latched
        s["hum_hl_tripped"] = self.hl_tripped
        s["duct_wetting"] = self.hum.wetting
        s["ph_frozen"] = self.ph.frozen or self.ph.damaged
        s["cc_frozen"] = self.cc.frozen or self.cc.damaged
        s["hc_frozen"] = self.hc.frozen or self.hc.damaged
        s["sim_clock"] = self.clock().strftime("%a %Y-%m-%d %H:%M")

    @staticmethod
    def fault_text(code: int) -> str:
        return FAULT_TEXT.get(int(code), "")
