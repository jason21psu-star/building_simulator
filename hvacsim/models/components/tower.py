"""
Induced-draft counterflow cooling tower cell with its own basin.

Per cell (design review): inlet isolation valve to the distribution basin,
two-position bypass valve straight into the cell's basin (fails closed, i.e.
water over the fill), outlet isolation valve to the CW supply header,
equalizer line isolation valve, basin heater with low-level lockout,
mechanical makeup float valve, low/high level switches, basin temperature.

Thermal: effectiveness on the wet bulb, eps = 1 - exp(-NTU), with NTU from
the air/water flow ratio, calibrated to the design point.  Water spread
poorly over the fill below ~50 % of rated flow; above ~120 % the
distribution basin overflows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GAL_PER_FT3 = 7.48


@dataclass
class TowerCell:
    design_gpm: float = 3600.0
    range_f: float = 10.0
    approach_f: float = 7.0
    design_wb: float = 78.0
    basin_gal: float = 6500.0
    level_normal_in: float = 12.0
    level_low_in: float = 8.0
    level_high_in: float = 16.0
    level_overflow_in: float = 18.0
    heater_kw: float = 12.0
    # state
    basin_t: float = 70.0
    level_in: float = 12.0
    ice: float = 0.0                 # fill icing 0..1
    frozen: bool = False             # basin frozen solid around the outlet

    def __post_init__(self) -> None:
        t_in = self.design_wb + self.approach_f + self.range_f
        eps = self.range_f / (t_in - self.design_wb)
        self.ntu_d = -math.log(1.0 - eps)
        self.area_ft2 = self.basin_gal / GAL_PER_FT3 / (self.level_overflow_in / 12.0)
        self.t_out = self.basin_t
        self.q_fill = 0.0
        self.q_bypass = 0.0
        self.q_out = 0.0
        self.q_makeup = 0.0
        self.q_eq = 0.0
        self.evap_gpm = 0.0
        self.spill_gpm = 0.0
        self.heat_rejected = 0.0

    @property
    def gal_per_in(self) -> float:
        return self.area_ft2 / 12.0 * GAL_PER_FT3

    def leaving_water(self, t_in: float, gpm: float, fan_pu: float, wb: float, oat: float,
                      clogged: bool = False, fouled: bool = False) -> float:
        """Water temperature leaving the fill (before the basin)."""
        if gpm < 1.0:
            return t_in
        air = max(0.12, fan_pu)                       # natural draft with the fan off
        ratio = gpm / self.design_gpm
        wetting = min(1.0, ratio / 0.5) ** 0.7        # poor distribution at low flow
        ntu = self.ntu_d * (air / max(ratio, 0.05)) ** 0.6 * wetting
        if clogged:
            ntu *= 0.75
        if fouled:
            ntu *= 0.7
        ntu *= (1.0 - 0.7 * self.ice)
        eps = 1.0 - math.exp(-max(0.0, ntu))
        # below freezing the air still cools by sensible + evaporation down to the wet bulb
        return t_in - eps * (t_in - wb)

    def step(self, h: float, t_in: float, q_fill: float, q_bypass: float, q_out: float, q_eq: float,
             t_eq: float, fan_pu: float, wb: float, oat: float, heater_on: bool, makeup_ok: bool,
             clogged: bool = False, fouled: bool = False) -> None:
        """Advance h s.  Flows in gpm; q_eq > 0 flows into this basin from the equalizer."""
        overflow_limit = self.design_gpm * (1.0 if clogged else 1.2)
        self.spill_gpm = max(0.0, q_fill - overflow_limit)
        q_fill_eff = q_fill - self.spill_gpm
        self.t_out = self.leaving_water(t_in, q_fill_eff, fan_pu, wb, oat, clogged, fouled)
        self.q_fill, self.q_bypass, self.q_out, self.q_eq = q_fill, q_bypass, q_out, q_eq
        # evaporation ~ 1 % of flow per 10 °F of range
        self.evap_gpm = q_fill_eff * max(0.0, t_in - self.t_out) / 1000.0
        # mechanical float makeup valve
        if makeup_ok:
            open_frac = max(0.0, min(1.0, (self.level_normal_in - self.level_in) / 1.0))
            self.q_makeup = 150.0 * open_frac
        else:
            self.q_makeup = 0.0
        q_in = q_fill_eff + q_bypass + self.q_makeup + max(0.0, q_eq)
        q_leave = q_out + self.evap_gpm + max(0.0, -q_eq)
        self.level_in += (q_in - q_leave) * h / 60.0 / self.gal_per_in
        overflow = self.level_in > self.level_overflow_in
        self.level_in = max(0.0, min(self.level_overflow_in, self.level_in))
        # basin temperature (well mixed)
        vol = max(200.0, self.level_in * self.gal_per_in)
        heat_in = (q_fill_eff * self.t_out + q_bypass * t_in + self.q_makeup * 55.0
                   + max(0.0, q_eq) * t_eq) * h / 60.0
        flow_in = (q_fill_eff + q_bypass + self.q_makeup + max(0.0, q_eq)) * h / 60.0
        t = self.basin_t
        if flow_in > 0:
            t = (t * vol + heat_in) / (vol + flow_in)
        # losses to the outdoors when idle (surface + air through the louvers)
        ua = 0.0008 + 0.004 * max(0.12, fan_pu)
        t += (min(oat, wb + 2.0) - t) * min(1.0, ua * h / 60.0) if q_fill_eff < 1.0 else 0.0
        if heater_on and self.level_in > self.level_low_in - 2.0:
            t += self.heater_kw * 3412.0 / 3600.0 * h / (vol * 8.33)
        self.basin_t = t
        # freezing
        if self.basin_t <= 32.0 and q_fill_eff + q_bypass < 1.0:
            self.basin_t = 32.0
            self.frozen = True
        elif self.basin_t > 34.0:
            self.frozen = False
        # fill icing: freezing air, low water loading, fan pulling air
        loading = q_fill_eff / self.design_gpm
        if oat < 32.0 and q_fill_eff > 1.0 and (loading < 0.5 or self.t_out < 36.0):
            self.ice = min(1.0, self.ice + h / 3600.0 * (0.5 + fan_pu) * (32.0 - oat) / 20.0)
        elif oat > 34.0 or (q_fill_eff > 0.6 * self.design_gpm and self.t_out > 45.0):
            self.ice = max(0.0, self.ice - h / 1800.0)
        self.heat_rejected = max(0.0, q_fill_eff * 500.0 * (t_in - self.t_out))
        if overflow:
            pass

    def level_low(self) -> bool:
        return self.level_in < self.level_low_in

    def level_high(self) -> bool:
        return self.level_in > self.level_high_in
