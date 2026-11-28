"""
Lochinvar Crest condensing boiler (FB series) with SMART TOUCH control in BMS mode.

ASSUMPTIONS (docs/HW_PLANT_DESIGN.md, marked (A)): the Crest manuals and BMS
points list were not available when this was written.  Timings, limits, the
lockout / blocking code lists and the BACnet object names are documented
placeholders, kept in the tables below so they can be replaced in one place.

Control (BMS, direct firing rate): the BAS writes BMS ENABLE and BMS FIRING
RATE (0-100 %).  The boiler runs its own safety sequence:

  standby -> permissives -> pre-purge -> ignition trial -> low fire hold
          -> modulating (commanded rate, limited) -> post-purge -> anti-cycle
  blocking conditions stop the burner and clear by themselves;
  lockouts latch until Reset on the display.  Power loss never latches.
"""

from __future__ import annotations

import random
from typing import Any, Dict, Optional, Set

from ...base import ParamInfo
from ..vendor import register
from .base import Boiler

# ---------------------------------------------------------------- code tables (A)

STATUS_CODES = {
    0: "Standby",
    1: "Pre-purge",
    2: "Ignition",
    3: "Low fire hold",
    4: "Running",
    5: "Post-purge",
    6: "Anti-cycle delay",
    7: "Blocked",
    8: "Lockout",
    9: "Local mode",
}
LOCKOUT_CODES = {
    0: "None",
    1: "Ignition failure (3 tries)",
    2: "Manual reset high limit",
    3: "Low water cutoff",
    4: "Gas valve fault",
    5: "Blocked flue / condensate",
}
BLOCKING_CODES = {
    0: "None",
    1: "Low flow (flow switch open)",
    2: "Combustion air louver not proven",
    3: "Gas pressure switch",
    4: "Power loss / emergency stop",
    5: "Operating limit (200 F)",
    6: "BMS communication loss",
    7: "Flame loss - retrying",
    8: "Supply sensor fault",
    9: "Return sensor fault",
}

CREST_FAULTS = {
    "ignition_fail": "no ignition (locks out after 3 tries)",
    "flame_unstable": "unstable flame / dirty flame sensor (dropouts and retries)",
    "high_limit_trip": "manual reset high limit tripped",
    "lwco_trip": "low water cutoff tripped",
    "flow_sw_open": "flow switch failed open",
    "flow_sw_stuck": "flow switch stuck closed (fires without flow)",
    "flue_blocked": "blocked flue / condensate drain (cut-back, then lockout)",
    "gas_valve": "gas valve fault",
    "hx_scaled": "scaled heat exchanger (high flue temp, low efficiency)",
    "supply_sensor_open": "supply sensor open",
    "supply_sensor_short": "supply sensor shorted",
    "return_sensor_open": "return sensor open",
    "return_sensor_short": "return sensor shorted",
    "gateway_offline": "BMS (BACnet) interface offline",
}

# BACnet object list (A): kind, instance, object name, key, units, role
#   role "cmd" = written by the BAS, None = read only, ("msv", table) = multi-state with texts
_F, _PCT = "degreesFahrenheit", "percent"
CREST_OBJECTS = [
    ("binary-value", 1, "BMS ENABLE", "bms_enable", None, "cmd"),
    ("analog-value", 1, "BMS FIRING RATE", "bms_rate", _PCT, "cmd"),
    ("analog-value", 10, "SUPPLY TEMP", "supply_temp", _F, None),
    ("analog-value", 11, "RETURN TEMP", "return_temp", _F, None),
    ("analog-value", 12, "FLUE TEMP", "flue_temp", _F, None),
    ("analog-value", 13, "FIRING RATE", "firing_rate", _PCT, None),
    ("analog-value", 14, "INPUT MBH", "input_mbh", "kiloBtusPerHour", None),
    ("analog-value", 15, "FLAME SIGNAL", "flame_signal", "noUnits", None),
    ("analog-value", 16, "EFFICIENCY", "efficiency", _PCT, None),
    ("analog-value", 17, "RUN HOURS", "run_hours", "hours", None),
    ("analog-value", 18, "CYCLES", "cycles", "noUnits", None),
    ("analog-value", 19, "CONDENSATE", "condensate_gph", "usGallonsPerHour", None),
    ("binary-value", 10, "BOILER PUMP REQUEST", "pump_request", None, None),
    ("binary-value", 11, "FLOW SWITCH", "flow_switch", None, None),
    ("binary-value", 12, "LWCO", "lwco_ok", None, None),
    ("binary-value", 13, "LOCAL/BMS", "bms_mode", None, None),
    ("binary-value", 14, "ALARM", "alarm", None, None),
    ("binary-value", 15, "FLAME", "flame", None, None),
    ("multi-state-value", 1, "STATUS", "status_code", None, ("msv", STATUS_CODES)),
    ("multi-state-value", 2, "LOCKOUT CODE", "lockout_code", None, ("msv", LOCKOUT_CODES)),
    ("multi-state-value", 3, "BLOCKING CODE", "blocking_code", None, ("msv", BLOCKING_CODES)),
]

