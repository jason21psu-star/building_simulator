"""
Moist-air psychrometrics in I-P units (ASHRAE Fundamentals formulation).

Temperatures in °F, pressures in psia, humidity ratio W in lb water / lb dry
air, enthalpy in Btu/lb dry air, RH in percent (0-100).

Used by the weather station now, and by the AHU (mixing, coils, humidifier)
later.
"""

from __future__ import annotations

import math

# Hyland-Wexler saturation pressure coefficients (ASHRAE, I-P, T in °R, p in psia)
_ICE = (-1.0214165e4, -4.8932428, -5.3765794e-3, 1.9202377e-7, 3.5575832e-10, -9.0344688e-14, 4.1635019)
_WATER = (-1.0440397e4, -1.1294650e1, -2.7022355e-2, 1.2890360e-5, -2.4780681e-9, 6.5459673)


def p_atm(elevation_ft: float = 0.0) -> float:
    """Standard atmospheric pressure (psia) at an elevation."""
    return 14.696 * (1.0 - 6.8754e-6 * elevation_ft) ** 5.2559


def p_ws(t_f: float) -> float:
    """Saturation vapour pressure (psia) over water (t >= 32 °F) or ice (t < 32 °F)."""
    T = t_f + 459.67
    if t_f < 32.0:
        c1, c2, c3, c4, c5, c6, c7 = _ICE
        return math.exp(c1 / T + c2 + c3 * T + c4 * T ** 2 + c5 * T ** 3 + c6 * T ** 4 + c7 * math.log(T))
    c8, c9, c10, c11, c12, c13 = _WATER
    return math.exp(c8 / T + c9 + c10 * T + c11 * T ** 2 + c12 * T ** 3 + c13 * math.log(T))


def w_from_pw(pw: float, p: float) -> float:
    return 0.621945 * pw / max(p - pw, 1e-9)


def pw_from_w(w: float, p: float) -> float:
    return p * w / (0.621945 + w)


def w_sat(t_f: float, p: float) -> float:
    return w_from_pw(p_ws(t_f), p)


def w_from_rh(t_f: float, rh: float, p: float) -> float:
    rh = max(0.0, min(100.0, rh))
    return w_from_pw(rh / 100.0 * p_ws(t_f), p)


def rh_from_w(t_f: float, w: float, p: float) -> float:
    return max(0.0, min(100.0, 100.0 * pw_from_w(max(w, 0.0), p) / p_ws(t_f)))


def enthalpy(t_f: float, w: float) -> float:
    """Moist air enthalpy, Btu/lb dry air."""
    return 0.240 * t_f + w * (1061.0 + 0.444 * t_f)


def t_from_h_w(h: float, w: float) -> float:
    """Dry bulb from enthalpy and humidity ratio."""
    return (h - 1061.0 * w) / (0.240 + 0.444 * w)


def dewpoint(t_f: float, w: float, p: float) -> float:
    """Dew point (°F) for humidity ratio w; frost point below 32 °F."""
    pw = pw_from_w(max(w, 1e-7), p)
    lo, hi = -100.0, max(t_f, -99.0) + 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if p_ws(mid) < pw:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _w_from_wetbulb(t_f: float, twb: float, p: float) -> float:
    ws = w_sat(twb, p)
    if twb >= 32.0:
        return ((1093.0 - 0.556 * twb) * ws - 0.240 * (t_f - twb)) / (1093.0 + 0.444 * t_f - twb)
    return ((1220.0 - 0.04 * twb) * ws - 0.240 * (t_f - twb)) / (1220.0 + 0.444 * t_f - 0.48 * twb)


def wetbulb(t_f: float, w: float, p: float) -> float:
    """Thermodynamic wet bulb (°F)."""
    lo, hi = dewpoint(t_f, w, p) - 1.0, t_f
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _w_from_wetbulb(t_f, mid, p) > w:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def grains(w: float) -> float:
    """Humidity ratio in grains per lb dry air."""
    return w * 7000.0


def state(t_f: float, rh: float, p: float) -> dict:
    """All properties for a dry bulb / RH pair."""
    w = w_from_rh(t_f, rh, p)
    return {
        "t": t_f,
        "rh": rh,
        "w": w,
        "grains": grains(w),
        "h": enthalpy(t_f, w),
        "dewpoint": dewpoint(t_f, w, p),
        "wetbulb": wetbulb(t_f, w, p),
    }
