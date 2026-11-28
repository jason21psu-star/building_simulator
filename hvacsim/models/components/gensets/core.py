"""
Shared generator set engine and alternator core (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 6).

Taken out of the Cummins PowerCommand 3.3 model with its numbers unchanged:
cranking and speed, coolant, oil pressure, battery, AC output, run hours.

The vendor runs its control's sequence and tells the core the engine phase
each step: "stop" (also start delay and crank rest), "crank", "ramp" (with
the fraction of the ramp done), "rated" or "cooldown".  Conditions the core
detects are raised as engine events, which the vendor maps to its own fault
codes:

    pre_high_coolant, high_coolant_shutdown, low_oil_warning, low_oil_shutdown,
    high_ac_volts, low_ac_volts, overcurrent
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

RATED_RPM = 1800.0

ENGINE_EVENTS = ("pre_high_coolant", "high_coolant_shutdown", "low_oil_warning", "low_oil_shutdown",
                 "high_ac_volts", "low_ac_volts", "overcurrent")


class GensetCore:
    faults: Set[str]
    cb_closed: bool

    def _init_core(self, cfg: Dict[str, Any]) -> None:
        self.kw_rating = float(cfg.get("kw_rating", 1000.0))
        self.volts_rated = float(cfg.get("volts", 480.0))
        self.rpm = 0.0
        self.coolant_c = 25.0
        self.oil_kpa = 0.0
        self.batt_v = 27.2
        self.run_s = 0.0
        self.kw = 0.0
        self.kvar = 0.0
        self.amps = 0.0
        self.freq = 0.0
        self.volts = 0.0
        self._low_oil_s = 0.0
        self._hc_s = 0.0

    @property
    def running(self) -> bool:
        return self.rpm > 600.0

    # the vendor maps engine events to its own codes
    def _event(self, name: str) -> None:
        raise NotImplementedError

    def _event_clear(self, name: str) -> None:
        raise NotImplementedError

    def _event_active(self, name: str) -> bool:
        raise NotImplementedError

    def _core_engine(self, h: float, t_amb: float, bus_f: Optional[float], phase: str, ramp_frac: float = 0.0) -> None:
        """Engine and alternator physics for h seconds in the given phase."""
        f = self.faults
        if phase == "crank":
            target = 0.0 if "fail_to_crank" in f else (120.0 if "weak_battery" in f else 180.0)
            self.rpm += (target - self.rpm) * min(1.0, h / 1.0)
        elif phase == "ramp":
            self.rpm = max(self.rpm, 600.0 + (RATED_RPM - 600.0) * min(1.0, ramp_frac))
        elif phase in ("rated", "cooldown"):
            if self.cb_closed and bus_f is not None:
                self.rpm = bus_f / 60.0 * RATED_RPM
            else:
                self.rpm += (RATED_RPM - self.rpm) * min(1.0, h / 1.0)
        else:
            self.rpm = max(0.0, self.rpm - h * 300.0)
        run = self.running
        if run:
            self.run_s += h
        # coolant (°C): block heater keeps it warm; running warms toward 85 °C, load adds a little
        load_pu = self.kw / self.kw_rating if self.kw_rating else 0.0
        tgt = (85.0 + 5.0 * load_pu) if run else max(t_amb, 38.0)
        if run and "high_coolant" in f:
            tgt = 112.0
        self.coolant_c += (tgt - self.coolant_c) * min(1.0, h / (240.0 if run else 3600.0))
        if run and self.coolant_c >= 101.7:
            if not self._event_active("pre_high_coolant"):
                self._event("pre_high_coolant")
            self._hc_s += h
            if self.coolant_c >= 104.4 and self._hc_s > 5.0:
                self._event("high_coolant_shutdown")
        else:
            self._hc_s = 0.0
            if self.coolant_c < 98.0:
                self._event_clear("pre_high_coolant")
        # oil pressure (kPa)
        oil_t = 400.0 * min(1.0, self.rpm / RATED_RPM) * (0.25 if "low_oil" in f else 1.0)
        self.oil_kpa += (oil_t - self.oil_kpa) * min(1.0, h / 1.5)
        if phase in ("rated", "cooldown") and self.oil_kpa < 140.0:
            self._low_oil_s += h
            if not self._event_active("low_oil_warning"):
                self._event("low_oil_warning")
            if self._low_oil_s > 5.0:
                self._event("low_oil_shutdown")
        else:
            self._low_oil_s = 0.0
            if self.oil_kpa > 200.0 or not run:
                self._event_clear("low_oil_warning")
        # battery (V): charger float 27.2, dips while cranking
        if "charger_fail" in f:
            self._rest_v = max(23.6, getattr(self, "_rest_v", 25.4) - h * 0.0005)   # no charger: slow drain
            float_v = self._rest_v
        else:
            self._rest_v = 25.4
            float_v = 27.2
        if "low_battery" in f:
            float_v = min(float_v, 23.0)
        if phase == "crank" and "fail_to_crank" not in f:
            dip = 15.5 if "weak_battery" in f else 20.5
            self.batt_v += (dip - self.batt_v) * min(1.0, h / 0.5)
        else:
            self.batt_v += (float_v - self.batt_v) * min(1.0, h / 5.0)
        # AC output
        if run and phase in ("ramp", "rated", "cooldown"):
            self.freq = self.rpm / RATED_RPM * 60.0
            v = self.volts_rated * min(1.0, self.rpm / RATED_RPM)
            if "high_ac_volts" in f and phase == "rated":
                v *= 1.12
            if "low_ac_volts" in f and phase == "rated":
                v *= 0.85
            self.volts = v
        else:
            self.freq = 0.0
            self.volts = 0.0
        if phase == "rated":
            if "high_ac_volts" in f:
                self._hold(h, "_hv", 10.0, "high_ac_volts")
            if "low_ac_volts" in f:
                self._hold(h, "_lv", 10.0, "low_ac_volts")
            if "high_current" in f and self.cb_closed and self.kw > 50.0:
                self._hold(h, "_hi", 5.0, "overcurrent")


    def _hold(self, h: float, attr: str, limit: float, event: str) -> None:
        t = getattr(self, attr, 0.0) + h
        setattr(self, attr, t)
        if t >= limit:
            setattr(self, attr, 0.0)
            self._event(event)
