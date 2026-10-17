"""One trading day for one strategy: schedule, single entry attempt, monitoring, exit.

The same code runs shadow, live and backtest sessions; only the clock, data
source and order gateway differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from .alerts import Alerter, AlertLevel
from .basket import Basket, BasketStatus, ExitReason
from .brokers.base import MarketData, OrderGateway
from .config import ExecutionConfig, StrategyConfig
from .costs import ZerodhaOptionsCostModel
from .execution import BasketExecutor
from .journal import Journal
from .models import Position
from .pnl import mark_basket
from .state import DayState, DayStateStore
from .strategies.base import Skip, Strategy
from .timeutil import Clock, ist, wait_until

ENTERING_MARKER = "ENTERING"


class DayOutcome(str, Enum):
    NOT_TRADING_DAY = "NOT_TRADING_DAY"
    ALREADY_ATTEMPTED = "ALREADY_ATTEMPTED"
    SKIPPED = "SKIPPED"
    ABORTED = "ABORTED"  # orders may have been sent; unwound and confirmed flat
    TRADED = "TRADED"
    HALTED = "HALTED"  # manual intervention required


@dataclass
class DayResult:
    trade_date: date
    outcome: DayOutcome
    reason: str | None = None
    plan: dict | None = None
    basket: Basket | None = None

    @property
    def net_pnl(self) -> float | None:
        b = self.basket
        if b is None or b.status not in (BasketStatus.CLOSED, BasketStatus.ABORTED) or not b.is_flat():
            return None
        return b.realized_net_pnl()

    def to_row(self) -> dict:
        row: dict = {"date": self.trade_date.isoformat(), "outcome": self.outcome.value, "reason": self.reason}
        if self.plan:
            p = self.plan
            row.update(expiry=str(p["expiry"]), days_to_expiry=p["days_to_expiry"],
                       underlying=p["underlying_price"], forward=p["forward"])
            for role, leg in p["legs"].items():
                row[f"{role.lower()}_strike"] = leg["strike"]
                row[f"{role.lower()}_delta"] = leg["delta"]
            row.update(planned_units=p["units"], lot_size=p["lot_size"], funds=p["funds"],
                       credit_per_unit_touch=p["credit_per_unit_touch"],
                       friction_fraction_of_stop=p["friction_fraction_of_stop"])
        b = self.basket
        if b is not None:
            row.update(units=b.units, margin_basis=round(b.margin_basis, 2), basket_status=b.status.value,
                       entered_at=b.entered_at.isoformat() if b.entered_at else None,
                       exit_reason=b.exit_reason.value if b.exit_reason else None,
                       closed_at=b.closed_at.isoformat() if b.closed_at else None,
                       gross_pnl=round(b.cash_flow(), 2) if b.is_flat() else None,
                       charges=round(b.charges(), 2))
            net = self.net_pnl
            row["net_pnl"] = round(net, 2) if net is not None else None
            row["net_pnl_pct_of_margin"] = (
                round(100 * net / b.margin_basis, 4) if net is not None and b.margin_basis and b.units else None
            )
        return row


class TradingSession:
    def __init__(
        self,
        strategy: Strategy,
        executor: BasketExecutor,
        market: MarketData,
        gateway: OrderGateway,
        clock: Clock,
        journal: Journal,
        alerter: Alerter,
        store: DayStateStore,
        costs: ZerodhaOptionsCostModel,
        strategy_config: StrategyConfig,
        execution_config: ExecutionConfig,
        mode: str,
        monitor_interval_seconds: float | None = None,
    ) -> None:
        self.strategy = strategy
        self.executor = executor
        self.market = market
        self.gateway = gateway
        self.clock = clock
        self.journal = journal
        self.alerter = alerter
        self.store = store
        self.costs = costs
        self.strategy_config = strategy_config
        self.execution_config = execution_config
        self.mode = mode
        self.monitor_interval = monitor_interval_seconds or strategy_config.monitor_interval_seconds

    # ----------------------------------------------------------------- day
    def run_day(self) -> DayResult:
        today = self.clock.now().date()
        state = self.store.load(self.strategy.name, today)
        self.journal.record("session_start", strategy=self.strategy.name, trade_date=today, state=state.to_dict())

        if state.halted:
            self.alerter.alert(AlertLevel.CRITICAL, "Strategy halted for today; manual intervention pending",
                               {"trade_date": today, "reason": state.halt_reason})
            return DayResult(today, DayOutcome.HALTED, state.halt_reason)
        if state.basket:
            basket = Basket.from_dict(state.basket)
            if basket.status in (BasketStatus.OPEN, BasketStatus.CLOSING, BasketStatus.ENTERING):
                return self._resume(state, basket)
        if state.outcome == ENTERING_MARKER and not state.basket:
            # Crashed between starting entry and recording the basket.
            return self._halt(state, None, "ENTRY_INTERRUPTED_BEFORE_BASKET_RECORDED")
        if state.entry_attempted:
            return DayResult(today, DayOutcome.ALREADY_ATTEMPTED, state.outcome)
        if not self.strategy.is_trading_day(today):
            self.journal.record("not_trading_day", trade_date=today)
            return DayResult(today, DayOutcome.NOT_TRADING_DAY)

        entry_at = ist(today, self.strategy.entry_time)
        latest = entry_at + timedelta(seconds=self.strategy.max_entry_delay_seconds)
        if self.clock.now() > latest:
            state.entry_attempted = True
            state.entry_attempted_at = self.clock.now().isoformat()
            return self._skip(state, "ENTRY_WINDOW_MISSED", started_at=self.clock.now())
        wait_until(self.clock, entry_at)

        now = self.clock.now()
        # Recorded before any entry work, so no restart can make a second attempt today.
        state.entry_attempted = True
        state.entry_attempted_at = now.isoformat()
        self.store.save(state)
        lateness = (now - entry_at).total_seconds()
        self.journal.record("entry_attempt", scheduled=entry_at, actual=now, delay_seconds=round(lateness, 3))
        if now > latest:
            return self._skip(state, "ENTRY_WINDOW_MISSED", delay_seconds=lateness)

        try:
            plan = self.strategy.plan_entry(now)
        except Exception as exc:
            self.journal.record("exception", where="plan_entry", error=repr(exc))
            return self._skip(state, "ENTRY_PLANNING_ERROR", error=repr(exc))
        if isinstance(plan, Skip):
            return self._skip(state, plan.reason, **plan.details)
        plan_dict = plan.to_dict()

        state.outcome = ENTERING_MARKER
        self.store.save(state)
        basket_id = f"{self.strategy.name}-{today.isoformat()}-{self.mode}"
        try:
            basket = self.executor.enter(plan, basket_id)
        except Exception as exc:
            self.journal.record("exception", where="enter", error=repr(exc))
            return self._halt(state, None, f"ENTRY_EXCEPTION: {exc!r}", plan_dict)
        self._save_basket(state, basket)

        if basket.status is BasketStatus.ABORTED:
            state.outcome = DayOutcome.ABORTED.value
            state.reason = basket.notes[-1] if basket.notes else None
            self.store.save(state)
            return DayResult(today, DayOutcome.ABORTED, state.reason, plan_dict, basket)
        if basket.status is BasketStatus.HALTED:
            return self._halt(state, basket, basket.notes[-1] if basket.notes else "HALTED", plan_dict)

        if basket.units != plan.units:
            basket.margin_basis = self.strategy.margin_basis_for_units(plan, basket.units)
            self.journal.record("margin_basis_updated", units=basket.units, margin_basis=basket.margin_basis)
        self._save_basket(state, basket)
        return self._monitor_and_close(state, basket, plan_dict)

    # ------------------------------------------------------------ internals
    def _resume(self, state: DayState, basket: Basket) -> DayResult:
        self.journal.record("session_resume", basket_id=basket.basket_id, status=basket.status)
        restore = getattr(self.gateway, "restore_positions", None)
        if restore is not None:  # simulated gateways lose positions on restart
            restore(Position(c.tradingsymbol, c.exchange, basket.product, basket.net_quantity(role))
                    for role, c in basket.legs.items())
        if basket.status is BasketStatus.ENTERING:
            return self._halt(state, basket, "ENTRY_INTERRUPTED")
        return self._monitor_and_close(state, basket, None)

    def _monitor_and_close(self, state: DayState, basket: Basket, plan_dict: dict | None) -> DayResult:
        reason = basket.exit_reason if basket.status is BasketStatus.CLOSING else self._monitor(basket)
        try:
            basket = self.executor.close(basket, reason)
        except Exception as exc:
            self.journal.record("exception", where="close", error=repr(exc))
            return self._halt(state, basket, f"EXIT_EXCEPTION: {exc!r}", plan_dict)
        self._save_basket(state, basket)
        if basket.status is not BasketStatus.CLOSED:
            return self._halt(state, basket, basket.notes[-1] if basket.notes else "EXIT_FAILED", plan_dict)
        state.outcome = DayOutcome.TRADED.value
        state.reason = reason.value
        self.store.save(state)
        return DayResult(state.trade_date, DayOutcome.TRADED, reason.value, plan_dict, basket)

    def _monitor(self, basket: Basket) -> ExitReason:
        exit_at = ist(basket.trade_date, self.strategy.exit_time)
        max_failures = self.strategy_config.max_consecutive_mark_failures
        max_order_qty = self.execution_config.max_order_lots * basket.lot_size
        failures = 0
        last_logged: datetime | None = None
        while True:
            now = self.clock.now()
            if now >= exit_at:
                self.journal.record("exit_signal", reason=ExitReason.TIME)
                return ExitReason.TIME
            snapshot = None
            try:
                quotes = self.market.quotes(list(basket.legs.values()))
                snapshot = mark_basket(basket, quotes, self.strategy_config.mark_method, self.costs,
                                       max_order_qty, now, self.strategy_config.max_quote_age_seconds)
            except Exception as exc:
                self.journal.record("mark_error", error=repr(exc))
            if snapshot is None:
                failures += 1
                self.journal.record("mark_unavailable", consecutive=failures)
                if failures >= max_failures:
                    self.alerter.alert(AlertLevel.WARNING, "Basket marks unavailable; exiting",
                                       {"basket_id": basket.basket_id, "consecutive_failures": failures})
                    return ExitReason.DATA_FAILURE
            else:
                failures = 0
                interval = self.strategy_config.mtm_journal_interval_seconds
                if last_logged is None or (now - last_logged).total_seconds() >= interval:
                    self.journal.record("mtm", **snapshot.to_dict())
                    last_logged = now
                reason = self.strategy.exit_signal(snapshot.net, basket.margin_basis, now)
                if reason is not None:
                    self.journal.record("exit_signal", reason=reason, snapshot=snapshot.to_dict(),
                                        margin_basis=basket.margin_basis)
                    return reason
            remaining = (exit_at - self.clock.now()).total_seconds()
            self.clock.sleep(max(0.0, min(self.monitor_interval, remaining)))

    def _save_basket(self, state: DayState, basket: Basket) -> None:
        state.basket = basket.to_dict()
        self.store.save(state)

    def _skip(self, state: DayState, reason: str, **details) -> DayResult:
        state.outcome = DayOutcome.SKIPPED.value
        state.reason = reason
        self.store.save(state)
        self.journal.record("day_skipped", reason=reason, **details)
        return DayResult(state.trade_date, DayOutcome.SKIPPED, reason)

    def _halt(self, state: DayState, basket: Basket | None, reason: str, plan_dict: dict | None = None) -> DayResult:
        state.halted = True
        state.halt_reason = reason
        state.outcome = DayOutcome.HALTED.value
        if basket is not None:
            state.basket = basket.to_dict()
        self.store.save(state)
        self.journal.record("session_halted", reason=reason)
        self.alerter.alert(AlertLevel.CRITICAL, "MANUAL INTERVENTION REQUIRED: session halted",
                           {"trade_date": state.trade_date, "reason": reason,
                            "basket_id": basket.basket_id if basket else None})
        return DayResult(state.trade_date, DayOutcome.HALTED, reason, plan_dict, basket)
