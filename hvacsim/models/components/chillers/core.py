"""
Shared chiller thermal core (docs/EQUIPMENT_INTERFACE_DESIGN.md, section 4).

Taken out of the YORK YK model with its numbers unchanged: capacity against
the water, the kW curve against part load and lift, the evaporator and
condenser heat balance and the refrigerant saturation temperatures.  Every
chiller uses it, so two vendors with the same ratings make the same tons and
kW on the same plant; they differ in their controls, codes and integration.

The vendor sets self.cap (fraction of design tons) from its own capacity
control, and says whether the compressor is running (self.running) and
whether it is coasting down after a stop (coasting=True).
"""

from __future__ import annotations

from typing import Any, Dict, Set


class ChillerCore:
    faults: Set[str]

    def _init_core(self, cfg: Dict[str, Any]) -> None:
        self.capacity = float(cfg.get("capacity_tons", 1200.0))
        self.chw_gpm_d = float(cfg.get("chw_design_gpm", 2400.0))
        self.cw_gpm_d = float(cfg.get("cw_design_gpm", 3600.0))
        self.sp_d = float(cfg.get("chws_design_temp", 44.0))
        self.kwpt = float(cfg.get("kw_per_ton", 0.57))
        self.min_load = float(cfg.get("min_load_pct", 10.0)) / 100.0
        self.kw_d = self.capacity * self.kwpt
        self.lift_d = (95.0 + 1.5) - (self.sp_d - 2.0)

    def _reset_core(self) -> None:
        self.cap = 0.0                           # fraction of design tons being produced
        self.q = 0.0                             # Btu/h removed from the chilled water
        self.kw = 0.0
        self.kwh = 0.0
        self.lchwt = 75.0
        self.echwt = 75.0
        self.lcwt = 75.0
        self.ecwt = 75.0
        self.evap_sat = 74.0
        self.cond_sat = 76.0
        self.evap_gpm = 0.0
        self.cond_gpm = 0.0

    @property
    def lift(self) -> float:
        return self.cond_sat - self.evap_sat

    def load_fraction(self) -> float:
        return self.q / (self.capacity * 12000.0)

    def _core_thermal(self, h: float, t_room: float, coasting: bool) -> float:
        """Heat balance, power and refrigerant temperatures for h seconds.  Returns the load fraction."""
        cap_q = self.cap * self.capacity * 12000.0 if self.running else 0.0
        # evaporator
        if self.evap_gpm > 1.0:
            lchwt_target = self.echwt - cap_q / (500.0 * self.evap_gpm)
            tau = max(10.0, 6000.0 / self.evap_gpm * 2.0)        # tube water volume
        else:
            lchwt_target = self.lchwt + (t_room - self.lchwt) * 0.05 - cap_q * 1e-6
            tau = 900.0
        self.lchwt += (lchwt_target - self.lchwt) * min(1.0, h / tau)
        self.q = cap_q if self.evap_gpm > 1.0 else 0.0
        plr = self.cap
        # power
        if self.running:
            lift_ratio = max(0.3, self.lift / self.lift_d)
            f_plr = 1.0 + 0.35 * (1.0 - plr) ** 2 - 0.15 * (1.0 - plr)
            fouling = 1.0
            self.kw = self.kw_d * plr * f_plr * lift_ratio ** 1.15 * fouling + 0.012 * self.kw_d
        else:
            self.kw = 0.0
        q_rej = self.q + self.kw * 3412.0
        # condenser
        if self.cond_gpm > 1.0:
            lcwt_target = self.ecwt + q_rej / (500.0 * self.cond_gpm)
            tau_c = max(10.0, 9000.0 / self.cond_gpm * 2.0)
        else:
            lcwt_target = self.lcwt + (t_room - self.lcwt) * 0.02 + q_rej / 2.0e6
            tau_c = 120.0 if q_rej > 0 else 900.0
        self.lcwt += (lcwt_target - self.lcwt) * min(1.0, h / tau_c)
        # refrigerant temperatures: approaches grow with load and with low water flow
        load = self.q / (self.capacity * 12000.0)
        flow_e = (self.chw_gpm_d / max(self.evap_gpm, 0.15 * self.chw_gpm_d)) ** 0.8
        flow_c = (self.cw_gpm_d / max(self.cond_gpm, 0.15 * self.cw_gpm_d)) ** 0.8
        app_e = (0.3 + 1.7 * load * flow_e) * (2.5 if "evap_fouled" in self.faults else 1.0)
        if "low_charge" in self.faults:
            app_e += 1.0 + 6.0 * load
        rej_load = q_rej / (self.capacity * 12000.0 * 1.15)
        app_c = (0.3 + 1.2 * rej_load * flow_c) * (3.5 if "cond_fouled" in self.faults else 1.0)
        if self.running:
            e_target = self.lchwt - app_e
            c_target = self.lcwt + app_c
            if self.cond_gpm < 1.0:
                c_target = self.cond_sat + q_rej / 1.5e5      # no condenser water: pressure climbs fast
            tau_r = 15.0
        else:
            mid = 0.5 * (self.lchwt + self.lcwt)
            e_target = mid - 0.5 if coasting else mid
            c_target = mid + 0.5 if coasting else mid
            tau_r = 300.0
        self.evap_sat += (e_target - self.evap_sat) * min(1.0, h / tau_r)
        self.cond_sat += (c_target - self.cond_sat) * min(1.0, h / tau_r)
        return load
