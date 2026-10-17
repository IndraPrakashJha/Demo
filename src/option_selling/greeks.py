"""Black-76 pricing, implied volatility and delta.

Kite quotes carry no Greeks, so they are derived here. The forward is implied
from put-call parity near the money, which avoids needing a dividend yield or
a futures quote. Delta is reported as the spot-equivalent Black-Scholes delta
(N(d1) for calls, N(d1) - 1 for puts), the convention option-chain screens use.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

from .models import OptionType

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _d1(forward: float, strike: float, t: float, sigma: float) -> float:
    return (math.log(forward / strike) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))


def black76_price(
    forward: float, strike: float, t: float, sigma: float, r: float, option_type: OptionType
) -> float:
    df = math.exp(-r * t)
    if t <= 0 or sigma <= 0:
        intrinsic = forward - strike if option_type is OptionType.CE else strike - forward
        return df * max(intrinsic, 0.0)
    d1 = _d1(forward, strike, t, sigma)
    d2 = d1 - sigma * math.sqrt(t)
    if option_type is OptionType.CE:
        return df * (forward * norm_cdf(d1) - strike * norm_cdf(d2))
    return df * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1))


def implied_vol(
    price: float,
    forward: float,
    strike: float,
    t: float,
    r: float,
    option_type: OptionType,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-7,
    max_iter: int = 200,
) -> float | None:
    """Bisection implied volatility; ``None`` when the price is outside no-arbitrage bounds."""
    if price <= 0 or t <= 0 or forward <= 0 or strike <= 0:
        return None
    df = math.exp(-r * t)
    intrinsic = df * max(forward - strike if option_type is OptionType.CE else strike - forward, 0.0)
    upper = df * (forward if option_type is OptionType.CE else strike)
    if price <= intrinsic or price >= upper:
        return None
    f_lo = black76_price(forward, strike, t, lo, r, option_type) - price
    f_hi = black76_price(forward, strike, t, hi, r, option_type) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = black76_price(forward, strike, t, mid, r, option_type) - price
        if abs(f_mid) < tol or (hi - lo) < 1e-10:
            return mid
        if f_mid > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def delta(forward: float, strike: float, t: float, sigma: float, option_type: OptionType) -> float:
    d1 = _d1(forward, strike, t, sigma)
    n = norm_cdf(d1)
    return n if option_type is OptionType.CE else n - 1.0


def implied_forward(pairs: Iterable[tuple[float, float, float]], r: float, t: float) -> float | None:
    """Median parity-implied forward from (strike, call_mid, put_mid) triples."""
    growth = math.exp(r * t)
    values = [k + growth * (c - p) for k, c, p in pairs]
    return statistics.median(values) if values else None