# sequence timings and limits (A)
PREPURGE_S = 10.0
IGNITION_S = 4.0
LOW_FIRE_HOLD_S = 30.0
POSTPURGE_S = 30.0
ANTI_CYCLE_S = 60.0
IGNITION_RATE = 0.40
MIN_RATE = 0.04                 # 25:1 turndown
START_THRESHOLD = 5.0           # % BMS rate that starts a call for heat
TRIES = 3
RATE_SLEW = 0.02                # per second (2 %/s)
CUTBACK_SUPPLY = 190.0
OPERATING_LIMIT = 200.0
HIGH_LIMIT = 210.0
MAX_RISE = 40.0
COMM_HOLD_S = 60.0
LOCAL_SP = 180.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@register
class CrestBoiler(Boiler):
    MODEL = "lochinvar_crest"
    MAKE = "Lochinvar"
    DISPLAY = "Lochinvar Crest (SMART TOUCH, BMS)"
    SHORT = "Lochinvar Crest"
    PROTOCOL = "bacnet"
    INTEGRATION = "Crest BMS objects (assumed names)"
    BINARY_CMD_TEXT = ("ENABLE", "DISABLE")
    DEVICE_MODEL_NAME = "Lochinvar Crest SMART TOUCH BMS (simulated)"
    DEFAULT_DESCRIPTION = "Lochinvar Crest condensing boiler"
    OBJECTS = CREST_OBJECTS
    BMS_MODES = ("rate",)
    ROLES = {"enable": "bms_enable", "rate_cmd": "bms_rate", "sp_cmd": None,
             "firing": "firing_rate", "flame": "flame", "supply_t": "supply_temp", "return_t": "return_temp",
             "alarm": "alarm", "pump_request": "pump_request", "flow_switch": "flow_switch", "local": None}
    PANEL = ["local", "bms", "reset"]
    PANEL_LABELS = {"bms": "BMS"}
    PANEL_PARAMS = {"local_sp": ParamInfo("", "Local supply setpoint", "display", "°F", minimum=80, maximum=190)}
    FAULTS = CREST_FAULTS
    FAULT_ALIASES = {"comm_loss": "gateway_offline"}
    CODE_TABLES = {"status": STATUS_CODES, "lockout": LOCKOUT_CODES, "blocking": BLOCKING_CODES}

    def __init__(self, tag: str, cfg: Dict[str, Any], faults: Set[str], rng: Optional[random.Random] = None,
                 t_init: float = 70.0):
        self.tag = tag
        self.faults = faults
        self._init_core(cfg, t_init)
        self.rng = rng or random.Random()
        # BMS / display
        self.bms_enable = False
        self.bms_rate = 0.0
        self.local = False
        self.local_sp = LOCAL_SP
        self.comm_ok = True
        self._comm_lost_s = 0.0
        self._held_rate = 0.0
        # external permissives and power
        self.powered = True
        self.louver_ok = True
        self.gas_ok = True
        # state
        self.state = 0
        self.timer = 0.0
        self.tries = 0
        self.lockout = 0
        self.blocking = 0
        self.flame_signal = 0.0
        self.run_hours = 0.0
        self.cycles = 0
        self._flue_block_s = 0.0
        self._dropout_in = self._next_dropout()
        self._i = 0.0
        self.anti_cycle = 0.0
        self.post = 0.0
        self.pump_req_s = 0.0

    # --------------------------------------------------------------- display
    def display(self, key: str) -> None:
        if key == "local":
            self.local = True
        elif key == "bms":
            self.local = False
        elif key == "reset":
            if self.lockout and not self._lockout_cause_present():
                self.lockout = 0
                self.tries = 0
                self.state = 0
                self.timer = 0.0
        else:
            raise KeyError(key)

    panel = display

    def bas_write(self, key: str, value: Any) -> None:
        if key == "bms_enable":
            self.bms_enable = bool(value)
        elif key == "bms_rate":
            self.bms_rate = float(value)
        else:
            raise KeyError(f"{self.DISPLAY}: {key!r} is not a command object")

    def _lockout_cause_present(self) -> bool:
        f = self.faults
        return ("high_limit_trip" in f and self.lockout == 2) or ("lwco_trip" in f and self.lockout == 3)

    def _next_dropout(self) -> float:
        return self.rng.uniform(120.0, 420.0)

    # --------------------------------------------------------------- sensors
    def flow_switch(self) -> bool:
        if "flow_sw_open" in self.faults:
            return False
        if "flow_sw_stuck" in self.faults:
            return True
        return self.gpm >= self.min_flow

    def supply_reading(self) -> float:
        if "supply_sensor_open" in self.faults:
            return -40.0
        if "supply_sensor_short" in self.faults:
            return 302.0
        return self.t_hx

    def return_reading(self) -> float:
        if "return_sensor_open" in self.faults:
            return -40.0
        if "return_sensor_short" in self.faults:
            return 302.0
        return self.t_ret

    # --------------------------------------------------------------- call for heat
    def _demand(self) -> Optional[float]:
        """Requested firing rate 0..1, or None when there is no call for heat."""
        if self.local:
            err = self.local_sp - self.t_hx
            self._i = _clamp(self._i + 0.0005 * err, 0.0, 1.0)
            r = _clamp(0.04 * err + self._i, 0.0, 1.0)
            if self.state in (0, 6) and err < 5.0:
                return None
            return r if err > -5.0 else None
        if not self.comm_ok:
            return self._held_rate if self._comm_lost_s < COMM_HOLD_S and self._held_rate > 0 else None
        if not self.bms_enable:
            return None
        if self.state in (0, 6) and self.bms_rate < START_THRESHOLD:
            return None
        if self.bms_rate <= 0.0:
            return None
        return _clamp(self.bms_rate / 100.0, 0.0, 1.0)

    def _blocking_now(self) -> int:
        f = self.faults
        if not self.powered:
            return 4
        if "supply_sensor_open" in f or "supply_sensor_short" in f:
            return 8
        if "return_sensor_open" in f or "return_sensor_short" in f:
            return 9
        if not self.flow_switch():
            return 1
        if not self.louver_ok:
            return 2
        if not self.gas_ok:
            return 3
        if self.t_hx >= OPERATING_LIMIT or (self.blocking == 5 and self.t_hx > CUTBACK_SUPPLY):
            return 5
        if not self.local and not self.comm_ok and self._comm_lost_s >= COMM_HOLD_S:
            return 6
        return 0

    def _burner_off(self, post: bool = True) -> None:
        if self.flame or self.state in (2, 3, 4):
            self.anti_cycle = ANTI_CYCLE_S
            if post and self.powered:
                self.post = POSTPURGE_S
        self.flame = False
        self.rate = 0.0

    # --------------------------------------------------------------- step
    def step(self, h: float, gpm: float, ewt: float, t_room: float = 75.0) -> float:
        """Advance h s with gpm entering at ewt. Returns the leaving water temperature."""
        f = self.faults
        self.gpm = max(0.0, gpm)
        self.t_ret = ewt
        if not self.comm_ok:
            self._comm_lost_s += h
        else:
            self._comm_lost_s = 0.0
            if self.bms_enable:
                self._held_rate = _clamp(self.bms_rate / 100.0, 0.0, 1.0)
            else:
                self._held_rate = 0.0
        self.anti_cycle = max(0.0, self.anti_cycle - h)
        self.post = max(0.0, self.post - h) if self.powered else 0.0

        # latched safeties (hardware limits work regardless of the call for heat)
        hl_reading = self._core_hot_spot(h)
        if not self.lockout:
            if "high_limit_trip" in f or (self.powered and hl_reading >= HIGH_LIMIT):
                self._lock(2)
            elif "lwco_trip" in f:
                self._lock(3)

        block = self._blocking_now()
        demand = self._demand() if not self.lockout else None
        if self.lockout:
            self._burner_off()
            self.state = 8
        elif block:
            if self.state in (1, 2, 3, 4):
                self._burner_off()
            self.blocking = block
            self.state = 7
            self.tries = 0
        else:
            if self.state == 7:
                self.state = 6 if self.anti_cycle > 0 else 0
            if self.blocking != 7:
                self.blocking = 0
            self._sequence(h, demand)

        self._core_hx(h, ewt, t_room)
        if self.flame:
            self.run_hours += h / 3600.0
        # flame signal (µA)
        target_fs = (2.0 if "flame_unstable" in f else 8.0) if self.flame else 0.0
        self.flame_signal += (target_fs - self.flame_signal) * min(1.0, h / 2.0)
        if self.flame:
            self.flame_signal += self.rng.gauss(0.0, 0.05 if "flame_unstable" not in f else 0.4)
        self.flame_signal = max(0.0, self.flame_signal)
        if self.state in (2, 3, 4) or self.post > 0:
            self.pump_req_s = 30.0
        else:
            self.pump_req_s = max(0.0, self.pump_req_s - h)
        return self.t_hx

    def _lock(self, code: int) -> None:
        self._burner_off(post=True)
        self.lockout = code
        self.state = 8

    def _rate_limit(self) -> float:
        lim = 1.0
        if self.t_hx > CUTBACK_SUPPLY:
            lim = min(lim, 1.0 - (self.t_hx - CUTBACK_SUPPLY) / (OPERATING_LIMIT - CUTBACK_SUPPLY) * (1.0 - self.min_rate))
        rise = self.t_hx - self.t_ret
        if rise > MAX_RISE:
            lim = min(lim, 1.0 - (rise - MAX_RISE) / 10.0)
        if "flue_blocked" in self.faults:
            lim = min(lim, 0.5)
        return _clamp(lim, self.min_rate, 1.0)

    def _sequence(self, h: float, demand: Optional[float]) -> None:
        f = self.faults
        st = self.state
        call = demand is not None and demand > 0.0
        if st in (0, 6, 5):
            if st == 6 and self.anti_cycle <= 0:
                st = 0
            if st == 5 and self.post <= 0:
                st = 6 if self.anti_cycle > 0 else 0
            if st == 0 and self.post > 0:
                st = 5
            if st == 0 and call and self.anti_cycle <= 0:
                st, self.timer = 1, 0.0
            if st == 6 and self.post > 0:
                st = 5
        elif st == 1:                                   # pre-purge
            if not call:
                st = 0
            else:
                self.timer += h
                if self.timer >= PREPURGE_S:
                    st, self.timer = 2, 0.0
                    if "gas_valve" in f:
                        self._lock(4)
                        return
        elif st == 2:                                   # ignition trial
            self.timer += h
            if self.timer >= IGNITION_S:
                if "ignition_fail" in f:
                    self.tries += 1
                    self.flame = False
                    if self.tries >= TRIES:
                        self._lock(1)
                        return
                    st, self.timer = 1, 0.0               # re-purge and retry
                else:
                    self.flame = True
                    self.rate = IGNITION_RATE
                    self.cycles += 1
                    self.tries = 0
                    self.blocking = 0
                    st, self.timer = 3, 0.0
                    self._dropout_in = self._next_dropout()
                    self._flue_block_s = 0.0
        elif st in (3, 4):
            if not call:
                self._burner_off()
                st = 5 if self.post > 0 else 6
            else:
                if st == 3:
                    self.timer += h
                    self.rate = IGNITION_RATE
                    if self.timer >= LOW_FIRE_HOLD_S:
                        st = 4
                else:
                    target = _clamp(demand, self.min_rate, 1.0)
                    if demand < self.min_rate:
                        self._burner_off()
                        self.state = 5 if self.post > 0 else 6
                        return
                    target = min(target, self._rate_limit())
                    step = RATE_SLEW * h
                    self.rate += _clamp(target - self.rate, -step, step)
                if "flue_blocked" in f:
                    self._flue_block_s += h
                    if self._flue_block_s > 120.0:
                        self._lock(5)
                        return
                if "flame_unstable" in f:
                    self._dropout_in -= h
                    if self._dropout_in <= 0:
                        self._burner_off()
                        self.blocking = 7
                        self.tries += 1
                        self._dropout_in = self._next_dropout()
                        if self.tries >= TRIES:
                            self._lock(1)
                            return
                        st = 5
        self.state = st
        if st not in (2, 3, 4):
            self.flame = False
            self.rate = 0.0
        if st == 0 and self.blocking == 7:
            self.blocking = 0

    # --------------------------------------------------------------- outputs
    def status_text(self) -> str:
        if not self.powered:
            return "NO POWER"
        txt = STATUS_CODES.get(self.state, "?")
        if self.state == 8:
            txt += f": {LOCKOUT_CODES.get(self.lockout, '?')}"
        elif self.state == 7 or self.blocking:
            txt = f"{txt} · {BLOCKING_CODES.get(self.blocking, '?')}" if self.blocking else txt
        if self.state in (3, 4):
            txt += f" {100 * self.rate:.0f} %"
        if self.local:
            txt += " · LOCAL"
        return txt

    def points(self) -> Dict[str, Any]:
        status = 9 if (self.local and self.state in (0, 6)) else self.state
        return {
            "supply_temp": round(self.supply_reading(), 1),
            "return_temp": round(self.return_reading(), 1),
            "flue_temp": round(self.flue, 1),
            "firing_rate": round(100.0 * self.rate, 1) if self.flame else 0.0,
            "input_mbh": round(self.rate * self.input_mbh, 0) if self.flame else 0.0,
            "flame_signal": round(self.flame_signal, 2),
            "efficiency": round(100.0 * self.eff, 1),
            "run_hours": round(self.run_hours, 2),
            "cycles": float(self.cycles),
            "condensate_gph": round(self.condensate, 1),
            "pump_request": self.pump_request,
            "flow_switch": self.flow_switch(),
            "lwco_ok": "lwco_trip" not in self.faults and self.lockout != 3,
            "bms_mode": not self.local,
            "alarm": bool(self.lockout),
            "flame": bool(self.flame),
            "status_code": status,
            "lockout_code": self.lockout,
            "blocking_code": self.blocking,
        }

    # --------------------------------------------------------------- interface
    @property
    def alarm(self) -> bool:
        return bool(self.lockout)

    @property
    def pump_request(self) -> bool:
        return bool(self.pump_req_s > 0 or self.state in (1, 2, 3, 4))

    def generic_state(self) -> str:
        if not self.powered:
            return "no_power"
        if self.state == 8:
            return "lockout"
        if self.state in (1, 2):
            return "starting"
        if self.state in (3, 4):
            return "running"
        if self.state == 5:
            return "stopping"
        if self.state == 7:
            return "alarm"
        return "local" if self.local else "off"

    def codes(self) -> list:
        out = []
        if self.lockout:
            out.append(f"LOCKOUT {self.lockout} {LOCKOUT_CODES.get(self.lockout, '')}")
        if self.blocking:
            out.append(f"BLOCKING {self.blocking} {BLOCKING_CODES.get(self.blocking, '')}")
        return out

    def view_fields(self) -> list:
        return [("Flame", f"{self.flame_signal:.1f} µA"), ("Flue", f"{self.flue:.0f} °F"),
                ("Eff.", f"{100.0 * self.eff:.1f} %"), ("Hours · cycles", f"{self.run_hours:.1f} · {self.cycles}")]
