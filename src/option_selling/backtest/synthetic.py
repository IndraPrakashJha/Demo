"""Synthetic NIFTY option data for exercising the backtest pipeline.

The output is NOT market data. A ``SYNTHETIC_DATA`` marker file is written so
reports built from it are labelled as synthetic.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..greeks import black76_price
from ..models import OptionType, round_to_tick
from .engine import SYNTHETIC_MARKER

SESSION_OPEN = time(9, 15)
SESSION_MINUTES = 375  # 09:15 to 15:30


def _tuesdays_after(d: date, horizon_days: int) -> list[date]:
    out = []
    for i in range(horizon_days + 1):
        x = d + timedelta(days=i)
        if x.weekday() == 1:
            out.append(x)
    return out


def generate(
    out_dir: Path,
    start: date,
    days: int = 5,
    seed: int = 7,
    spot: float = 25000.0,
    base_iv: float = 0.13,
    rate: float = 0.06,
    lot_size: int = 65,
    strike_step: int = 50,
    strike_range_pct: float = 0.08,
    bar_seconds: int = 60,
    spread_pct: float = 0.006,
) -> list[date]:
    rng = random.Random(seed)
    (out_dir / "options").mkdir(parents=True, exist_ok=True)
    (out_dir / "underlying").mkdir(parents=True, exist_ok=True)
    (out_dir / SYNTHETIC_MARKER).write_text("Synthetic data generated for pipeline tests. Not market data.\n")
    written = []
    d = start
    minute_vol = base_iv / math.sqrt(252 * SESSION_MINUTES)
    bars = SESSION_MINUTES * 60 // bar_seconds
    while len(written) < days:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        spot *= math.exp(rng.gauss(0, base_iv / math.sqrt(252) * 0.3))  # overnight gap
        expiries = [e for e in _tuesdays_after(d, 24) if 7 <= (e - d).days <= 17]
        centre = round(spot / strike_step) * strike_step
        half_range = int(spot * strike_range_pct / strike_step) * strike_step
        strikes = range(int(centre - half_range), int(centre + half_range) + 1, strike_step)
        day_iv = base_iv * math.exp(rng.gauss(0, 0.1))
        path = []
        for i in range(bars):
            ts = datetime.combine(d, SESSION_OPEN) + timedelta(seconds=i * bar_seconds)
            spot *= math.exp(rng.gauss(0, minute_vol * math.sqrt(bar_seconds / 60)))
            path.append((ts, spot))

        with open(out_dir / "underlying" / f"{d.isoformat()}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "price"])
            for ts, s in path:
                w.writerow([ts.isoformat(sep=" "), f"{s:.2f}"])

        with open(out_dir / "options" / f"{d.isoformat()}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "expiry", "strike", "option_type", "close", "bid", "ask", "lot_size"])
            for ts, s in path:
                for expiry in expiries:
                    t = (datetime.combine(expiry, time(15, 30)) - ts).total_seconds() / (365 * 24 * 3600)
                    forward = s * math.exp(rate * t)
                    for k in strikes:
                        m = math.log(k / forward)
                        iv = max(0.05, day_iv - 0.4 * m + 1.5 * m * m)
                        for ot in (OptionType.PE, OptionType.CE):
                            price = black76_price(forward, k, t, iv, rate, ot)
                            close = max(0.05, round_to_tick(price, 0.05, up=False))
                            half = max(0.05, close * spread_pct / 2)
                            bid = round_to_tick(close - half, 0.05, up=False)
                            ask = round_to_tick(close + half, 0.05, up=True)
                            w.writerow([ts.isoformat(sep=" "), expiry.isoformat(), k, ot.value, f"{close:.2f}",
                                        f"{bid:.2f}" if bid > 0 else "", f"{ask:.2f}", lot_size])
        written.append(d)
        d += timedelta(days=1)
    return written
