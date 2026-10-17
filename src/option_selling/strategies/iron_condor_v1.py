"""IRON_CONDOR_V1: fixed four-leg NIFTY iron condor.

Rules (fixed, see ``SPEC``):
  * one entry attempt at 10:00 IST, at most 60 s late; otherwise skip the day
  * nearest expiry with >= ``min_days_to_expiry`` calendar days (configurable, default 10)
  * buy ~0.05 |delta| put, sell ~0.25 put, sell ~0.25 call, buy ~0.05 call
  * size by broker-confirmed funds and broker basket margin, <= 90% allocation, whole units only
  * exit at +0.25% / -0.50% of the approved basket margin (net P&L), or at 15:25 IST

The strategy only plans and signals; ``execution.BasketExecutor`` trades.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from ..basket import ExitReason
from ..brokers.base import AccountData, MarketData
from ..config import ExecutionConfig, StrategyConfig
from ..costs import ZerodhaOptionsCostModel
from ..greeks import delta as bs_delta
from ..greeks import implied_forward, implied_vol
from ..journal import Journal
from ..models import (
    PROTECTIVE_LEGS,
    SHORT_LEGS,
    STRIKE_ORDER,
    LegRole,
    OptionContract,
    OptionType,
    OrderRequest,
    Quote,
    Side,
)
from ..timeutil import ist, year_fraction
from .base import Skip


@dataclass(frozen=True)
class IronCondorV1Spec:
    name: str = "IRON_CONDOR_V1"
    underlying: str = "NIFTY"
    entry_time: time = time(10, 0)
    max_entry_delay_seconds: int = 60
    exit_time: time = time(15, 25)
    long_put_delta: float = 0.05
    short_put_delta: float = 0.25
    short_call_delta: float = 0.25
    long_call_delta: float = 0.05
    max_allocation_fraction: float = 0.90
    profit_target_fraction: float = 0.0025  # +0.25% of approved basket margin
    stop_loss_fraction: float = 0.0050  # -0.50% of approved basket margin

    def target_delta(self, role: LegRole) -> float:
        return {
            LegRole.LONG_PUT: self.long_put_delta,
            LegRole.SHORT_PUT: self.short_put_delta,
            LegRole.SHORT_CALL: self.short_call_delta,
            LegRole.LONG_CALL: self.long_call_delta,
        }[role]


SPEC = IronCondorV1Spec()
EXPIRY_CLOSE = time(15, 30)


@dataclass(frozen=True)
class ChainRow:
    contract: OptionContract
    quote: Quote
    iv: float
    delta: float

    @property
    def abs_delta(self) -> float:
        return abs(self.delta)


@dataclass(frozen=True)
class LegChoice:
    role: LegRole
    contract: OptionContract
    quote: Quote
    iv: float
    delta: float

    @property
    def touch_price(self) -> float:
        """Price an entry order trades at: ask for buys, bid for sells."""
        return self.quote.ask if self.role.is_long else self.quote.bid

    def to_dict(self) -> dict:
        return {
            "tradingsymbol": self.contract.tradingsymbol,
            "strike": self.contract.strike,
            "option_type": self.contract.option_type.value,
            "bid": self.quote.bid,
            "ask": self.quote.ask,
            "mid": self.quote.mid,
            "iv": round(self.iv, 5),
            "delta": round(self.delta, 4),
            "quote_time": self.quote.timestamp,
        }


@dataclass(frozen=True)
class EntryPlan:
    strategy: str
    decided_at: datetime
    expiry: date
    days_to_expiry: int
    underlying_price: float
    forward: float
    legs: dict[LegRole, LegChoice]
    lot_size: int
    units: int
    funds: float
    allocation_cap: float
    margin_per_unit: float
    margin_basis: float
    credit_per_unit_mid: float
    credit_per_unit_touch: float
    max_loss_per_unit: float
    estimated_round_trip_charges: float

    def contracts(self) -> dict[LegRole, OptionContract]:
        return {role: leg.contract for role, leg in self.legs.items()}

    def to_dict(self) -> dict:
        qty = self.units * self.lot_size
        friction = self.estimated_round_trip_charges + 2 * (self.credit_per_unit_mid - self.credit_per_unit_touch) * qty
        return {
            "strategy": self.strategy,
            "decided_at": self.decided_at,
            "expiry": self.expiry,
            "days_to_expiry": self.days_to_expiry,
            "underlying_price": self.underlying_price,
            "forward": round(self.forward, 2),
            "legs": {role.value: leg.to_dict() for role, leg in self.legs.items()},
            "lot_size": self.lot_size,
            "units": self.units,
            "quantity_per_leg": qty,
            "funds": self.funds,
            "allocation_cap": round(self.allocation_cap, 2),
            "margin_per_unit": round(self.margin_per_unit, 2),
            "margin_basis": round(self.margin_basis, 2),
            "credit_per_unit_mid": round(self.credit_per_unit_mid, 2),
            "credit_per_unit_touch": round(self.credit_per_unit_touch, 2),
            "max_loss_per_unit": round(self.max_loss_per_unit, 2),
            "max_loss_total": round(self.max_loss_per_unit * qty, 2),
            "profit_target_amount": round(SPEC.profit_target_fraction * self.margin_basis, 2),
            "stop_loss_amount": round(SPEC.stop_loss_fraction * self.margin_basis, 2),
            "estimated_round_trip_charges": round(self.estimated_round_trip_charges, 2),
            # Charges plus crossing the spread on entry and exit, relative to the stop distance.
            "estimated_friction": round(friction, 2),
            "friction_fraction_of_stop": round(friction / (SPEC.stop_loss_fraction * self.margin_basis), 3)
            if self.margin_basis
            else None,
        }


class IronCondorV1:
    spec = SPEC
    name = SPEC.name
    entry_time = SPEC.entry_time
    max_entry_delay_seconds = SPEC.max_entry_delay_seconds
    exit_time = SPEC.exit_time

    def __init__(
        self,
        config: StrategyConfig,
        execution: ExecutionConfig,
        market: MarketData,
        account: AccountData,
        costs: ZerodhaOptionsCostModel,
        journal: Journal,
    ) -> None:
        self.config = config
        self.execution = execution
        self.market = market
        self.account = account
        self.costs = costs
        self.journal = journal

    # -- calendar -----------------------------------------------------------
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.config.holidays

    def select_expiry(self, contracts: Sequence[OptionContract], today: date) -> date | None:
        eligible = sorted({c.expiry for c in contracts if (c.expiry - today).days >= self.config.min_days_to_expiry})
        return eligible[0] if eligible else None

    # -- entry --------------------------------------------------------------
    def plan_entry(self, now: datetime) -> EntryPlan | Skip:
        today = now.date()
        contracts = [c for c in self.market.option_contracts(self.spec.underlying) if c.expiry >= today]
        if not contracts:
            return self._skip("NO_INSTRUMENTS")
        expiry = self.select_expiry(contracts, today)
        if expiry is None:
            return self._skip("NO_ELIGIBLE_EXPIRY", min_days_to_expiry=self.config.min_days_to_expiry,
                              expiries=sorted({c.expiry for c in contracts})[:8])
        self.journal.record("expiry_selected", expiry=expiry, days_to_expiry=(expiry - today).days)

        underlying = self.market.underlying_quote()
        spot = underlying.last if underlying else None
        if not spot or spot <= 0:
            return self._skip("NO_UNDERLYING_PRICE")
        if not self._fresh(underlying, now):
            return self._skip("STALE_UNDERLYING_QUOTE", quote_time=underlying.timestamp)

        window = self.config.strike_window_pct
        candidates = [c for c in contracts if c.expiry == expiry and spot * (1 - window) <= c.strike <= spot * (1 + window)]
        if not candidates:
            return self._skip("NO_STRIKES_IN_WINDOW", spot=spot)
        quotes = self.market.quotes(candidates)
        t = year_fraction(now, ist(expiry, EXPIRY_CLOSE))
        if t <= 0:
            return self._skip("EXPIRY_IN_PAST")

        forward = self._forward(candidates, quotes, spot, t, now)
        if forward is None:
            return self._skip("NO_PARITY_FORWARD", spot=spot)
        chain = self._chain(candidates, quotes, forward, t, now)
        self.journal.record(
            "chain_snapshot",
            expiry=expiry,
            spot=spot,
            forward=round(forward, 2),
            time_to_expiry_years=round(t, 6),
            rows=[
                {"strike": r.contract.strike, "type": r.contract.option_type.value, "bid": r.quote.bid,
                 "ask": r.quote.ask, "iv": round(r.iv, 5), "delta": round(r.delta, 4)}
                for r in sorted(chain, key=lambda r: (r.contract.option_type.value, r.contract.strike))
            ],
        )

        legs = self._select_legs(chain)
        if isinstance(legs, Skip):
            return legs
        structure = self._validate_structure(legs, expiry)
        if isinstance(structure, Skip):
            return structure
        credit_mid, credit_touch, max_loss = structure
        lot_size = legs[LegRole.SHORT_PUT].contract.lot_size

        sizing = self._size(legs, lot_size)
        if isinstance(sizing, Skip):
            return sizing
        funds, cap, per_unit, units, margin_basis = sizing

        plan = EntryPlan(
            strategy=self.name,
            decided_at=now,
            expiry=expiry,
            days_to_expiry=(expiry - today).days,
            underlying_price=spot,
            forward=forward,
            legs=legs,
            lot_size=lot_size,
            units=units,
            funds=funds,
            allocation_cap=cap,
            margin_per_unit=per_unit,
            margin_basis=margin_basis,
            credit_per_unit_mid=credit_mid,
            credit_per_unit_touch=credit_touch,
            max_loss_per_unit=max_loss,
            estimated_round_trip_charges=self._round_trip_charges(legs, units * lot_size),
        )
        self.journal.record("entry_plan", plan=plan.to_dict())
        return plan

    def basket_orders(self, legs: dict[LegRole, LegChoice], quantity: int) -> list[OrderRequest]:
        """Entry orders for one basket (protective legs first), used for margin queries."""
        return [
            OrderRequest(
                contract=legs[role].contract,
                side=role.entry_side,
                quantity=quantity,
                price=legs[role].touch_price,
                product=self.execution.product,
                tag="",
            )
            for role in (*PROTECTIVE_LEGS, *SHORT_LEGS)
        ]

    def margin_basis_for_units(self, plan: EntryPlan, units: int) -> float:
        """Broker margin for the basket actually held (after any trim to fewer units)."""
        if units == plan.units:
            return plan.margin_basis
        try:
            return self.account.basket_margin(self.basket_orders(plan.legs, units * plan.lot_size)).total
        except Exception as exc:
            self.journal.record("margin_requery_failed", error=repr(exc))
            return plan.margin_basis * units / plan.units

    # -- exit ---------------------------------------------------------------
    def exit_signal(self, net_pnl: float, margin_basis: float, now: datetime) -> ExitReason | None:
        if now >= ist(now.date(), self.exit_time):
            return ExitReason.TIME
        if margin_basis <= 0:
            return None
        if net_pnl <= -self.spec.stop_loss_fraction * margin_basis:
            return ExitReason.STOP_LOSS
        if net_pnl >= self.spec.profit_target_fraction * margin_basis:
            return ExitReason.TARGET
        return None

    # -- internals ----------------------------------------------------------
    def _skip(self, reason: str, **details) -> Skip:
        self.journal.record("entry_skip", reason=reason, **details)
        return Skip(reason, details)

    def _fresh(self, quote: Quote, now: datetime) -> bool:
        if quote.timestamp is None:
            return True
        return (now - quote.timestamp).total_seconds() <= self.config.max_quote_age_seconds

    def _liquid(self, quote: Quote) -> bool:
        if not quote.two_sided:
            return False
        allowed = max(self.config.max_spread_abs, self.config.max_spread_pct * quote.mid)
        return quote.spread <= allowed + 1e-9

    def _forward(self, candidates, quotes, spot: float, t: float, now: datetime) -> float | None:
        by_strike: dict[float, dict[OptionType, Quote]] = {}
        for c in candidates:
            q = quotes.get(c.key)
            if q is not None and q.two_sided and self._fresh(q, now):
                by_strike.setdefault(c.strike, {})[c.option_type] = q
        pairs = [
            (k, v[OptionType.CE].mid, v[OptionType.PE].mid)
            for k, v in by_strike.items()
            if OptionType.CE in v and OptionType.PE in v
        ]
        pairs.sort(key=lambda p: abs(p[0] - spot))
        return implied_forward(pairs[:3], self.config.risk_free_rate, t)

    def _chain(self, candidates, quotes, forward: float, t: float, now: datetime) -> list[ChainRow]:
        rows = []
        r = self.config.risk_free_rate
        for c in candidates:
            q = quotes.get(c.key)
            if q is None or not self._fresh(q, now) or not self._liquid(q):
                continue
            otm = c.strike < forward if c.option_type is OptionType.PE else c.strike > forward
            if not otm:
                continue
            iv = implied_vol(q.mid, forward, c.strike, t, r, c.option_type)
            if iv is None:
                continue
            rows.append(ChainRow(c, q, iv, bs_delta(forward, c.strike, t, iv, c.option_type)))
        return rows

    def _closest(self, rows: list[ChainRow], role: LegRole) -> ChainRow | Skip:
        target = self.spec.target_delta(role)
        tolerance = self.config.long_delta_tolerance if role.is_long else self.config.short_delta_tolerance
        if not rows:
            return self._skip("NO_CANDIDATE_STRIKES", role=role)
        # Ties go to the further-OTM strike (lower risk).
        further = (lambda row: row.contract.strike) if role.option_type is OptionType.PE else (lambda row: -row.contract.strike)
        best = min(rows, key=lambda row: (abs(row.abs_delta - target), further(row)))
        if abs(best.abs_delta - target) > tolerance:
            return self._skip("NO_STRIKE_NEAR_TARGET_DELTA", role=role, target=target, tolerance=tolerance,
                              best_strike=best.contract.strike, best_delta=round(best.delta, 4))
        return best

    def _select_legs(self, chain: list[ChainRow]) -> dict[LegRole, LegChoice] | Skip:
        puts = [r for r in chain if r.contract.option_type is OptionType.PE]
        calls = [r for r in chain if r.contract.option_type is OptionType.CE]
        short_put = self._closest(puts, LegRole.SHORT_PUT)
        if isinstance(short_put, Skip):
            return short_put
        short_call = self._closest(calls, LegRole.SHORT_CALL)
        if isinstance(short_call, Skip):
            return short_call
        long_put = self._closest([r for r in puts if r.contract.strike < short_put.contract.strike], LegRole.LONG_PUT)
        if isinstance(long_put, Skip):
            return long_put
        long_call = self._closest([r for r in calls if r.contract.strike > short_call.contract.strike], LegRole.LONG_CALL)
        if isinstance(long_call, Skip):
            return long_call
        rows = {LegRole.LONG_PUT: long_put, LegRole.SHORT_PUT: short_put,
                LegRole.SHORT_CALL: short_call, LegRole.LONG_CALL: long_call}
        return {role: LegChoice(role, row.contract, row.quote, row.iv, row.delta) for role, row in rows.items()}

    def _validate_structure(self, legs: dict[LegRole, LegChoice], expiry: date) -> tuple[float, float, float] | Skip:
        contracts = [legs[role].contract for role in STRIKE_ORDER]
        if len({c.expiry for c in contracts}) != 1 or contracts[0].expiry != expiry:
            return self._skip("MIXED_EXPIRIES")
        if len({c.underlying for c in contracts}) != 1:
            return self._skip("MIXED_UNDERLYINGS")
        if len({c.lot_size for c in contracts}) != 1:
            return self._skip("INCOMPATIBLE_LOT_SIZES", lot_sizes=[c.lot_size for c in contracts])
        strikes = [c.strike for c in contracts]
        if not all(a < b for a, b in zip(strikes, strikes[1:])):
            return self._skip("STRIKE_ORDER_VIOLATED", strikes=strikes)
        lp, sp, sc, lc = (legs[role] for role in STRIKE_ORDER)
        credit_mid = sp.quote.mid + sc.quote.mid - lp.quote.mid - lc.quote.mid
        credit_touch = sp.touch_price + sc.touch_price - lp.touch_price - lc.touch_price
        widest_wing = max(sp.contract.strike - lp.contract.strike, lc.contract.strike - sc.contract.strike)
        # Equal quantities with long wings beyond the shorts bound the expiry loss per unit.
        max_loss = widest_wing - credit_touch
        if credit_touch <= 0:
            return self._skip("NON_POSITIVE_CREDIT", credit_touch=round(credit_touch, 2))
        if not math.isfinite(max_loss) or max_loss <= 0:
            return self._skip("INCONSISTENT_PRICES", max_loss=max_loss)
        return credit_mid, credit_touch, max_loss

    def _size(self, legs: dict[LegRole, LegChoice], lot_size: int):
        funds = self.account.available_funds()
        if funds is None or funds <= 0:
            return self._skip("NO_AVAILABLE_FUNDS", funds=funds)
        cap = funds * self.spec.max_allocation_fraction
        per_unit = self.account.basket_margin(self.basket_orders(legs, lot_size)).total
        if per_unit <= 0:
            return self._skip("INVALID_MARGIN", margin_per_unit=per_unit)
        units = math.floor(cap / per_unit)
        if self.config.max_units:
            units = min(units, self.config.max_units)
        margin = 0.0
        # Margin is not exactly linear in size, so confirm the full basket fits the cap.
        while units >= 1:
            margin = self.account.basket_margin(self.basket_orders(legs, units * lot_size)).total
            if margin <= cap:
                break
            units -= 1
        if units < 1:
            return self._skip("INSUFFICIENT_FUNDS", funds=funds, allocation_cap=cap, margin_per_unit=per_unit)
        self.journal.record("sizing", funds=funds, allocation_cap=cap, margin_per_unit=per_unit,
                            units=units, margin_basis=margin)
        return funds, cap, per_unit, units, margin

    def _round_trip_charges(self, legs: dict[LegRole, LegChoice], quantity: int) -> float:
        max_qty = self.execution.max_order_lots * legs[LegRole.SHORT_PUT].contract.lot_size
        total = 0.0
        for role, leg in legs.items():
            entry = role.entry_side
            price = leg.touch_price
            total += self.costs.sliced_charges(entry, quantity, price, max_qty).total
            exit_price = leg.quote.bid if entry is Side.BUY else leg.quote.ask
            total += self.costs.sliced_charges(entry.opposite, quantity, exit_price, max_qty).total
        return total
