"""
Shared boiler heat exchanger core (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 5).

Taken out of the Lochinvar Crest model with its numbers unchanged:
- the heat exchanger water and metal mass, as an exact exponential update;
- efficiency against return water temperature and firing rate (condensing
  or not, set by the vendor);
- flue temperature and condensate;
- the no-flow hot spot at the high limit well.

The vendor runs its burner sequence and sets self.rate (0..1) and
self.flame; the core turns them into heat to the water.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Set


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class BoilerCore:
    faults: Set[str]
    CONDENSING = True
    HOT_SPOT_F = 35.0                    # °F above the water at the high limit well with no flow

    def _init_core(self, cfg: Dict[str, Any], t_init: float) -> None:
        self.input_mbh = float(cfg.get("input_mbh", 4000.0))
        self.design_gpm = float(cfg.get("design_gpm", 400.0))
        self.min_flow = float(cfg.get("min_flow_gpm", 100.0))
        self.turndown = float(cfg.get("turndown", 25.0))
        self.min_rate = 1.0 / max(1.0, self.turndown)
        self.rate = 0.0                  # actual firing rate 0..1
        self.flame = False
        self.t_hx = t_init               # water in the heat exchanger (= supply temp)
        self.t_ret = t_init
        self.gpm = 0.0
        self.q = 0.0                     # heat to water, Btu/h
        self.eff = 0.0
        self.flue = t_init
        self.condensate = 0.0
        self.hot_spot = 0.0              # °F above the water temp at the high limit well (no/low flow)

    @property
    def gas_mbh(self) -> float:
        return self.rate * self.input_mbh if self.flame else 0.0

    def _core_hot_spot(self, h: float) -> float:
        """Firing with little or no flow makes local boiling at the high limit well.  Returns its reading."""
        starve = max(0.0, 1.0 - self.gpm / max(1.0, self.min_flow))
        target = self.HOT_SPOT_F * starve if self.flame else 0.0
        self.hot_spot += (target - self.hot_spot) * min(1.0, h / 20.0)
        return self.t_hx + self.hot_spot

    def _efficiency(self, rwt: float) -> float:
        if self.CONDENSING:
            base = 0.87 + 0.09 * _clamp((135.0 - rwt) / 55.0, 0.0, 1.0)
        else:
            base = 0.82
        base += 0.01 * (1.0 - self.rate)
        if "hx_scaled" in self.faults:
            base -= 0.05
        return _clamp(base, 0.70, 0.985)

    def _core_hx(self, h: float, ewt: float, t_room: float) -> None:
        """Heat exchanger energy balance, flue temperature and condensate for h seconds."""
        # heat exchanger energy balance (water + metal ~ 3,000 Btu/°F per 4,000 MBH)
        cap = 3000.0 * self.input_mbh / 4000.0
        q_in = self.rate * self.input_mbh * 1000.0 if self.flame else 0.0
        self.eff = self._efficiency(ewt) if self.flame else 0.0
        self.q = q_in * self.eff
        m_cp = 500.0 * self.gpm
        loss = 300.0 * (self.t_hx - t_room)
        if m_cp > 0:
            # exact exponential update toward the steady leaving temperature
            t_ss = ewt + (self.q - loss) / m_cp
            self.t_hx = t_ss + (self.t_hx - t_ss) * math.exp(-m_cp * h / 3600.0 / cap)
        else:
            self.t_hx += (self.q - loss) * h / 3600.0 / cap
        # flue and condensate
        scale = 60.0 if "hx_scaled" in self.faults else 0.0
        target_flue = (ewt + 12.0 + 35.0 * self.rate + scale) if self.flame else max(t_room, self.t_hx - 20.0)
        self.flue += (target_flue - self.flue) * min(1.0, h / 20.0)
        dew = 130.0
        self.condensate = ((0.006 * self.input_mbh * self.rate * _clamp((dew - ewt) / 50.0, 0.0, 1.0))
                           if self.flame and self.CONDENSING else 0.0)
