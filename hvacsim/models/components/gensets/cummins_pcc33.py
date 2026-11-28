"""
Cummins natural gas generator set with PowerCommand 3.3 control.

Register layout, run states, scaling and fault codes follow the Cummins
Modbus Register Mapping manual A029X159 (section 13 parametrics, section 14
fault status bitmaps, section 3 NFPA 110 bitmaps).  Timings are assumptions
(docs/GENERATOR_DESIGN.md, section 5).
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Set, Tuple

from ..vendor import register
from .base import GeneratorSet
from .core import RATED_RPM  # noqa: F401  (kept importable from here)

# run sequence state (register 40011)
SEQ_STOP, SEQ_DELAY, SEQ_WARMUP, SEQ_RATED, SEQ_COOLDOWN = 0, 1, 2, 3, 4
SEQ_TEXT = {0: "Stop", 1: "Time Delay to Start", 2: "Warmup at Idle", 3: "Rated Freq and Voltage",
            4: "Cooldown / Stop Delay", 5: "Cooldown at Idle", 6: "Rated to Idle Transition Delay"}
SWITCH_TEXT = {0: "Off", 1: "Auto", 2: "Manual"}

# fault type (register 40013, PC 3.x)
FT_NONE, FT_WARNING, FT_DERATE, FT_SHUTDOWN_COOLDOWN, FT_SHUTDOWN = 0, 1, 2, 3, 4

# fault code -> (text, type, (bitmap register, bit) from section 14, or None when not listed there)
FAULT_CODES: Dict[int, Tuple[str, int, Optional[Tuple[int, int]]]] = {
    143: ("Low Oil Pressure Warning", FT_WARNING, None),
    146: ("Pre-High Coolant Temperature", FT_WARNING, None),
    151: ("High Coolant Temperature Shutdown", FT_SHUTDOWN, None),
    234: ("Engine Overspeed", FT_SHUTDOWN, (40401, 13)),
    359: ("Fail To Start", FT_SHUTDOWN, (40403, 10)),
    415: ("Low Oil Pressure Shutdown", FT_SHUTDOWN, (40403, 12)),
    441: ("Low Battery Voltage", FT_WARNING, (40404, 4)),
    1438: ("Fail To Crank", FT_SHUTDOWN, (40407, 8)),
    1442: ("Weak Battery", FT_WARNING, (40407, 11)),
    1444: ("Overload", FT_WARNING, (40407, 13)),
    1446: ("High AC Voltage", FT_SHUTDOWN, (40407, 15)),
    1447: ("Low AC Voltage", FT_SHUTDOWN, (40408, 0)),
    1448: ("Underfrequency", FT_SHUTDOWN, (40408, 1)),
    1452: ("Genset Breaker Fail To Close", FT_WARNING, None),
    1457: ("Fail To Synchronize", FT_WARNING, None),
    1472: ("Overcurrent Shutdown", FT_SHUTDOWN, (40408, 10)),
    2678: ("Charger AC Failure", FT_WARNING, None),
    2725: ("Gas Supply Pressure Moderate Low", FT_DERATE, (40453, 10)),
    2569: ("Gas Supply Pressure Critical Low", FT_SHUTDOWN_COOLDOWN, (40464, 15)),
}
BITMAP_REGS = range(40400, 40476)            # fault status bitmaps (all readable, mostly zero)

# injectable faults (web page / instructor)
PCC_FAULTS = {
    "fail_to_crank": "fail to crank (1438)",
    "fail_to_start": "cranks, will not fire (359)",
    "high_coolant": "high coolant temperature (146 warning, 151 shutdown)",
    "low_oil": "low oil pressure (143 warning, 415 shutdown)",
    "overspeed": "overspeed on start (234)",
    "high_ac_volts": "high AC voltage (1446)",
    "low_ac_volts": "low AC voltage (1447)",
    "high_current": "overcurrent (1472)",
    "low_battery": "low battery voltage (441)",
    "weak_battery": "weak battery: slow crank (1442)",
    "low_gas": "low gas supply pressure: derate, then fail to start (2725 / 2569)",
    "fail_to_sync": "fails to synchronize (1457)",
    "cb_fail_close": "genset breaker fails to close (1452)",
    "charger_fail": "battery charger AC failure (2678)",
    "comm_loss": "Modbus communication loss (gateway: no response)",
}


# integration list (A029X159 section 13): kind, register, name, key, units, role
#   holding registers as the manual numbers them; the gateway applies the 4xxxx offset
_HR = "holding-register"
PCC_OBJECTS: List[Tuple] = [
    (_HR, 40010, "Control Switch Position", "switch", None, ("msv", SWITCH_TEXT, 3)),
    (_HR, 40011, "Genset Run Sequence State", "seq_state", None, ("msv", SEQ_TEXT, 7)),
    (_HR, 40012, "Most Recent Fault / Warning", "fault_code", None, None),
    (_HR, 40013, "Fault Type", "fault_type", None, None),
    (_HR, 40014, "Genset % Standby Total kW (x0.01)", "pct_kw", "percent", None),
    (_HR, 40016, "NFPA 110 Bitmap", "nfpa110", None, None),
    (_HR, 40017, "Extended NFPA 110 Bitmap", "nfpa110_ext", None, None),
    (_HR, 40021, "L-N Volts Average", "v_ln", "volts", None),
    (_HR, 40025, "L-L Volts Average", "v_ll", "volts", None),
    (_HR, 40029, "Current Average", "amps", "amperes", None),
    (_HR, 40034, "Total kW", "kw", "kilowatts", None),
    (_HR, 40038, "Total kVAR", "kvar", None, None),
    (_HR, 40039, "Total Power Factor (x0.01)", "pf", None, None),
    (_HR, 40043, "Total kVA", "kva", None, None),
    (_HR, 40044, "Frequency (x0.01 Hz)", "hz", "hertz", None),
    (_HR, 40061, "Battery Voltage (x0.1 V)", "battery", "volts", None),
    (_HR, 40062, "Oil Pressure (kPa)", "oil", None, None),
    (_HR, 40064, "Coolant Temperature (x0.1 C)", "coolant", None, None),
    (_HR, 40068, "Engine Speed (rpm)", "rpm", None, None),
    (_HR, 40069, "Number of Start Attempts", "starts", None, None),
    (_HR, 40070, "Engine Running Time (32-bit, x0.1 s)", "run_time", None, None),
    (_HR, 40074, "Number of Runs (32-bit)", "runs", None, None),
    (_HR, 40300, "Remote Start", "remote_start", None, "cmd"),
    (_HR, 40301, "Fault Reset", "fault_reset", None, "cmd"),
]

# engine events (shared core) and plant events (paralleling master) -> PowerCommand fault codes
EVENT_CODES = {
    "pre_high_coolant": 146, "high_coolant_shutdown": 151, "low_oil_warning": 143, "low_oil_shutdown": 415,
    "high_ac_volts": 1446, "low_ac_volts": 1447, "overcurrent": 1472,
    "overload": 1444, "underfrequency": 1448, "cb_fail_to_close": 1452, "fail_to_sync": 1457,
}

CRANK_S, REST_S, ATTEMPTS = 15.0, 15.0, 3
START_DELAY_S = 1.0
FIRE_AFTER_S = 3.0               # gas engines fire after a few seconds of cranking
RAMP_S = 6.0                     # fire -> rated speed
COOLDOWN_S = 300.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


@register
class Genset(GeneratorSet):
    MODEL = "cummins_pcc33"
    MAKE = "Cummins"
    DISPLAY = "Cummins PowerCommand 3.3"
    SHORT = "Cummins PC 3.3"
    PROTOCOL = "modbus_tcp"
    INTEGRATION = "PowerCommand 3.3 Modbus (A029X159)"
    OBJECTS = PCC_OBJECTS
    ROLES = {"remote_start": "remote_start", "fault_reset": "fault_reset", "running": "nfpa110",
             "at_rated": "seq_state", "kw": "kw", "hz": "hz", "volts": "v_ll", "shutdown": "fault_type",
             "warning": "fault_type", "in_auto": "switch", "fault_code": "fault_code"}
    PANEL = ["off", "auto", "manual", "reset"]
    PANEL_LABELS = {"reset": "Fault reset"}
    FAULTS = PCC_FAULTS
    EVENT_CODES = EVENT_CODES
    CODE_TABLES = {"fault": {c: t[0] for c, t in FAULT_CODES.items()}, "sequence": SEQ_TEXT, "switch": SWITCH_TEXT}

    def __init__(self, tag: str, cfg: Dict[str, Any], faults: Set[str], rng: Optional[random.Random] = None):
        self.tag = tag
        self._init_core(cfg)
        self.faults = faults
        self.comm_ok = True
        self.rng = rng or random.Random()
        self.switch = 1                  # 0 Off, 1 Auto, 2 Manual
        self.remote_start = 0            # register 40300
        self.master_call = False
        self.state = "stop"              # stop, delay, crank, rest, ramp, rated, cooldown
        self.timer = 0.0
        self.attempt = 0
        self.crank_total = 0.0
        self.cb_closed = False
        self.shutdown = False
        self.fault_code = 0              # active shutdown code (latched) or most recent warning
        self.recent = 0                  # most recent fault / warning code (40012)
        self.recent_type = 0
        self.active: Dict[int, int] = {}   # active fault codes -> type
        self.starts = 0                  # start attempts
        self.runs = 0
        self.load_demand_stopped = False
        self._sync_s = 0.0

    # ------------------------------------------------------------ commands
    @property
    def start_request(self) -> bool:
        if self.shutdown or self.switch == 0:
            return False
        return self.switch == 2 or bool(self.master_call) or bool(self.remote_start)

    def set_switch(self, pos: int) -> None:
        self.switch = int(pos)

    def fault_reset(self) -> bool:
        """Clears latched shutdowns (switch in Off or Auto, as on the real control)."""
        if self.switch == 2:
            return False
        self.shutdown = False
        self.fault_code = 0
        self.active = {k: v for k, v in self.active.items() if v in (FT_WARNING, FT_DERATE) and self._cause(k)}
        self.attempt = 0
        if self.state not in ("rated", "cooldown", "ramp"):
            self.state, self.timer = "stop", 0.0
        return True

    def _cause(self, code: int) -> bool:
        f = self.faults
        return {441: "low_battery" in f, 1442: "weak_battery" in f, 2678: "charger_fail" in f,
                2725: "low_gas" in f, 146: "high_coolant" in f and self.coolant_c > 101.0,
                143: "low_oil" in f and self.running}.get(code, False)

    def _raise(self, code: int) -> None:
        ftype = FAULT_CODES[code][1]
        if code not in self.active:
            self.active[code] = ftype
        self.recent, self.recent_type = code, ftype
        if ftype in (FT_SHUTDOWN, FT_SHUTDOWN_COOLDOWN) and not self.shutdown:
            self.shutdown = True
            self.fault_code = code
            self.cb_closed = False
            self.state, self.timer = "stop", 0.0

    def _clear(self, code: int) -> None:
        if code in self.active and self.active[code] in (FT_WARNING, FT_DERATE):
            del self.active[code]

    # ----- events (shared core and paralleling plant) -> PowerCommand codes
    def _event(self, name: str) -> None:
        self._raise(EVENT_CODES[name])

    def _event_clear(self, name: str) -> None:
        self._clear(EVENT_CODES[name])

    def _event_active(self, name: str) -> bool:
        return EVENT_CODES[name] in self.active

    plant_event = _event
    plant_event_clear = _event_clear
    plant_event_active = _event_active

    @property
    def at_rated(self) -> bool:
        return self.state == "rated" and not self.shutdown

    def capacity(self) -> float:
        return self.kw_rating * (0.75 if 2725 in self.active else 1.0)

    # ------------------------------------------------------------ step
    def step(self, h: float, t_amb: float, bus_f: Optional[float]) -> None:
        f = self.faults
        req = self.start_request
        st = self.state
        # warnings that come and go with their cause
        for code, name in ((441, "low_battery"), (2678, "charger_fail"), (2725, "low_gas")):
            if name in f:
                if code not in self.active:
                    self._raise(code)
            else:
                self._clear(code)
        if self.switch == 0 and st != "stop":
            self.cb_closed = False
            st, self.timer = "stop", 0.0                       # Off: immediate stop, no cooldown
        if st == "stop":
            self.attempt = 0
            if req:
                st, self.timer = "delay", 0.0
                self.runs += 1
        elif st == "delay":
            self.timer += h
            if not req:
                st = "stop"
            elif self.timer >= START_DELAY_S:
                st, self.timer = "crank", 0.0
                self.attempt += 1
                self.starts += 1
                if "low_gas" in f:
                    self._raise(2569)                        # critical low gas: no start
                    st = "stop"
        elif st == "crank":
            self.timer += h
            self.crank_total += h
            if not req:
                st = "stop"
            elif "fail_to_crank" in f:
                if self.timer >= CRANK_S:
                    self._raise(1438)
                    st = "stop"
            else:
                fire_after = FIRE_AFTER_S * (2.5 if "weak_battery" in f else 1.0)
                fails = "fail_to_start" in f or ("weak_battery" in f and self.attempt == 1)
                if not fails and self.timer >= fire_after:
                    st, self.timer = "ramp", 0.0
                    if "weak_battery" in f:
                        self._raise(1442)
                elif self.timer >= CRANK_S:
                    if self.attempt >= ATTEMPTS:
                        self._raise(359)
                        st = "stop"
                    else:
                        st, self.timer = "rest", 0.0
        elif st == "rest":
            self.timer += h
            if not req:
                st = "stop"
            elif self.timer >= REST_S:
                st, self.timer = "crank", 0.0
                self.attempt += 1
                self.starts += 1
        elif st == "ramp":
            self.timer += h
            if self.timer >= RAMP_S:
                if "overspeed" in f:
                    self.rpm = 2150.0
                    self._raise(234)
                    st = "stop"
                else:
                    st, self.timer = "rated", 0.0
        elif st == "rated":
            if not req:
                st, self.timer = "cooldown", 0.0
                self.cb_closed = False
        elif st == "cooldown":
            self.timer += h
            self.cb_closed = False
            if req:
                st = "rated"
            elif self.timer >= COOLDOWN_S:
                st, self.timer = "stop", 0.0
        if self.shutdown:
            st = "stop"
            self.cb_closed = False
        self.state = st

        # ---- engine physics (shared core) ----
        phase = {"delay": "stop", "rest": "stop"}.get(st, st)
        self._core_engine(h, t_amb, bus_f, phase, self.timer / RAMP_S if st == "ramp" else 0.0)

    # ------------------------------------------------------------ outputs
    def seq_state(self) -> int:
        return {"stop": SEQ_STOP, "delay": SEQ_DELAY, "crank": SEQ_DELAY, "rest": SEQ_DELAY,
                "ramp": SEQ_WARMUP, "rated": SEQ_RATED, "cooldown": SEQ_COOLDOWN}[self.state]

    def fault_type(self) -> int:
        if self.shutdown:
            return FAULT_CODES.get(self.fault_code, ("", FT_SHUTDOWN, None))[1]
        return max(self.active.values()) if self.active else FT_NONE

    def nfpa110(self) -> int:
        a = self.active
        bits = {
            15: bool(self.shutdown or a),
            14: self.cb_closed and self.kw > 1.0,
            13: self.running,
            12: self.switch != 1,
            10: 441 in a or 1442 in a,
            9: 2678 in a,
            8: self.fault_code in (359, 1438),
            7: self.coolant_c < 21.0,
            6: 146 in a,
            5: self.fault_code == 151,
            4: 143 in a,
            3: self.fault_code == 415,
            2: self.fault_code == 234,
            0: 2725 in a or self.fault_code == 2569,
        }
        return sum(1 << b for b, on in bits.items() if on)

    def nfpa110_ext(self) -> int:
        a = self.active
        bits = {
            15: bool(self.shutdown),
            13: self.fault_code == 1446,
            12: self.fault_code == 1447,
            11: self.fault_code == 1448,
            10: 1444 in a,
            9: self.fault_code == 1472,
            5: 1457 in a,
            4: 1452 in a,
            3: self.load_demand_stopped,
            2: self.shutdown and self.fault_code in (1448, 1472, 1446, 1447),
        }
        return sum(1 << b for b, on in bits.items() if on)

    def status_text(self) -> str:
        if self.shutdown:
            return f"SHUTDOWN {self.fault_code} {FAULT_CODES.get(self.fault_code, ('?',))[0]}"
        txt = {"stop": "Stopped", "delay": "Time delay to start", "crank": f"Cranking (attempt {self.attempt})",
               "rest": f"Crank rest (attempt {self.attempt})", "ramp": "Accelerating", "rated": "Rated",
               "cooldown": f"Cooldown {max(0, COOLDOWN_S - self.timer):.0f} s"}[self.state]
        if self.state == "rated":
            txt += " · on bus" if self.cb_closed else " · breaker open"
        if self.load_demand_stopped and self.state == "stop":
            txt = "Stopped by load demand"
        return txt

    def registers(self, pf: float) -> Dict[int, int]:
        """PowerCommand 3.x register image (A029X159 section 13)."""
        r: Dict[int, int] = {}
        kw, kvar = self.kw if self.cb_closed else 0.0, self.kvar if self.cb_closed else 0.0
        kva = math.hypot(kw, kvar)
        v_ll = self.volts
        v_ln = v_ll / math.sqrt(3.0)
        amps = kva * 1000.0 / (math.sqrt(3.0) * v_ll) if v_ll > 50 else 0.0
        self.amps = amps
        r[40010] = self.switch
        r[40011] = self.seq_state()
        r[40012] = self.fault_code or self.recent
        r[40013] = self.fault_type()
        r[40014] = int(round(100.0 * 100.0 * kw / self.kw_rating)) & 0xFFFF
        r[40016] = self.nfpa110()
        r[40017] = self.nfpa110_ext()
        for i in range(3):
            r[40018 + i] = int(round(v_ln))
            r[40022 + i] = int(round(v_ll))
            r[40026 + i] = int(round(amps))
            r[40031 + i] = int(round(kw / 3.0)) & 0xFFFF
            r[40035 + i] = int(round(kvar / 3.0)) & 0xFFFF
            r[40040 + i] = int(round(kva / 3.0))
        r[40021], r[40025], r[40029] = int(round(v_ln)), int(round(v_ll)), int(round(amps))
        r[40030] = 0
        r[40034] = int(round(kw)) & 0xFFFF
        r[40038] = int(round(kvar)) & 0xFFFF
        r[40039] = int(round(100.0 * (pf if kva > 1 else 1.0))) & 0xFFFF
        r[40043] = int(round(kva))
        r[40044] = int(round(100.0 * self.freq))
        r[40061] = int(round(10.0 * self.batt_v))
        r[40062] = int(round(self.oil_kpa))
        r[40064] = int(round(10.0 * self.coolant_c)) & 0xFFFF
        r[40068] = int(round(self.rpm))
        r[40069] = self.starts & 0xFFFF
        rt = int(self.run_s * 10.0) & 0xFFFFFFFF
        r[40070], r[40071] = rt >> 16, rt & 0xFFFF
        r[40074], r[40075] = (self.runs >> 16) & 0xFFFF, self.runs & 0xFFFF
        r[40300] = 1 if self.remote_start else 0
        r[40301] = 0
        for reg in range(40010, 40076):
            r.setdefault(reg, 65535)            # unlisted in the block: "not available" (manual 13.2)
        for reg in BITMAP_REGS:
            r[reg] = 0
        codes = set(self.active)
        if self.fault_code:
            codes.add(self.fault_code)
        for code in codes:
            loc = FAULT_CODES.get(code, ("", 0, None))[2]
            if loc:
                r[loc[0]] |= 1 << loc[1]
        return r

    # ------------------------------------------------------------ interface
    @property
    def in_auto(self) -> bool:
        return self.switch == 1

    def modbus_registers(self, pf: float) -> Dict[int, int]:
        return self.registers(pf)

    def modbus_write(self, reg: int, value: int) -> bool:
        if reg == 40300:
            self.remote_start = 1 if value else 0
            return True
        if reg == 40301:
            if value:
                self.fault_reset()
            return True
        return False

    def bas_write(self, key: str, value: Any, seen: bool = True) -> None:
        reg = next(o[1] for o in PCC_OBJECTS if o[3] == key and o[5] == "cmd")
        self.modbus_write(reg, int(value))

    def panel(self, key: str) -> None:
        if key in ("off", "auto", "manual"):
            self.set_switch({"off": 0, "auto": 1, "manual": 2}[key])
        elif key == "reset":
            if not self.fault_reset():
                raise ValueError(f"{self.tag}: fault reset needs the control switch in Off or Auto")
        else:
            raise KeyError(key)

    def points(self) -> Dict[str, Any]:
        r = self.registers(1.0)
        out = {}
        for _k, reg, _n, key, _u, _r in PCC_OBJECTS:
            v = r.get(reg, 0)
            if key in ("run_time", "runs"):
                v = v * 65536 + r.get(reg + 1, 0)
            out[key] = v
        return out

    def display_code(self) -> int:
        return self.fault_code or (max(self.active) if self.active else 0)

    def switch_text(self) -> str:
        return SWITCH_TEXT[self.switch]

    @classmethod
    def decode(cls, role: str, regs: Dict[int, int]):
        def signed(v):
            return v - 65536 if v >= 32768 else v
        if role == "running":
            return bool(regs[40016] & (1 << 13))
        if role == "at_rated":
            return regs[40011] == SEQ_RATED
        if role == "kw":
            return float(signed(regs[40034]))
        if role == "hz":
            return regs[40044] / 100.0
        if role == "volts":
            return float(regs[40025])
        if role == "shutdown":
            return regs[40013] in (FT_SHUTDOWN, FT_SHUTDOWN_COOLDOWN)
        if role == "warning":
            return regs[40013] in (FT_WARNING, FT_DERATE)
        if role == "in_auto":
            return regs[40010] == 1
        if role == "fault_code":
            return int(regs[40012])
        if role in ("remote_start", "fault_reset"):
            return regs[cls.role_register(role)]
        raise KeyError(role)

    def generic_state(self) -> str:
        if self.shutdown:
            return "lockout"
        return {"stop": "off", "delay": "starting", "crank": "starting", "rest": "starting", "ramp": "starting",
                "rated": "running", "cooldown": "stopping"}[self.state]

    def codes(self) -> list:
        types = {FT_WARNING: "WARNING", FT_DERATE: "DERATE", FT_SHUTDOWN_COOLDOWN: "SHUTDOWN", FT_SHUTDOWN: "SHUTDOWN"}
        return [f"{types.get(t, '')} {c} {FAULT_CODES.get(c, ('?',))[0]}".strip() for c, t in sorted(self.active.items())]

    def view_fields(self) -> list:
        return [("Switch", SWITCH_TEXT.get(self.switch, "?")), ("rpm", f"{self.rpm:.0f}"),
                ("Coolant", f"{self.coolant_c * 9 / 5 + 32:.0f} °F"), ("Oil", f"{self.oil_kpa * 0.145038:.0f} psi"),
                ("Battery", f"{self.batt_v:.1f} V")]
