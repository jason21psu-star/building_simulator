"""
Water-side building blocks for central plants: pumps, two-position isolation
valves, parallel pump/branch solves and a stratified thermal storage tank.

Units: gpm, ft of head (psi = ft / 2.307), °F, gallons.
Pressure loss of a branch = R * Q^2 (R in ft/gpm^2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

FT_PER_PSI = 2.307


def bisect(f: Callable[[float], float], lo: float, hi: float, iters: int = 48) -> float:
    """Root of a function that decreases from f(lo) >= 0 to f(hi) <= 0 (clamped to the bracket)."""
    flo = f(lo)
    if flo <= 0:
        return lo
    if f(hi) >= 0:
        return hi
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def root(f: Callable[[float], float], lo: float, hi: float, xtol: float = 1e-3, maxit: int = 60) -> float:
    """
    Root of a decreasing function on [lo, hi] (Illinois false position: bracketed,
    superlinear).  Clamps to the bracket when there is no sign change.
    """
    flo, fhi = f(lo), f(hi)
    if flo <= 0:
        return lo
    if fhi >= 0:
        return hi
    side = 0
    x = lo
    for _ in range(maxit):
        x = (lo * fhi - hi * flo) / (fhi - flo)
        fx = f(x)
        if abs(fx) < 1e-9 or hi - lo < xtol:
            return x
        if fx > 0:
            lo, flo = x, fx
            if side == 1:
                fhi *= 0.5
            side = 1
        else:
            hi, fhi = x, fx
            if side == -1:
                flo *= 0.5
            side = -1
    return x


# ---------------------------------------------------------------------------
# Pumps
# ---------------------------------------------------------------------------


@dataclass
class Pump:
    """
    Centrifugal pump on a VFD.  Rated point (design_gpm, design_head) at full speed;
    shutoff head 125 % of rated.  H = Hs*s^2 - k*Q^2, plus the pump's own branch
    piping (suction diffuser, check valve, triple duty valve) as branch_r*Q^2.
    """

    design_gpm: float
    design_head: float
    motor_hp: float
    branch_r: float = 0.0
    wear: float = 1.0               # 1 = new impeller; worn impeller < 1 (head)
    speed: float = 0.0              # per unit, from the drive
    flow: float = 0.0               # last solved flow, gpm
    head: float = 0.0               # last solved pump head, ft
    cavitation: float = 0.0         # 0..1 suction starvation

    @property
    def shutoff(self) -> float:
        return 1.25 * self.design_head * self.wear

    @property
    def k(self) -> float:
        return 0.25 * self.design_head / (self.design_gpm ** 2)

    def flow_at(self, header_head: float) -> float:
        """Flow this pump pushes against a header-to-header head (check valve: never negative)."""
        avail = self.shutoff * self.speed * self.speed - header_head
        if avail <= 0 or self.speed < 0.05:
            return 0.0
        return math.sqrt(avail / (self.k + self.branch_r))

    def pump_head(self, q: float) -> float:
        return max(0.0, self.shutoff * self.speed * self.speed - self.k * q * q)

    def shaft_hp(self) -> float:
        """Brake horsepower at the last solved point (losses keep a floor at shutoff)."""
        s = self.speed
        hydraulic = self.flow * self.head / (3960.0 * 0.82)
        rated = self.design_gpm * self.design_head / (3960.0 * 0.82)
        return hydraulic + 0.10 * rated * s ** 3

    def load_torque_pu(self, s: float) -> float:
        """Load torque (per unit of motor rated torque) at shaft speed s, for the drive."""
        if s <= 0.02:
            return 0.02
        ratio = s / max(self.speed, 0.05) if self.speed > 0.05 else 1.0
        hp = self.shaft_hp() * ratio ** 3 * (1.0 - 0.6 * self.cavitation)
        return 0.02 + hp / self.motor_hp / max(s, 0.05)


def pumps_flow(pumps: Sequence[Pump], header_head: float) -> float:
    return sum(p.flow_at(header_head) for p in pumps)


def max_head(pumps: Sequence[Pump]) -> float:
    return max([p.shutoff * p.speed * p.speed for p in pumps] + [0.0])


def parallel_r(rs: Iterable[float]) -> float:
    """Equivalent R of parallel branches (R=inf branches are closed)."""
    c = sum(1.0 / math.sqrt(r) for r in rs if r < 1e12 and r > 0)
    return 1.0 / (c * c) if c > 0 else float("inf")


def solve_pumps(pumps: Sequence[Pump], offset: float, r_sys: float) -> Tuple[float, float]:
    """
    Operating point of parallel pumps against a quadratic system: header head
    H = offset + r_sys * Q^2.  Returns (total flow, header head).
    """
    running = [(p.shutoff * p.speed * p.speed, p.k + p.branch_r) for p in pumps if p.speed >= 0.05]
    hmax = max([a for a, _b in running] + [0.0])
    if hmax <= 0 or r_sys >= 1e12:
        h, q = hmax, 0.0
    else:
        sq = math.sqrt

        def f(H: float) -> float:
            qp = 0.0
            for a, b in running:
                if a > H:
                    qp += sq((a - H) / b)
            return qp - (sq((H - offset) / r_sys) if H > offset else 0.0)

        h = root(f, min(0.0, offset), hmax, xtol=1e-4)
        q = pumps_flow(pumps, h)
    for p in pumps:
        p.flow = p.flow_at(h)
        p.head = p.pump_head(p.flow) if p.flow > 0 else p.shutoff * p.speed * p.speed
    return q, h


# ---------------------------------------------------------------------------
# Two-position isolation valve with end switches
# ---------------------------------------------------------------------------


def butterfly(pos: float) -> float:
    """Installed flow fraction of a butterfly valve: most of the change in the first third of travel."""
    pos = max(0.0, min(1.0, pos))
    return 1.0 - (1.0 - pos) ** 3


@dataclass
class IsoValve:
    """Motorized two-position valve: BO command, open and closed end switches."""

    stroke_s: float = 60.0
    fail_closed: bool = False       # spring return closed on loss of power (else fail in place)
    pos: float = 0.0                # 0 closed .. 1 open
    leak: float = 0.002             # flow fraction through a closed valve

    def step(self, h: float, cmd_open: bool, powered: bool = True, failed: bool = False,
             stuck_at: Optional[float] = None) -> float:
        if stuck_at is not None:
            self.pos = max(0.0, min(1.0, stuck_at))
            return self.pos
        if not powered:
            if self.fail_closed:
                self.pos = max(0.0, self.pos - h / 20.0)
            return self.pos
        if failed:
            return self.pos
        target = 1.0 if cmd_open else 0.0
        rate = h / self.stroke_s
        self.pos = min(target, self.pos + rate) if target > self.pos else max(target, self.pos - rate)
        return self.pos

    def fraction(self, seat_leak: bool = False) -> float:
        leak = 0.05 if seat_leak else self.leak
        return max(leak, butterfly(self.pos))

    def open_switch(self, misadjusted: bool = False) -> bool:
        return self.pos >= (0.80 if misadjusted else 0.98)

    def closed_switch(self) -> bool:
        return self.pos <= 0.02


def valve_r(r_open: float, fraction: float) -> float:
    return r_open / max(fraction, 1e-6) ** 2


# ---------------------------------------------------------------------------
# Stratified thermal storage tank
# ---------------------------------------------------------------------------


@dataclass
class StratifiedTank:
    """
    Vertical node model of a closed stratified chilled water tank.  Charging flow
    (positive) enters the bottom diffuser cold and pushes warm water out of the top;
    discharging flow enters the top warm and leaves the bottom.  Upwind advection
    plus a little inter-node mixing gives a thermocline that moves and blurs.
    """

    volume_gal: float
    height_ft: float
    nodes: int = 40
    t_init_cold: float = 44.0
    t_init_warm: float = 56.0
    charge: float = 0.8              # initial fraction charged (cold from the bottom up)
    mixing: float = 2.0e-4           # inter-node exchange, 1/s
    t: List[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.t:
            n_cold = int(round(self.nodes * self.charge))
            self.t = [self.t_init_cold if i < n_cold else self.t_init_warm for i in range(self.nodes)]

    @property
    def node_gal(self) -> float:
        return self.volume_gal / self.nodes

    def step(self, h: float, q_gpm: float, t_in: float, t_amb: float = 75.0) -> float:
        """Advance h s with flow q (+ charge: t_in enters at the bottom). Returns the outlet temperature."""
        t = self.t
        n = self.nodes
        moved = abs(q_gpm) * h / 60.0 / self.node_gal          # node volumes displaced
        sub = max(1, int(math.ceil(moved / 0.5)))
        frac = moved / sub
        for _ in range(sub):
            if q_gpm > 0:          # upward: bottom (0) is the inlet
                for i in range(n - 1, 0, -1):
                    t[i] += frac * (t[i - 1] - t[i])
                t[0] += frac * (t_in - t[0])
            elif q_gpm < 0:        # downward: top (n-1) is the inlet
                for i in range(n - 1):
                    t[i] += frac * (t[i + 1] - t[i])
                t[n - 1] += frac * (t_in - t[n - 1])
        # mixing / conduction and buoyancy (a colder node above a warmer one overturns)
        m = min(0.25, self.mixing * h)
        new = t[:]
        for i in range(n):
            lo = t[i - 1] if i > 0 else t[i]
            hi = t[i + 1] if i < n - 1 else t[i]
            new[i] = t[i] + m * (lo + hi - 2 * t[i])
        for i in range(n - 1):
            if new[i] > new[i + 1] + 0.05:
                a = 0.5 * (new[i] + new[i + 1])
                new[i] = new[i + 1] = a
        # shell gain from the surroundings (insulated)
        g = min(1.0, h / (14 * 86400.0))
        self.t = [x + g * (t_amb - x) for x in new]
        if q_gpm > 0:
            return self.t[-1]
        if q_gpm < 0:
            return self.t[0]
        return self.t[0]

    @property
    def top(self) -> float:
        return self.t[-1]

    @property
    def bottom(self) -> float:
        return self.t[0]

    def sensor(self, k: int, count: int) -> float:
        """Temperature at sensor k (0 = top) of `count` evenly spaced sensors."""
        z = 1.0 - (k + 0.5) / count                 # height fraction from the bottom
        x = z * self.nodes - 0.5
        i = max(0, min(self.nodes - 2, int(math.floor(x))))
        f = max(0.0, min(1.0, x - i))
        return self.t[i] * (1 - f) + self.t[i + 1] * f

    def charge_fraction(self, t_cold: float = 44.0, t_warm: float = 56.0) -> float:
        span = max(0.1, t_warm - t_cold)
        return sum(max(0.0, min(1.0, (t_warm - x) / span)) for x in self.t) / self.nodes

    def ton_hours(self, t_warm: float = 56.0) -> float:
        """Cooling stored relative to t_warm return water."""
        return sum(max(0.0, t_warm - x) for x in self.t) * self.node_gal * 8.33 / 12000.0


# ---------------------------------------------------------------------------
# Refrigerant (R-134a saturation, two-constant fit, ±1 psi from -20 to 130 °F)
# ---------------------------------------------------------------------------

_A, _B = 13.496, 4792.0


def r134a_psig(t_sat_f: float) -> float:
    return math.exp(_A - _B / (t_sat_f + 459.67)) - 14.696


def r134a_tsat(psig: float) -> float:
    psia = max(1.0, psig + 14.696)
    return _B / (_A - math.log(psia)) - 459.67
