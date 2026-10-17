"""Historical minute data for backtests.

Directory layout (all timestamps IST, one file per trading day)::

    <data_dir>/underlying/YYYY-MM-DD.csv   timestamp,price
    <data_dir>/options/YYYY-MM-DD.csv      timestamp,expiry,strike,option_type,close[,bid,ask,lot_size]

``bar_timestamp = "start"`` (the default) means a row stamped 10:00 is the bar
covering 10:00-10:01, so its close only becomes usable at 10:01. This prevents
look-ahead. Without bid/ask columns a spread of ``synthetic_spread_pct`` is
built around the close.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import BacktestConfig
from ..models import MarginResult, OptionContract, OptionType, OrderRequest, Quote, Side, round_to_tick
from ..timeutil import Clock, as_ist


@dataclass
class _Series:
    times: list[datetime] = field(default_factory=list)  # when each bar becomes usable
    close: list[float] = field(default_factory=list)
    bid: list[float | None] = field(default_factory=list)
    ask: list[float | None] = field(default_factory=list)

    def sort(self) -> None:
        order = sorted(range(len(self.times)), key=self.times.__getitem__)
        self.times = [self.times[i] for i in order]
        self.close = [self.close[i] for i in order]
        self.bid = [self.bid[i] for i in order]
        self.ask = [self.ask[i] for i in order]

    def at(self, now: datetime, max_age: timedelta) -> int | None:
        i = bisect_right(self.times, now) - 1
        if i < 0 or now - self.times[i] > max_age:
            return None
        return i


def _float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)


def available_days(data_dir: Path, start: date, end: date) -> list[date]:
    days = []
    for path in sorted((data_dir / "options").glob("*.csv")):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if start <= d <= end and (data_dir / "underlying" / path.name).exists():
            days.append(d)
    return days


@dataclass
class HistoricalDay:
    trade_date: date
    underlying: _Series
    options: dict[tuple[date, float, OptionType], _Series]
    lot_sizes: dict[date, int]

    @classmethod
    def load(cls, data_dir: Path, d: date, config: BacktestConfig) -> HistoricalDay:
        shift = timedelta(seconds=config.bar_seconds if config.bar_timestamp == "start" else 0)
        underlying = _Series()
        with open(data_dir / "underlying" / f"{d.isoformat()}.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                underlying.times.append(as_ist(datetime.fromisoformat(row["timestamp"])) + shift)
                underlying.close.append(float(row["price"]))
                underlying.bid.append(None)
                underlying.ask.append(None)
        underlying.sort()

        options: dict[tuple[date, float, OptionType], _Series] = {}
        lot_sizes: dict[date, int] = {}
        with open(data_dir / "options" / f"{d.isoformat()}.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                expiry = date.fromisoformat(row["expiry"][:10])
                key = (expiry, float(row["strike"]), OptionType(row["option_type"].strip().upper()))
                series = options.setdefault(key, _Series())
                series.times.append(as_ist(datetime.fromisoformat(row["timestamp"])) + shift)
                series.close.append(float(row["close"]))
                series.bid.append(_float(row.get("bid")))
                series.ask.append(_float(row.get("ask")))
                if row.get("lot_size"):
                    lot_sizes[expiry] = int(float(row["lot_size"]))
        for series in options.values():
            series.sort()
        return cls(d, underlying, options, lot_sizes)


class HistoricalMarketData:
    """MarketData over one ``HistoricalDay``, read at the simulated clock's time."""

    def __init__(self, day: HistoricalDay, clock: Clock, config: BacktestConfig,
                 underlying: str = "NIFTY", exchange: str = "NFO") -> None:
        self.day = day
        self.clock = clock
        self.config = config
        self.max_age = timedelta(seconds=config.max_bar_age_seconds)
        self._series: dict[str, tuple[OptionContract, _Series]] = {}
        for (expiry, strike, option_type), series in day.options.items():
            if expiry < day.trade_date:
                continue
            contract = OptionContract(
                tradingsymbol=f"{underlying}{expiry:%y%m%d}{strike:g}{option_type.value}",
                exchange=exchange,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                lot_size=day.lot_sizes.get(expiry, config.lot_size),
                tick_size=config.tick_size,
            )
            self._series[contract.key] = (contract, series)

    def option_contracts(self, underlying: str) -> list[OptionContract]:
        return [c for c, _ in self._series.values() if c.underlying == underlying]

    def quotes(self, contracts: Sequence[OptionContract]) -> dict[str, Quote]:
        now = self.clock.now()
        out = {}
        for contract in contracts:
            entry = self._series.get(contract.key)
            if entry is None:
                continue
            series = entry[1]
            i = series.at(now, self.max_age)
            if i is None:
                continue
            close = series.close[i]
            bid, ask = series.bid[i], series.ask[i]
            if bid is None or ask is None:
                bid, ask = self._synthetic_spread(close)
            # Freshness is enforced above via max_bar_age_seconds.
            out[contract.key] = Quote(bid=bid, ask=ask, last=close, timestamp=now)
        return out

    def underlying_quote(self) -> Quote | None:
        now = self.clock.now()
        i = self.day.underlying.at(now, self.max_age)
        if i is None:
            return None
        return Quote(bid=None, ask=None, last=self.day.underlying.close[i], timestamp=now)

    def _synthetic_spread(self, close: float) -> tuple[float | None, float | None]:
        tick = self.config.tick_size
        half = close * self.config.synthetic_spread_pct / 2
        bid = round_to_tick(close - half, tick, up=False)
        ask = round_to_tick(close + half, tick, up=True)
        if ask - bid < tick:
            ask = round(bid + tick, 2)
        return (bid if bid > 0 else None), ask


class BacktestAccount:
    """Fixed capital and an approximate iron-condor margin (see BacktestConfig)."""

    def __init__(self, config: BacktestConfig, market: HistoricalMarketData) -> None:
        self.config = config
        self.market = market

    def available_funds(self) -> float:
        return self.config.capital

    def basket_margin(self, orders: Sequence[OrderRequest]) -> MarginResult:
        legs = {(o.contract.option_type, o.side): o for o in orders}
        try:
            lp, sp = legs[(OptionType.PE, Side.BUY)], legs[(OptionType.PE, Side.SELL)]
            lc, sc = legs[(OptionType.CE, Side.BUY)], legs[(OptionType.CE, Side.SELL)]
        except KeyError as exc:
            raise ValueError("approximate margin supports complete iron condors only") from exc
        quantity = max(o.quantity for o in orders)
        wing = max(sp.contract.strike - lp.contract.strike, lc.contract.strike - sc.contract.strike)
        underlying = self.market.underlying_quote()
        spot = underlying.last if underlying else 0.0
        total = wing * quantity * self.config.margin_wing_factor + self.config.margin_exposure_pct * spot * quantity
        return MarginResult(total=total, raw={"model": "approximate", "wing": wing, "quantity": quantity})
