"""
Lochinvar Crest FCB condensing boiler with SMART TOUCH control, controlled by
a BAS over BACnet (or Modbus).  Built from the manuals (docs/CREST_DESIGN.md):

  * Crest Installation & Operation Manual, FCB1000-6000, 100343224_2000600568 Rev U
    (section 10: operating information, protection, sequence of operation)
  * ModBus and BACnet Communication Instructions, 100161012_2000020084 Rev X
    (sections 4 and 7: memory maps; 6: timing; 10: unit operation; 12: conversions;
    13: status / blocking / lockout codes)

How the BAS commands it (communication manual section 10):
  AV0 Configuration bits tell the boiler which BAS values to use: bit 0 = Boiler
  Enable (AV1 Coils bit 0 / BV0), bit 2 = Rate / Setpoint Command (AV2), bit 4 =
  System Supply Temperature (AV6) ...  Values whose bit is clear are ignored.
  The BAS timer (5-120 s) restarts on every BAS write; when it lapses the boiler
  reverts to its own controls: its hardwired Enable input and its local setpoint.

Temperatures on the BAS are in °C (0.1 °C); Modbus registers carry them x10.

Values in neither manual are marked (A): sequence timings, ignition tries, the
louver proving time, the codes for an open flow switch and a blocked vent.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Set

from ...base import ParamInfo
from ..vendor import register
from .base import Boiler

# ---------------------------------------------------------------- codes (communication manual section 13, Crest)

STATUS_CODES = {
    2: "Heat demand blocked: high absolute outlet temperature",
    3: "Heat demand blocked: high absolute flue temperature",
    4: "Heat demand blocked: high absolute delta T",
    7: "Heat demand blocked: changed personality plug",
    8: "Heat demand blocked: low 24 VAC",
    9: "Outdoor shutdown",
    10: "Blocked: boiler switched off (display)",
    11: "No heat demand requested",
    12: "Blocked: line frequency",
    16: "Service function",
    19: "DHW function: storage tank",
    21: "SH function: heat demand from room thermostat",
    22: "SH function: heat demand from Boiler Management System",
    23: "SH function: heat demand from cascade",
    30: "Heat demand activated by freeze protection",
    32: "DHW pump delay",
    33: "SH pump delay",
    34: "No heat function (after pump delay)",
    40: "Lockout",
}
BLOCKING_CODES = {
    0: "No blocking",
    5: "Low 24 VAC supply",
    6: "MRHL is open",
    7: "High outdoor temperature",
    8: "Boiler switched off (display)",
    10: "High delta T",
    11: "High outlet water temperature",
    12: "Anti-cycling time",
    13: "Changed ID plug",
    14: "Line frequency",
    15: "High flue temperature",
}
LOCKOUT_CODES = {
    134: "Louvers Not Open",
    167: "Blocked Drain Switch Open",
    169: "Gas Pressure Switch Open",
    170: "Low Water Cut-Off Open",
    179: "Inlet Sensor Short",
    180: "Inlet Sensor Open",
    192: "Outlet Sensor Short",
    193: "Outlet Sensor Open",
    232: "Flame Failure 1",
    233: "Ignition Failure",
    240: "External Manual Reset High Limit",
    244: "Large Gas Valve Relay Failure",
    246: "Internal Manual Reset High Limit",
}

CREST_FAULTS = {
    "ignition_fail": "no ignition (lockout 233 after 3 tries)",
    "flame_unstable": "flame drops out while running (recycles, then lockout 232)",
    "high_limit_trip": "auxiliary manual reset high limit open (lockout 240)",
    "lwco_trip": "low water cutoff open (lockout 170)",
    "flow_sw_open": "flow switch failed open (no firing)",
    "flow_sw_stuck": "flow switch stuck closed (fires without flow)",
    "flue_blocked": "blocked vent / drain switch opens while firing (lockout 167)",
    "gas_valve": "gas valve relay failure at ignition (lockout 244)",
    "hx_scaled": "scaled heat exchanger (high flue temp, low efficiency)",
    "supply_sensor_open": "outlet sensor S1 open (lockout 193)",
    "supply_sensor_short": "outlet sensor S1 shorted (lockout 192)",
    "return_sensor_open": "inlet sensor S2 open (lockout 180)",
    "return_sensor_short": "inlet sensor S2 shorted (lockout 179)",
    "gateway_offline": "BACnet / ModBus interface offline (the BAS timer lapses)",
    "gas_pressure": "gas pressure switch open (lockout 169)",
    "louver_fail": "louver proving switch fails open (blocks a start; lockout 134 if firing)",
}

# ---------------------------------------------------------------- integration (communication manual sections 4 and 7)
# key, BACnet (kind, instance, name), Modbus (kind, number), units, role, Modbus scale
_C, _PCT, _N = "degreesCelsius", "percent", "noUnits"
_POINTS = [
    # written by the BAS
    ("enable_bv", ("binary-value", 0, "Boiler Enable"), ("coil", 1), None, "cmd", 1),
    ("tank_tstat", ("binary-value", 4, "Tank Thermostat"), ("coil", 5), None, "cmd", 1),
    ("config", ("analog-value", 0, "Configuration"), ("holding-register", 40001), _N, "cmd", 1),
    ("coils", ("analog-value", 1, "Coils"), ("holding-register", 40002), _N, "cmd", 1),
    ("rate_cmd", ("analog-value", 2, "0-10 Volt Input / Rate Command / Setpoint Command"),
     ("holding-register", 40003), _PCT, "cmd", 1),
    ("tank_sp_c", ("analog-value", 3, "Tank Setpoint"), ("holding-register", 40004), _C, "cmd", 10),
    ("tank_temp_c", ("analog-value", 4, "Tank Temperature"), ("holding-register", 40005), _C, "cmd", 10),
    ("oat_c", ("analog-value", 5, "Outdoor Temperature"), ("holding-register", 40006), _C, "cmd", 10),
    ("sys_supply_c", ("analog-value", 6, "System Supply Temperature"), ("holding-register", 40007), _C, "cmd", 10),
    ("sys_return_c", ("analog-value", 7, "System Return Temperature"), ("holding-register", 40008), _C, "cmd", 10),
]
# binary inputs (BACnet BI n = Modbus discrete input 10001 + n); FCB models
_BI = [(0, "Manual Reset High Limit", "mrhl"), (1, "Flow Switch", "flow_switch"),
       (2, "Gas Pressure Switch", "gas_ps"), (3, "Louver Proving Switch", "louver_ps"),
       (5, "Blocked Drain Switch", "drain_sw"), (7, "Flame 1", "flame1"), (10, "Blocked Vent Switch", "vent_sw"),
       (17, "Internal LWCO", "lwco"), (18, "Gas Pressure Switch 2", "gas_ps2"), (20, "Flue Damper Switch", "damper_sw"),
       (22, "Flame 2", "flame2"), (25, "Enable", "enable_in"), (29, "Tank Thermostat Input", "tank_in"),
       (32, "Run Time Contacts", "runtime"), (33, "Alarm Contacts", "alarm_contacts"), (34, "Boiler Pump", "boiler_pump"),
       (35, "DHW Pump", "dhw_pump"), (36, "Louver Relay", "louver_relay"), (37, "Gas Valve 1", "gas_valve1"),
       (38, "System Pump", "system_pump"), (40, "DHW Recirc Pump", "recirc_pump"), (43, "Flue Damper Output", "damper_out"),
       (45, "Gas Valve 2", "gas_valve2"), (48, "Main Fan 1", "fan1"), (50, "External Spark / HSI", "spark")]
# analog inputs (BACnet AI n = Modbus input register 30001 + n)
_AI = [(0, "Binary Inputs 0-15", "bi_0_15", _N, 1), (1, "Binary Inputs 16-31", "bi_16_31", _N, 1),
       (2, "Binary Inputs 32-47", "bi_32_47", _N, 1), (3, "System / Cascade Setpoint", "sys_sp_c", _C, 10),
       (4, "System Pump Speed", "sys_pump_speed", _PCT, 1), (5, "Cascade Total Power", "cascade_total", _PCT, 1),
       (6, "Cascade Current Power", "cascade_current", _PCT, 1), (7, "Outlet Setpoint", "outlet_sp_c", _C, 10),
       (8, "Outlet Temperature", "outlet_c", _C, 10), (9, "Inlet Temperature", "inlet_c", _C, 10),
       (10, "Flue Temperature", "flue_c", _C, 10), (11, "Firing Rate", "firing_rate", _PCT, 1),
       (12, "Boiler Pump Speed", "boiler_pump_speed", _PCT, 1), (13, "Boiler Status Code", "status_code", _N, 1),
       (14, "Boiler Blocking Code", "blocking_code", _N, 1), (15, "Boiler Lockout Code", "lockout_code", _N, 1),
       (18, "Air Damper Target Angle", "air_damper", _N, 1), (19, "Gas Damper Target Angle", "gas_damper", _N, 1),
       (20, "Fan Speed Target", "fan_speed", _N, 1), (22, "O2 Sensor Status", "o2_status", _N, 1),
       (23, "Current Trim Factor", "trim", _N, 1), (24, "Barometer", "barometer", _N, 1),
       (25, "Binary Inputs 48-63", "bi_48_63", _N, 1), (35, "Flame 1 Current", "flame1_ua", _N, 10),
       (36, "Flame 2 Current", "flame2_ua", _N, 10), (37, "Corrected O2 Reading", "o2_pct", _N, 10)]
_POINTS += [(k, ("binary-input", n, name), ("discrete-input", 10001 + n), None, None, 1) for n, name, k in _BI]
_POINTS += [(k, ("analog-input", n, name), ("input-register", 30001 + n), u, None, sc) for n, name, k, u, sc in _AI]

CREST_OBJECTS = [(b[0], b[1], b[2], key, units, role) for key, b, _m, units, role, _s in _POINTS]
CREST_MODBUS_OBJECTS = [(m[0], m[1], b[2], key, units, role, float(sc)) for key, b, m, units, role, sc in _POINTS]
BI_NUMBER = {k: n for n, _name, k in _BI}

# ---------------------------------------------------------------- limits and timings

# I&O section 10, protection features
OUTLET_REDUCE_F = 185.0            # reduce fan speed above
OUTLET_RESTART_DROP_F = 10.0       # auto-reset high limit: restart after the outlet drops 10 °F
FLUE_REDUCE_F, FLUE_SHUTDOWN_F, FLUE_RESTART_DROP_F = 215.0, 225.0, 10.0
DT_REDUCE_F, DT_SHUTDOWN_F, DT_RESTART_F = 70.0, 80.0, 50.0
INTERNAL_MRHL_F = 205.0
FREEZE_PUMP_F, FREEZE_FIRE_F = 45.0, 37.0
TARGET_MIN_F, TARGET_MAX_F = 32.0, 185.0

# sequence timings (A): not in the I&O or the communication manual
PREPURGE_S = 10.0
IGNITION_S = 4.0
STABILIZE_S = 30.0
IGNITION_RATE = 0.40
POSTPURGE_S = 30.0
TRIES = 3
RATE_SLEW = 0.02                   # per second (2 %/s)
VENT_BLOCK_S = 120.0
START_THRESHOLD = 5.0              # % rate command that starts a call for heat in rate mode

SENSOR_OPEN_F, SENSOR_SHORT_F = -40.0, 302.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


@register
class CrestBoiler(Boiler):
    MODEL = "lochinvar_crest"
    MAKE = "Lochinvar"
    DISPLAY = "Lochinvar Crest FCB (SMART TOUCH)"
    SHORT = "Lochinvar Crest"
    PROTOCOL = "bacnet"
    INTEGRATION = "Crest BACnet objects (Rev X section 7)"
    BINARY_CMD_TEXT = ("ON", "OFF")
    DEVICE_MODEL_NAME = "Lochinvar Crest SMART TOUCH BACnet (simulated)"
    DEFAULT_DESCRIPTION = "Lochinvar Crest FCB condensing boiler"
    OBJECTS = CREST_OBJECTS
    BMS_MODES = ("rate", "setpoint")
    ROLES = {"enable": "coils", "rate_cmd": "rate_cmd", "sp_cmd": "rate_cmd", "config": "config",
             "firing": "firing_rate", "flame": "flame1", "supply_t": "outlet_c", "return_t": "inlet_c",
             "alarm": "alarm_contacts", "pump_request": "boiler_pump", "flow_switch": "flow_switch", "local": None}
    PANEL = ["power", "reset"]
    PANEL_LABELS = {"power": "Power"}
    PANEL_PARAMS = {
        "local_sp": ParamInfo("", "Space heating set point (local)", "display", "°F", minimum=TARGET_MIN_F,
                              maximum=TARGET_MAX_F),
        "bas_timer_s": ParamInfo("", "BAS timer (Setup > BAS)", "display", "s", minimum=5, maximum=120),
        "anti_cycle_s": ParamInfo("", "Anti-cycling time", "display", "s", minimum=0, maximum=1200),
        "auto_hl_f": ParamInfo("", "Auto reset high limit", "display", "°F", minimum=100, maximum=200),
        "aux_mrhl_f": ParamInfo("", "Auxiliary manual reset high limit (dial)", "display", "°F", minimum=100,
                                maximum=210),
        "bms_t_low_f": ParamInfo("", "BMS temperature at low analog input (2 V)", "display", "°F", minimum=32,
                                 maximum=185),
        "bms_t_high_f": ParamInfo("", "BMS temperature at high analog input (10 V)", "display", "°F", minimum=32,
                                  maximum=185),
    }
    SETTINGS = {
        "demand_config": "enable_bms",    # enable (ENABLE active, BMS inactive) / enable_bms / bms (BMS only)
        "bms_type": "rate",               # rate / setpoint (the plant's boiler_bms_mode)
        "hw_enable": False,               # hardwired Enable input made (jumpered)
        "bas_timer_s": 120.0,
        "anti_cycle_s": 60.0,             # (A) installer adjustable
        "pump_delay_s": 30.0,             # I&O page 43: factory 30 s
        "auto_hl_f": 195.0,
        "aux_mrhl_f": 200.0,
        "local_sp": 180.0,
        "bms_t_low_f": 69.8,
        "bms_t_high_f": 179.6,
    }
    FAULTS = CREST_FAULTS
    FAULT_ALIASES = {"comm_loss": "gateway_offline"}
    CODE_TABLES = {"status": STATUS_CODES, "blocking": BLOCKING_CODES, "lockout": LOCKOUT_CODES}
    CONDENSING = True

    @classmethod
    def cmd_default(cls, key: str, cfg: Dict[str, Any]) -> Any:
        return 0.0

    @classmethod
    def bas_setup_writes(cls, mode: str) -> Dict[str, Any]:
        """What a BAS must write besides the commands: the configuration bits (enable + rate/setpoint)."""
        return {"config": 5.0}

    @classmethod
    def encode_role(cls, role: str, value: Any, settings: Optional[Dict[str, Any]] = None) -> Any:
        """Engineering value -> what the BAS writes: enable -> coil bit, setpoint °F -> % of the 0-10 V span."""
        if role == "enable":
            return 1.0 if value else 0.0
        if role == "sp_cmd":
            st = {**cls.SETTINGS, **(settings or {})}
            lo, hi = float(st["bms_t_low_f"]), float(st["bms_t_high_f"])
            volts = (float(value) - lo) * (10.0 - 2.0) / (hi - lo) + 2.0
            return round(_clamp(volts, 0.0, 10.0) * 10.0)
        return value

    def __init__(self, tag: str, cfg: Dict[str, Any], faults: Set[str], rng: Optional[random.Random] = None,
                 t_init: float = 70.0):
        self.tag = tag
        self.faults = faults
        self.rng = rng or random.Random()
        self._init_core(cfg, t_init)
        st = self.settings_for(cfg)
        self.demand_config = str(st["demand_config"]).lower()
        if self.demand_config not in ("enable", "enable_bms", "bms"):
            raise ValueError(f"{tag}: demand_config {self.demand_config!r} (enable / enable_bms / bms)")
        self.bms_type = self.check_mode(st["bms_type"])
        self.hw_enable = bool(st["hw_enable"])
        self.pump_delay_s = float(st["pump_delay_s"])
        for k in self.PANEL_PARAMS:
            setattr(self, k, float(st[k]))
        # BAS objects (as last written)
        self.enable_bv = False
        self.tank_tstat = False
        self.config = 0.0
        self.coils = 0.0
        self.rate_cmd = 0.0
        self.tank_sp_c = self.tank_temp_c = self.oat_c = self.sys_supply_c = self.sys_return_c = 0.0
        self.bas_idle_s = 0.0             # real seconds since the BAS last wrote (set by the runner)
        self._offline_s = 0.0
        # external permissives and power
        self.powered = True
        self.louver_ok = True
        self.gas_ok = True
        self.comm_ok = True
        self.display_on = True
        # sequence
        self.seq = 0                      # 0 standby, 1 pre-purge, 2 ignition, 3 stabilization, 4 running, 5 post-purge
        self.timer = 0.0
        self.tries = 0
        self.lockout = 0                  # active lockout code (0 = none)
        self.last_lockout = 0             # AI15 keeps the last code (only status 40 means locked out)
        self.blocking = 0
        self.flame_signal = 0.0
        self.run_hours = 0.0
        self.cycles = 0
        self.anti_cycle = 0.0
        self.off_timer = 0.0              # minimum off time after a limit shutdown
        self.post = 0.0
        self.pump_delay = 0.0
        self.louver_wait = 0.0
        self.vent_s = 0.0
        self.freeze = False
        self.demand_source = 0            # status code of the current demand (21 / 22 / 30), 0 none
        self.target_f = self.local_sp
        self._i = 0.0
        self._dropout_in = self._next_dropout()
        self._limit_hold = {"outlet": False, "flue": False, "dt": False}
        self.note = ""                    # display text for conditions the manual gives no code

    # --------------------------------------------------------------- BAS side
    def bas_write(self, key: str, value: Any, seen: bool = True) -> None:
        if key not in self.cmd_keys():
            raise KeyError(f"{self.DISPLAY}: {key!r} is not a command object")
        if key in ("enable_bv", "tank_tstat"):
            setattr(self, key, bool(value))
        else:
            setattr(self, key, float(value))
        if seen:
            self.bas_idle_s = 0.0

    @property
    def bas_active(self) -> bool:
        """The BAS timer has not lapsed (an offline interface means no writes: the timer runs out)."""
        return self.bas_idle_s + self._offline_s < self.bas_timer_s

    def _config_bit(self, bit: int) -> bool:
        return bool(int(self.config) & (1 << bit))

    # --------------------------------------------------------------- display
    def display(self, key: str) -> None:
        if key == "power":
            self.display_on = not self.display_on
            if not self.display_on:
                self._burner_off()
        elif key == "reset":
            if self.lockout and not self._lockout_cause_present():
                self.lockout = 0
                self.tries = 0
                self.seq = 0
                self.timer = 0.0
                self.louver_wait = 0.0
                self.vent_s = 0.0
        else:
            raise KeyError(key)

    panel = display

    def _lockout_cause_present(self) -> bool:
        f, c = self.faults, self.lockout
        return ((c == 240 and ("high_limit_trip" in f or self._hl_reading() >= self.aux_mrhl_f))
                or (c == 246 and self._hl_reading() >= INTERNAL_MRHL_F)
                or (c == 170 and "lwco_trip" in f)
                or (c == 169 and ("gas_pressure" in f or not self.gas_ok))
                or (c == 134 and not self.louver_proven())
                or (c in (193, 192) and ("supply_sensor_open" in f or "supply_sensor_short" in f))
                or (c in (180, 179) and ("return_sensor_open" in f or "return_sensor_short" in f)))

    def _next_dropout(self) -> float:
        return self.rng.uniform(120.0, 420.0)

    # --------------------------------------------------------------- sensors and switches
    def flow_switch(self) -> bool:
        if "flow_sw_open" in self.faults:
            return False
        if "flow_sw_stuck" in self.faults:
            return True
        return self.gpm >= self.min_flow

    def supply_reading(self) -> float:
        if "supply_sensor_open" in self.faults:
            return SENSOR_OPEN_F
        if "supply_sensor_short" in self.faults:
            return SENSOR_SHORT_F
        return self.t_hx

    def return_reading(self) -> float:
        if "return_sensor_open" in self.faults:
            return SENSOR_OPEN_F
        if "return_sensor_short" in self.faults:
            return SENSOR_SHORT_F
        return self.t_ret

    def _hl_reading(self) -> float:
        return self.t_hx + self.hot_spot

    def louver_proven(self) -> bool:
        return self.louver_ok and "louver_fail" not in self.faults

    # --------------------------------------------------------------- call for heat
    def _demand(self) -> Optional[float]:
        """Requested firing rate 0..1 (None = no call for heat); sets demand_source and target_f."""
        bas = self.bas_active
        cfg = self.demand_config
        if not self.display_on:
            self.demand_source = 0
            return None
        # what the boiler listens to (configuration bits, BAS timer -> its own controls)
        use_bas_enable = bas and cfg in ("enable", "enable_bms") and self._config_bit(0)
        use_bas_signal = bas and cfg in ("enable_bms", "bms") and self._config_bit(2)
        if cfg == "bms" or (cfg == "enable_bms" and bas):
            enabled = (bool(int(self.coils) & 1) or self.enable_bv) if use_bas_enable else self.hw_enable
            if cfg == "bms":
                enabled = True
            signal = self.rate_cmd if use_bas_signal else 0.0
            if not bas:                         # reverted to its own controls: hardwired enable, local setpoint
                return self._local_demand(self.hw_enable, 21)
            if not enabled:
                self.demand_source = 0
                return None
            if self.bms_type == "rate":
                if (self.seq in (0, 5) and signal < START_THRESHOLD) or signal <= 0.0:
                    self.demand_source = 0
                    return None
                self.demand_source = 22
                self.target_f = self.local_sp
                return _clamp(signal / 100.0, 0.0, 1.0)
            # setpoint: % of the 0-10 V span, 2 V = low temperature, 10 V = high temperature
            volts = signal / 10.0
            if volts < 2.0:
                self.demand_source = 0
                return None
            sp = self.bms_t_low_f + (volts - 2.0) * (self.bms_t_high_f - self.bms_t_low_f) / 8.0
            return self._modulate_to(_clamp(sp, TARGET_MIN_F, TARGET_MAX_F), 22)
        # ENABLE active, BMS inactive (or BAS timer lapsed): enable from the BAS or the terminal, local setpoint
        enabled = ((bool(int(self.coils) & 1) or self.enable_bv) if use_bas_enable else self.hw_enable)
        return self._local_demand(enabled, 22 if use_bas_enable else 21)

    def _local_demand(self, enabled: bool, source: int) -> Optional[float]:
        if not enabled:
            self.demand_source = 0
            return None
        return self._modulate_to(self.local_sp, source)

    def _control_temp(self) -> float:
        if self.bas_active and self._config_bit(4) and self.sys_supply_c:
            return c_to_f(self.sys_supply_c)
        return self.supply_reading()

    def _modulate_to(self, sp: float, source: int) -> Optional[float]:
        self.target_f = sp
        err = sp - self._control_temp()
        self._i = _clamp(self._i + 0.0005 * err, 0.0, 1.0)
        r = _clamp(0.04 * err + self._i, 0.0, 1.0)
        if (self.seq in (0, 5) and err < 5.0) or err <= -5.0:
            self.demand_source = 0
            return None
        self.demand_source = source
        return r

    # --------------------------------------------------------------- step
    def _burner_off(self, post: bool = True) -> None:
        if self.flame or self.seq in (2, 3, 4):
            self.anti_cycle = self.anti_cycle_s
            if post and self.powered:
                self.post = POSTPURGE_S
                self.seq = 5
            else:
                self.seq = 0
        elif self.seq in (1, 2):
            self.seq = 0
        self.flame = False
        self.rate = 0.0

    def _lock(self, code: int) -> None:
        self._burner_off(post=True)
        self.lockout = self.last_lockout = code

    def _limits(self) -> int:
        """Auto-reset limits (I&O section 10) -> blocking code, holding until their restart condition."""
        out_f, in_f, flue = self.supply_reading(), self.return_reading(), self.flue
        hold = self._limit_hold
        if out_f > self.auto_hl_f:
            hold["outlet"] = True
        elif hold["outlet"] and out_f <= self.auto_hl_f - OUTLET_RESTART_DROP_F and self.off_timer <= 0:
            hold["outlet"] = False
        if flue > FLUE_SHUTDOWN_F:
            hold["flue"] = True
        elif hold["flue"] and flue <= FLUE_SHUTDOWN_F - FLUE_RESTART_DROP_F and self.off_timer <= 0:
            hold["flue"] = False
        if self.flame and out_f - in_f > DT_SHUTDOWN_F:
            hold["dt"] = True
        elif hold["dt"] and out_f - in_f < DT_RESTART_F and self.off_timer <= 0:
            hold["dt"] = False
        if hold["outlet"]:
            return 11
        if hold["flue"]:
            return 15
        if hold["dt"]:
            return 10
        return 0

    def _rate_limit(self) -> float:
        lim = 1.0
        out_f = self.supply_reading()
        if out_f > OUTLET_REDUCE_F:
            lim = min(lim, 1.0 - (out_f - OUTLET_REDUCE_F) / (self.auto_hl_f - OUTLET_REDUCE_F) * (1.0 - self.min_rate))
        if self.flue > FLUE_REDUCE_F:
            lim = min(lim, 1.0 - (self.flue - FLUE_REDUCE_F) / (FLUE_SHUTDOWN_F - FLUE_REDUCE_F) * (1.0 - self.min_rate))
        dt = out_f - self.return_reading()
        if dt > DT_REDUCE_F:
            lim = min(lim, 1.0 - (dt - DT_REDUCE_F) / (DT_SHUTDOWN_F - DT_REDUCE_F) * (1.0 - self.min_rate))
        return _clamp(lim, self.min_rate, 1.0)

    def step(self, h: float, gpm: float, ewt: float, t_room: float = 75.0) -> float:
        """Advance h s with gpm entering at ewt. Returns the leaving water temperature."""
        f = self.faults
        self.gpm = max(0.0, gpm)
        self.t_ret = ewt
        self._offline_s = 0.0 if self.comm_ok else self._offline_s + h
        self.anti_cycle = max(0.0, self.anti_cycle - h)
        self.off_timer = max(0.0, self.off_timer - h)
        self.post = max(0.0, self.post - h) if self.powered else 0.0
        self.note = ""

        hl = self._core_hot_spot(h)
        if not self.powered:
            self._burner_off(post=False)
            self.seq, self.demand_source, self.pump_delay = 0, 0, 0.0
            self.blocking = 0
        else:
            # latched safeties: they act whatever the call for heat
            if not self.lockout:
                if "supply_sensor_open" in f:
                    self._lock(193)
                elif "supply_sensor_short" in f:
                    self._lock(192)
                elif "return_sensor_open" in f:
                    self._lock(180)
                elif "return_sensor_short" in f:
                    self._lock(179)
                elif "high_limit_trip" in f or hl >= self.aux_mrhl_f:
                    self._lock(240)
                elif hl >= INTERNAL_MRHL_F:
                    self._lock(246)
                elif "lwco_trip" in f:
                    self._lock(170)
            limit = self._limits()
            if limit and self.flame:
                self._burner_off()
                self.off_timer = self.anti_cycle_s
            demand = self._demand() if not self.lockout else None
            # freeze protection (I&O section 10)
            if self.return_reading() < FREEZE_FIRE_F and not self.lockout:
                self.freeze = True
            elif self.return_reading() > FREEZE_PUMP_F:
                self.freeze = False
            if self.freeze and demand is None:
                demand, self.demand_source = self.min_rate, 30
            if not self.display_on:
                self.blocking = 8
            elif limit:
                self.blocking = limit
            elif demand is not None and self.anti_cycle > 0 and self.seq in (0, 5):
                self.blocking = 12
            else:
                self.blocking = 0
            if self.lockout:
                self._burner_off()
            elif self.blocking:
                if self.seq in (1, 2, 3, 4):
                    self._burner_off()
            else:
                self._sequence(h, demand)
        # pump delay after a call for heat (I&O page 43)
        if self.demand_source or self.seq in (1, 2, 3, 4):
            self.pump_delay = self.pump_delay_s
        elif self.powered:
            self.pump_delay = max(0.0, self.pump_delay - h)

        self._core_hx(h, ewt, t_room)
        if self.flame:
            self.run_hours += h / 3600.0
        target_fs = (2.0 if "flame_unstable" in f else 8.0) if self.flame else 0.0
        self.flame_signal += (target_fs - self.flame_signal) * min(1.0, h / 2.0)
        if self.flame:
            self.flame_signal += self.rng.gauss(0.0, 0.05 if "flame_unstable" not in f else 0.4)
        self.flame_signal = max(0.0, self.flame_signal)
        return self.t_hx

    def _sequence(self, h: float, demand: Optional[float]) -> None:
        f = self.faults
        call = demand is not None and demand > 0.0
        st = self.seq
        if st == 5 and self.post <= 0:
            st = 0
        if st in (0, 5):
            if call and self.anti_cycle <= 0:
                # louver relay closes; wait for the proving switch
                if not self.flow_switch():
                    self.note = "Flow switch open"
                elif not self.louver_proven():
                    # I&O sequence step 1: the louver relay closes and the boiler blocks until proving closes
                    self.louver_wait += h
                    self.note = "Waiting for louver proving switch"
                else:
                    self.louver_wait = 0.0
                    st, self.timer = 1, 0.0
            else:
                self.louver_wait = 0.0
        elif st == 1:                                   # pre-purge
            if not call or not self.flow_switch():
                st = 0
            else:
                self.timer += h
                if self.timer >= PREPURGE_S:
                    if not self.gas_ok or "gas_pressure" in f:
                        self._lock(169)
                        return
                    st, self.timer = 2, 0.0
                    if "gas_valve" in f:
                        self._lock(244)
                        return
        elif st == 2:                                   # trial for ignition
            self.timer += h
            if self.timer >= IGNITION_S:
                if "ignition_fail" in f:
                    self.tries += 1
                    self.flame = False
                    if self.tries >= TRIES:
                        self._lock(233)
                        return
                    st, self.timer = 1, 0.0               # re-purge and retry
                else:
                    self.flame = True
                    self.rate = IGNITION_RATE
                    self.cycles += 1
                    self.tries = 0
                    st, self.timer = 3, 0.0
                    self._dropout_in = self._next_dropout()
                    self.vent_s = 0.0
        elif st in (3, 4):
            if not self.louver_proven():                # proof lost while firing (A: lockout 134)
                self._lock(134)
                return
            if not call or not self.flow_switch():
                if not self.flow_switch():
                    self.note = "Flow switch open"
                self._burner_off()
                return
            if st == 3:                                 # flame stabilization
                self.timer += h
                self.rate = IGNITION_RATE
                if self.timer >= STABILIZE_S:
                    st = 4
            else:
                if demand < self.min_rate:
                    self._burner_off()
                    return
                target = min(_clamp(demand, self.min_rate, 1.0), self._rate_limit())
                step = RATE_SLEW * h
                self.rate += _clamp(target - self.rate, -step, step)
            if "flue_blocked" in f:
                self.vent_s += h
                if self.vent_s > VENT_BLOCK_S:
                    self._lock(167)
                    return
            if "flame_unstable" in f:
                self._dropout_in -= h
                if self._dropout_in <= 0:
                    self.tries += 1
                    self._dropout_in = self._next_dropout()
                    self._burner_off()
                    if self.tries >= TRIES:
                        self._lock(232)
                    return
        self.seq = st
        if st not in (2, 3, 4):
            self.flame = False
            self.rate = 0.0

    # --------------------------------------------------------------- outputs
    @property
    def status_code(self) -> int:
        if self.lockout:
            return 40
        if not self.display_on:
            return 10
        if self.blocking in (11, 15, 10) and self.demand_source:
            return {11: 2, 15: 3, 10: 4}[self.blocking]
        if self.demand_source:
            return self.demand_source
        if self.pump_delay > 0:
            return 33
        return 11

    def status_text(self) -> str:
        if not self.powered:
            return "NO POWER"
        code = self.status_code
        if code == 40:
            return f"Lockout: {self.lockout} {LOCKOUT_CODES.get(self.lockout, '')}"
        txt = {0: "Standby", 1: "Pre-purge", 2: "Ignition", 3: "Flame stabilization", 4: "Running",
               5: "Post-purge"}[self.seq] if code in (21, 22, 30) else STATUS_CODES.get(code, str(code))
        if self.seq in (3, 4):
            txt += f" {100 * self.rate:.0f} %"
        if self.blocking:
            txt += f" · Blocking {self.blocking}: {BLOCKING_CODES.get(self.blocking, '')}"
        if self.note:
            txt += f" · {self.note}"
        if code in (21, 22, 30):
            txt += f" · {STATUS_CODES[code]}"
        if not self.bas_active:
            txt += " · BAS timer lapsed: own controls"
        return txt

    def points(self) -> Dict[str, Any]:
        out_f, in_f = self.supply_reading(), self.return_reading()
        powered = self.powered
        bi = {
            "mrhl": "high_limit_trip" not in self.faults and self.lockout not in (240, 246),
            "flow_switch": self.flow_switch(),
            "gas_ps": self.gas_ok and "gas_pressure" not in self.faults,
            "louver_ps": self.louver_proven(),
            "drain_sw": not ("flue_blocked" in self.faults and self.vent_s > VENT_BLOCK_S / 2),
            "flame1": bool(self.flame),
            "vent_sw": "flue_blocked" not in self.faults or self.vent_s < VENT_BLOCK_S / 2,
            "lwco": "lwco_trip" not in self.faults,
            "gas_ps2": self.gas_ok and "gas_pressure" not in self.faults,
            "damper_sw": self.seq in (1, 2, 3, 4),
            "flame2": False,
            "enable_in": self.hw_enable,
            "tank_in": False,
            "runtime": bool(self.flame),
            "alarm_contacts": bool(self.lockout),
            "boiler_pump": self.pump_request,
            "dhw_pump": False,
            "louver_relay": bool(self.demand_source) or self.seq in (1, 2, 3, 4) or self.post > 0,
            "gas_valve1": bool(self.flame) or self.seq == 2,
            "system_pump": False,
            "recirc_pump": False,
            "damper_out": self.seq in (1, 2, 3, 4) or self.post > 0,
            "gas_valve2": False,
            "fan1": self.seq in (1, 2, 3, 4) or self.post > 0,
            "spark": self.seq == 2,
        }
        if not powered:
            bi = {k: False for k in bi}
        words = [0, 0, 0, 0]
        for k, on in bi.items():
            if on:
                n = BI_NUMBER[k]
                words[n // 16] |= 1 << (n % 16)
        fan = (40.0 + 60.0 * self.rate) if self.flame else (100.0 if self.seq == 1 or self.post > 0 else 0.0)
        return {
            "enable_bv": bool(self.enable_bv), "tank_tstat": bool(self.tank_tstat), "config": float(self.config),
            "coils": float(self.coils), "rate_cmd": float(self.rate_cmd), "tank_sp_c": float(self.tank_sp_c),
            "tank_temp_c": float(self.tank_temp_c), "oat_c": float(self.oat_c), "sys_supply_c": float(self.sys_supply_c),
            "sys_return_c": float(self.sys_return_c),
            **bi,
            "bi_0_15": float(words[0]), "bi_16_31": float(words[1]), "bi_32_47": float(words[2]),
            "bi_48_63": float(words[3]),
            "sys_sp_c": round(f_to_c(self.target_f), 1) if powered else 0.0,
            "sys_pump_speed": 0.0, "cascade_total": 100.0 if powered else 0.0,
            "cascade_current": round(100.0 * self.rate, 0) if self.flame else 0.0,
            "outlet_sp_c": round(f_to_c(self.target_f) * 2.0) / 2.0 if powered else 0.0,
            "outlet_c": round(f_to_c(out_f), 1) if powered else 0.0,
            "inlet_c": round(f_to_c(in_f), 1) if powered else 0.0,
            "flue_c": round(f_to_c(self.flue), 1) if powered else 0.0,
            "firing_rate": round(100.0 * self.rate, 0) if self.flame else 0.0,
            "boiler_pump_speed": 100.0 if self.pump_request else 0.0,
            "status_code": float(self.status_code) if powered else 0.0,
            "blocking_code": float(self.blocking) if powered else 0.0,
            "lockout_code": float(self.last_lockout) if powered else 0.0,
            "air_damper": round(fan, 0), "gas_damper": round(100.0 * self.rate, 0) if self.flame else 0.0,
            "fan_speed": round(fan * 60.0, 0),
            "o2_status": 1.0 if self.flame else 0.0, "trim": 100.0, "barometer": 1013.0 if powered else 0.0,
            "flame1_ua": round(self.flame_signal, 1), "flame2_ua": 0.0,
            "o2_pct": round(6.5 - 2.0 * self.rate, 1) if self.flame else 20.9,
        }

    def role(self, role: str, points: Optional[Dict[str, Any]] = None) -> Any:
        v = super().role(role, points)
        if role in ("supply_t", "return_t") and v is not None:
            return round(c_to_f(v), 1)
        return v

    # --------------------------------------------------------------- interface
    @property
    def alarm(self) -> bool:
        return bool(self.lockout)

    @property
    def pump_request(self) -> bool:
        return bool(self.powered and (self.demand_source or self.seq in (1, 2, 3, 4) or self.pump_delay > 0
                                      or self.freeze))

    def generic_state(self) -> str:
        if not self.powered:
            return "no_power"
        if self.lockout:
            return "lockout"
        if self.seq in (1, 2):
            return "starting"
        if self.seq in (3, 4):
            return "running"
        if self.seq == 5:
            return "stopping"
        if self.blocking and self.blocking != 12:
            return "alarm"
        if not self.bas_active:
            return "local"
        return "off"

    def codes(self) -> List[str]:
        out = [f"STATUS {self.status_code} {STATUS_CODES.get(self.status_code, '')}"]
        if self.blocking:
            out.append(f"BLOCKING {self.blocking} {BLOCKING_CODES.get(self.blocking, '')}")
        if self.lockout:
            out.append(f"LOCKOUT {self.lockout} {LOCKOUT_CODES.get(self.lockout, '')}")
        return out

    def view_fields(self) -> List[tuple]:
        bas = (f"BAS {'active' if self.bas_active else 'LAPSED'} · config {int(self.config)}"
               f" · {self.demand_config} / {self.bms_type}")
        return [("BMS", bas), ("Flame", f"{self.flame_signal:.1f} µA"), ("Flue", f"{self.flue:.0f} °F"),
                ("Eff.", f"{100.0 * self.eff:.1f} %"), ("Hours · cycles", f"{self.run_hours:.1f} · {self.cycles}")]


@register
class CrestBoilerModbus(CrestBoiler):
    """The same boiler on its ModBus map (communication manual section 4), behind an RS-485 gateway."""

    MODEL = "lochinvar_crest_modbus"
    DISPLAY = "Lochinvar Crest FCB (SMART TOUCH, ModBus)"
    SHORT = "Lochinvar Crest (ModBus)"
    PROTOCOL = "modbus_tcp"
    INTEGRATION = "Crest ModBus map (Rev X section 4)"
    OBJECTS = CREST_MODBUS_OBJECTS
