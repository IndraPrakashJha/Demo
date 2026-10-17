"""Test doubles: a controllable market/account and Black-76 option chains."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, time

from option_selling.alerts import MemoryAlerter
from option_selling.brokers.simulated import SimulatedOrderGateway
from option_selling.config import ExecutionConfig, StrategyConfig
from option_selling.costs import ZerodhaOptionsCostModel
from option_selling.execution import BasketExecutor
from option_selling.greeks import black76_price
from option_selling.journal import Journal
from option_selling.models import MarginResult, OptionContract, OptionType, OrderRequest, Quote, round_to_tick
from option_selling.session import TradingSession
from option_selling.state import DayStateStore
from option_selling.strategies.iron_condor_v1 import IronCondorV1
from option_selling.timeutil import SimClock, ist

TRADE_DATE = date(2026, 10, 19)  # a Monday
EXPIRY = date(2026, 11, 3)  # 15 calendar days later
LOT = 65
BIG = 10**7


def make_contract(strike: float, ot: OptionType, expiry: date = EXPIRY, lot: int = LOT) -> OptionContract:
    return OptionContract(f"NIFTY{expiry:%y%m%d}{strike:g}{ot.value}", "NFO", "NIFTY", expiry, strike, ot, lot)


def bs_quotes(spot: float, expiry: date, now: datetime, iv: float = 0.13, r: float = 0.06, step: int = 50,
              range_pct: float = 0.1, spread: float = 0.10, lot: int = LOT):
    t = (ist(expiry, time(15, 30)) - now).total_seconds() / (365 * 24 * 3600)
    forward = spot * math.exp(r * t)
    centre = round(spot / step) * step
    half = int(spot * range_pct / step) * step
    contracts, quotes = [], {}
    for k in range(int(centre - half), int(centre + half) + 1, step):
        m = math.log(k / forward)
        vol = max(0.05, iv - 0.4 * m + 1.5 * m * m)
        for ot in (OptionType.PE, OptionType.CE):
            c = make_contract(k, ot, expiry, lot)
            price = black76_price(forward, k, t, vol, r, ot)
            bid = round_to_tick(max(0.05, price - spread / 2), 0.05, up=False)
            ask = round_to_tick(bid + spread, 0.05, up=True)
            contracts.append(c)
            quotes[c.key] = (bid, ask)
    return contracts, quotes


class FakeMarket:
    """MarketData + AccountData with prices that can move over (simulated) time."""

    def __init__(self, clock, spot: float, contracts: Sequence[OptionContract], quotes: dict[str, tuple[float, float]],
                 funds: float = 1_000_000.0, margin_per_lot: float = 50_000.0) -> None:
        self.clock = clock
        self.spot = spot
        self.contracts = list(contracts)
        self.base = dict(quotes)
        self.funds = funds
        self.margin_per_lot = margin_per_lot
        self.margin_surcharge: Callable[[int], float] = lambda lots: 0.0
        self.shift: Callable[[OptionContract, datetime], float] = lambda c, now: 1.0
        self.depth: dict[str, int] = {}  # optional ask/bid depth quantity per contract key
        self.missing: set[str] = set()  # contract keys with no quote at all
        self.no_ask: set[str] = set()
        self.no_bid: set[str] = set()
        self.margin_calls: list[int] = []

    def option_contracts(self, underlying: str) -> list[OptionContract]:
        return [c for c in self.contracts if c.underlying == underlying]

    def quote_for(self, c: OptionContract) -> Quote | None:
        if c.key in self.missing or c.key not in self.base:
            return None
        bid, ask = self.base[c.key]
        k = self.shift(c, self.clock.now())
        bid = round_to_tick(bid * k, 0.05, up=False)
        ask = round_to_tick(ask * k, 0.05, up=True)
        if ask <= bid:
            ask = round(bid + 0.05, 2)
        bid_v = None if c.key in self.no_bid else bid
        ask_v = None if c.key in self.no_ask else ask
        qty = self.depth.get(c.key, BIG)
        return Quote(bid=bid_v, ask=ask_v, last=(bid + ask) / 2, timestamp=self.clock.now(),
                     bid_depth=((bid_v, qty),) if bid_v else (), ask_depth=((ask_v, qty),) if ask_v else ())

    def quotes(self, contracts: Sequence[OptionContract]) -> dict[str, Quote]:
        out = {}
        for c in contracts:
            q = self.quote_for(c)
            if q is not None:
                out[c.key] = q
        return out

    def underlying_quote(self) -> Quote | None:
        return Quote(bid=None, ask=None, last=self.spot, timestamp=self.clock.now())

    def available_funds(self) -> float:
        return self.funds

    def basket_margin(self, orders: Sequence[OrderRequest]) -> MarginResult:
        lots = max(o.quantity for o in orders) // orders[0].contract.lot_size
        self.margin_calls.append(lots)
        return MarginResult(total=self.margin_per_lot * lots + self.margin_surcharge(lots))


def fast_execution(**overrides) -> ExecutionConfig:
    base = dict(fill_timeout_seconds=5, poll_interval_seconds=1, cancel_confirm_timeout_seconds=3,
                unknown_order_reconcile_seconds=3, position_sync_timeout_seconds=3, max_rebalance_attempts=3)
    base.update(overrides)
    return ExecutionConfig(**base)


class Harness:
    """Wires a full session around FakeMarket + SimulatedOrderGateway on a SimClock."""

    def __init__(self, start: datetime | None = None, spot: float = 25000.0, funds: float = 1_000_000.0,
                 margin_per_lot: float = 50_000.0, fill_model: str = "depth",
                 strategy_config: StrategyConfig | None = None, execution_config: ExecutionConfig | None = None,
                 store: DayStateStore | None = None, gateway_factory=None, costs: ZerodhaOptionsCostModel | None = None):
        self.clock = SimClock(start or ist(TRADE_DATE, time(9, 30)))
        contracts, quotes = bs_quotes(spot, EXPIRY, ist(TRADE_DATE, time(10, 0)))
        self.market = FakeMarket(self.clock, spot, contracts, quotes, funds, margin_per_lot)
        self.strategy_config = strategy_config or StrategyConfig(monitor_interval_seconds=60)
        self.execution_config = execution_config or fast_execution()
        self.journal = Journal(None, self.clock, "test", "test")
        self.alerter = MemoryAlerter()
        self.costs = costs or ZerodhaOptionsCostModel()
        self.store = store or DayStateStore(None)
        sim = SimulatedOrderGateway(self.market, self.clock, fill_model)
        self.gateway = gateway_factory(sim) if gateway_factory else sim
        self.strategy = IronCondorV1(self.strategy_config, self.execution_config, self.market, self.market,
                                     self.costs, self.journal)
        self.executor = BasketExecutor(self.gateway, self.market, self.clock, self.journal, self.alerter,
                                       self.costs, self.execution_config)
        self.session = TradingSession(self.strategy, self.executor, self.market, self.gateway, self.clock,
                                      self.journal, self.alerter, self.store, self.costs, self.strategy_config,
                                      self.execution_config, mode="test")

    def submitted(self) -> list[tuple[str, str]]:
        return [(e["role"], e["side"]) for e in self.journal.of("order_submit")]
