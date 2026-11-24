"""
Air-side building blocks for air handlers: actuators, valves, dampers,
water coils and a steam humidifier.

Units: °F, cfm, gpm, Btu/h, lb/lb (humidity ratio), in. w.c.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ... import psychro

AIR_LB_PER_CFM_H = 4.5      # lb dry air per hour per cfm (0.075 lb/ft3 x 60)
SENSIBLE = 1.08             # Btu/h per cfm per °F


def ssqrt(x: float) -> float:
    """Signed square root: flow through an orifice for a signed pressure difference."""
    return math.copysign(math.sqrt(abs(x)), x)


def ssq(x: float) -> float:
    """Signed square: pressure difference for a signed flow."""
    return math.copysign(x * x, x)


def signal_2_10(pct_of_10v: float) -> float:
    """2-10 V signal written as % of 10 V -> commanded position 0..1 (below 2 V = 0)."""
    volts = max(0.0, min(10.0, pct_of_10v / 10.0))
    return max(0.0, min(1.0, (volts - 2.0) / 8.0))


@dataclass
class Actuator:
    """Spring-return modulating actuator (2-10 V)."""

    stroke_s: float = 90.0
    spring_pos: float = 0.0       # position the spring returns to on loss of power (0..1)
    pos: float = 0.0

    def step(self, h: float, cmd: float, powered: bool = True, stuck_at: float | None = None) -> float:
        if stuck_at is not None:
            self.pos = max(0.0, min(1.0, stuck_at))
            return self.pos
        target = cmd if powered else self.spring_pos
        speed = h / self.stroke_s if powered else h / 20.0       # spring return is fast (~20 s)
        if target > self.pos:
            self.pos = min(target, self.pos + speed)
        else:
            self.pos = max(target, self.pos - speed)
        return self.pos


def equal_percentage(pos: float, rangeability: float = 50.0, authority: float = 0.5) -> float:
    """Installed flow fraction of an equal-percentage 2-way valve."""
    if pos <= 0.0:
        return 0.0
    f = rangeability ** (pos - 1.0)
    return 1.0 / math.sqrt((1.0 - authority) + authority / (f * f))


def damper_fraction(pos: float, leak: float = 0.02, exponent: float = 1.6) -> float:
    """Opposed-blade damper conductance fraction (leakage when closed)."""
    pos = max(0.0, min(1.0, pos))
    return leak + (1.0 - leak) * pos ** exponent


class WaterCoil:
    """
    Water coil (heating or cooling) with a lumped water/metal node.

    The coil surface is taken as the average of entering and leaving water;
    leaving air approaches the surface by a contact factor 1 - exp(-NTU).
    Cooling coils also dehumidify when the surface is below the entering
    dew point (apparatus dew point method).  The node lets the coil cool
    down (and freeze) when water stops flowing.
    """

    def __init__(self, *, cfm_d: float, gpm_d: float, ewt_d: float, eat_d: float, lat_d: float,
                 ew_d: float = 0.0, capacitance: float = 300.0, cooling: bool = False):
        self.cfm_d, self.gpm_d, self.cooling = cfm_d, gpm_d, cooling
        self.C = capacitance                     # Btu/°F (water + copper + fins)
        self.ntu_d = self._calibrate(ewt_d, eat_d, lat_d, ew_d)
        self.lwt = eat_d                         # leaving water / node temperature
        self.freeze_s = 0.0
        self.frozen = False                      # ice blocking the tubes
        self.damaged = False                     # burst tubes after a freeze
        self.q = 0.0
        self.flow_gpm = 0.0

    def _calibrate(self, ewt: float, eat: float, lat: float, ew: float) -> float:
        p = psychro.p_atm(0)
        m_air = AIR_LB_PER_CFM_H * self.cfm_d
        mcp = 500.0 * self.gpm_d
        cf = 0.5
        for _ in range(50):
            if self.cooling:
                w_in = ew or psychro.w_from_rh(eat, 50.0, p)
                h_in = psychro.enthalpy(eat, w_in)
                # leaving air on the line toward the surface
                q = m_air * (h_in - psychro.enthalpy(lat, min(w_in, psychro.w_sat(lat, p))))
                lwt = ewt + q / mcp
            else:
                q = SENSIBLE * self.cfm_d * (lat - eat)
                lwt = ewt - q / mcp
            surf = (ewt + lwt) / 2.0
            new_cf = (eat - lat) / (eat - surf) if self.cooling else (lat - eat) / (surf - eat)
            cf = max(0.02, min(0.97, new_cf))
        return -math.log(1.0 - cf)

    def step(self, h: float, cfm: float, t_in: float, w_in: float, ewt: float, gpm: float,
             p_atm: float) -> tuple:
        """Advance h seconds; return leaving air (t, w)."""
        if self.frozen:
            gpm = 0.0                            # ice blocks the tubes
        if self.damaged:
            gpm *= 0.5                           # burst tubes leak and bypass
        self.flow_gpm = gpm
        mcp = 500.0 * gpm
        surf = (ewt + self.lwt) / 2.0 if gpm > 0.01 else self.lwt
        if cfm > 1.0:
            ntu = self.ntu_d * (cfm / self.cfm_d) ** -0.2 * max(0.35, (gpm / self.gpm_d) ** 0.25 if gpm > 0 else 0.35)
            if self.damaged:
                ntu *= 0.6
            cf = 1.0 - math.exp(-ntu)
            t_out = t_in + cf * (surf - t_in)
            w_out = w_in
            if self.cooling and w_in > psychro.w_sat(surf, p_atm):
                w_out = w_in - cf * (w_in - psychro.w_sat(surf, p_atm))
            m_air = AIR_LB_PER_CFM_H * cfm
            q_air = m_air * (psychro.enthalpy(t_out, w_out) - psychro.enthalpy(t_in, w_in))   # + = air heated
        else:
            # no airflow: still-air convection to the air in the unit
            t_out, w_out = t_in, w_in
            q_air = 15.0 * (surf - t_in)
        self.q = q_air
        # node: water in minus heat given to the air
        dT = (mcp * (ewt - self.lwt) - q_air) / self.C
        self.lwt += dT * h / 3600.0
        # freezing: water below 32 °F for a while turns to ice; thawing a frozen coil bursts it
        if self.lwt < 32.0:
            self.freeze_s += h
            if self.freeze_s > 120.0:
                self.frozen = True
        else:
            self.freeze_s = max(0.0, self.freeze_s - h)
            if self.frozen and self.lwt > 36.0:
                self.frozen = False
                self.damaged = True
        return t_out, w_out

    def repair(self) -> None:
        self.frozen = False
        self.damaged = False
        self.freeze_s = 0.0


class SteamHumidifier:
    """Steam dispersion grid fed by a modulating steam valve."""

    def __init__(self, capacity_lb_h: float = 250.0):
        self.capacity = capacity_lb_h
        self.steam = 0.0          # lb/h being injected
        self.wetting = False      # duct wetting (air saturated)

    def step(self, cfm: float, t_in: float, w_in: float, valve_pos: float, steam_available: bool,
             p_atm: float) -> tuple:
        self.steam = self.capacity * valve_pos if steam_available else 0.0
        self.wetting = False
        if cfm < 50.0:
            return t_in, w_in
        m_air = AIR_LB_PER_CFM_H * cfm
        dw = self.steam / m_air
        h_out = psychro.enthalpy(t_in, w_in) + dw * 1150.0
        w_out = w_in + dw
        t_out = psychro.t_from_h_w(h_out, w_out)
        w_sat = psychro.w_sat(t_out, p_atm)
        if w_out > w_sat:
            # can't absorb it all: the rest condenses in the duct
            self.wetting = True
            w_out = w_sat
        return t_out, w_out
