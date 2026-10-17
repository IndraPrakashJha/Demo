"""Broker-reconciled basket execution: entry, trimming, unwinding and exit.

Rules this module enforces:

* Protective (long) legs are bought first. Short legs are sold only against
  long quantity the broker has confirmed, in order status AND positions.
* Submitting an order is never treated as execution. Fills come from final
  order status; exposure comes from broker positions.
* An order whose placement outcome is unknown is looked up by its unique tag
  and never resubmitted. If it cannot be found, no further orders are sent for
  that leg, and the basket is halted for manual intervention once it has been
  made as safe as possible.
* Risk is reduced in order: short legs are bought back before long wings are
  sold, and a wing is never sold below the open quantity of its short leg.
* A basket is CLOSED or ABORTED only once broker positions are confirmed flat.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .alerts import Alerter, AlertLevel
from .basket import Basket, BasketStatus, ExitReason
from .brokers.base import MarketData, OrderGateway, OrderOutcomeUnknown, OrderRejected
from .config import ExecutionConfig
from .costs import ZerodhaOptionsCostModel
from .journal import Journal
from .models import (
    STRIKE_ORDER,
    Execution,
    LegRole,
    OptionContract,
    OrderRequest,
    OrderStatus,
    Quote,
    Side,
    round_to_tick,
)
from .strategies.base import EntryPlanLike
from .timeutil import Clock

Instruction = tuple[LegRole, Side, int]


@dataclass
class GroupResult:
    filled: dict[LegRole, int]
    unresolved: list[str]


class BasketExecutor:
    def __init__(
        self,
        gateway: OrderGateway,
        market: MarketData,
        clock: Clock,
        journal: Journal,
        alerter: Alerter,
        costs: ZerodhaOptionsCostModel,
        config: ExecutionConfig,
    ) -> None:
        self.gateway = gateway
        self.market = market
        self.clock = clock
        self.journal = journal
        self.alerter = alerter
        self.costs = costs
        self.config = config
        # Orders (by tag or order id) whose outcome could not be established, with their leg.
        self.unresolved: dict[str, LegRole] = {}

    # ------------------------------------------------------------------ entry
    def enter(self, plan: EntryPlanLike, basket_id: str) -> Basket:
        self.unresolved.clear()
        basket = Basket(
            basket_id=basket_id,
            strategy=plan.strategy,
            trade_date=plan.decided_at.date(),
            expiry=plan.expiry,
            lot_size=plan.lot_size,
            legs=plan.contracts(),
            planned_units=plan.units,
            product=self.config.product,
            margin_basis=plan.margin_basis,
            funds=plan.funds,
        )
        self.journal.record("entry_start", basket_id=basket_id, planned_units=plan.units)
        try:
            existing = self._existing_positions(basket)
        except Exception as exc:
            return self._abort_without_orders(basket, "POSITIONS_UNAVAILABLE", error=repr(exc))
        if existing:
            # Reconciliation needs these contracts to be flat before we start.
            return self._abort_without_orders(basket, "PREEXISTING_POSITION", positions=existing)

        lot = plan.lot_size
        quantity = plan.units * lot

        # 1. Protective legs first.
        group = self._execute_group(
            basket, [(LegRole.LONG_PUT, Side.BUY, quantity), (LegRole.LONG_CALL, Side.BUY, quantity)],
            self.config.limit_buffer_pct, stop_on_unresolved=True,
        )
        exposure, matched = self._sync_exposure(basket)
        if group.unresolved or not matched:
            return self._unwind_failed_entry(basket, "PROTECTIVE_LEGS_UNRECONCILED")
        protected_units = min(exposure[LegRole.LONG_PUT], exposure[LegRole.LONG_CALL]) // lot
        self.journal.record("protective_legs_confirmed", exposure=_named(exposure), protected_units=protected_units)
        if protected_units < 1:
            return self._unwind_failed_entry(basket, "PROTECTIVE_LEGS_NOT_FILLED")

        # 2. Short legs, only against broker-confirmed protection on both sides.
        short_qty = protected_units * lot
        group = self._execute_group(
            basket, [(LegRole.SHORT_PUT, Side.SELL, short_qty), (LegRole.SHORT_CALL, Side.SELL, short_qty)],
            self.config.limit_buffer_pct, stop_on_unresolved=True,
        )
        exposure, matched = self._sync_exposure(basket)
        if group.unresolved or not matched:
            return self._unwind_failed_entry(basket, "SHORT_LEGS_UNRECONCILED")

        # 3. Keep only complete units; trim anything else.
        units = max(0, min(role.sign * exposure[role] for role in STRIKE_ORDER)) // lot
        target = {role: role.sign * units * lot for role in STRIKE_ORDER}
        if units < 1:
            return self._unwind_failed_entry(basket, "SHORT_LEGS_NOT_FILLED")
        if exposure != target:
            self.journal.record("basket_unbalanced", exposure=_named(exposure), target=_named(target))
            if not self._rebalance(basket, target) or self.unresolved:
                return self._halt(basket, "TRIM_TO_COMPLETE_UNITS_FAILED")

        basket.units = units
        basket.status = BasketStatus.OPEN
        basket.entered_at = self.clock.now()
        self.journal.record("basket_entered", basket_id=basket_id, units=units, planned_units=plan.units,
                            exposure=_named(basket.exposure()), executions=[e.to_dict() for e in basket.executions])
        if units < plan.units:
            self.alerter.alert(AlertLevel.WARNING, "Basket partially filled; trimmed to complete units",
                               {"basket_id": basket_id, "planned_units": plan.units, "units": units})
        return basket

    # ------------------------------------------------------------------- exit
    def close(self, basket: Basket, reason: ExitReason) -> Basket:
        basket.status = BasketStatus.CLOSING
        basket.exit_reason = reason
        basket.exit_started_at = self.clock.now()
        self.journal.record("exit_start", basket_id=basket.basket_id, reason=reason)
        flat_target = {role: 0 for role in basket.legs}
        reached = self._rebalance(basket, flat_target)
        try:
            exposure = self._broker_exposure(basket)
        except Exception as exc:
            self.journal.record("positions_error", error=repr(exc))
            exposure = None
        if not reached or exposure != flat_target or self.unresolved:
            return self._halt(basket, "EXIT_NOT_CONFIRMED_FLAT")
        if not basket.is_flat():
            # Broker flat but our ledger is not: P&L cannot be trusted.
            basket.notes.append("LEDGER_MISMATCH_AT_CLOSE")
            self.alerter.alert(AlertLevel.WARNING, "Broker flat but fill ledger is not; check P&L",
                               {"basket_id": basket.basket_id, "ledger": _named(basket.exposure())})
        basket.status = BasketStatus.CLOSED
        basket.closed_at = self.clock.now()
        self.journal.record("basket_closed", basket_id=basket.basket_id, reason=reason,
                            gross_pnl=round(basket.cash_flow(), 2), charges=round(basket.charges(), 2),
                            net_pnl=round(basket.cash_flow() - basket.charges(), 2))
        return basket

    # -------------------------------------------------------------- recovery
    def _abort_without_orders(self, basket: Basket, reason: str, **details) -> Basket:
        basket.status = BasketStatus.ABORTED
        basket.notes.append(reason)
        self.journal.record("entry_aborted", basket_id=basket.basket_id, reason=reason, orders_sent=False, **details)
        self.alerter.alert(AlertLevel.WARNING, f"Entry skipped: {reason}", {"basket_id": basket.basket_id, **details})
        return basket

    def _unwind_failed_entry(self, basket: Basket, reason: str) -> Basket:
        self.journal.record("entry_failed", basket_id=basket.basket_id, reason=reason,
                            ledger=_named(basket.exposure()), unresolved=dict(self.unresolved))
        flat = self._rebalance(basket, {role: 0 for role in basket.legs})
        if flat and not self.unresolved:
            basket.status = BasketStatus.ABORTED
            basket.notes.append(reason)
            self.journal.record("entry_aborted", basket_id=basket.basket_id, reason=reason, orders_sent=True)
            self.alerter.alert(AlertLevel.WARNING, f"Entry abandoned and unwound: {reason}",
                               {"basket_id": basket.basket_id})
            return basket
        return self._halt(basket, reason)

    def _halt(self, basket: Basket, reason: str) -> Basket:
        basket.status = BasketStatus.HALTED
        basket.notes.append(reason)
        try:
            broker = _named(self._broker_exposure(basket))
        except Exception as exc:
            broker = f"unavailable: {exc!r}"
        details = {
            "basket_id": basket.basket_id,
            "reason": reason,
            "broker_exposure": broker,
            "ledger_exposure": _named(basket.exposure()),
            "unresolved_orders": {k: v.value for k, v in self.unresolved.items()},
            "contracts": {r.value: c.tradingsymbol for r, c in basket.legs.items()},
        }
        self.journal.record("manual_intervention_required", **details)
        self.alerter.alert(AlertLevel.CRITICAL, "MANUAL INTERVENTION REQUIRED: automation halted", details)
        return basket

    def _rebalance(self, basket: Basket, target: dict[LegRole, int]) -> bool:
        """Reduce exposure to ``target`` with risk-reducing orders; True once the broker confirms it.

        Order quantities come from the fill ledger (broker-confirmed order fills),
        so a lagging or wrong positions feed can never cause repeated orders.
        Wing protection uses whichever of ledger and broker shows more short.
        """
        initial = basket.exposure()
        # Independent cap: across all attempts no leg trades more than its initial excess,
        # counted from order fills, so even a wrong ledger cannot cause repeated orders.
        budget = {role: abs(initial[role] - target[role]) for role in STRIKE_ORDER}

        def capped(orders: list[Instruction]) -> list[Instruction]:
            return [(r, s, min(q, budget[r])) for r, s, q in orders if min(q, budget[r]) > 0]

        def spend(result: GroupResult) -> None:
            for r, q in result.filled.items():
                budget[r] -= q

        for attempt in range(self.config.max_rebalance_attempts):
            broker, matched = self._sync_exposure(basket)
            ledger = basket.exposure()
            if ledger == target:
                break
            buffer = self.config.limit_buffer_pct + attempt * self.config.retry_buffer_step_pct
            blocked = set(self.unresolved.values())

            # Phase 1: buy back short quantity beyond the target.
            buys = capped([
                (role, Side.BUY, min(target[role], 0) - ledger[role])
                for role in STRIKE_ORDER
                if ledger[role] < min(target[role], 0) and role not in blocked
            ])
            if buys:
                spend(self._execute_group(basket, buys, buffer, stop_on_unresolved=False))
                broker, matched = self._sync_exposure(basket)
                ledger = basket.exposure()

            # Phase 2: sell long quantity beyond the target, never uncovering a short.
            sells = []
            for role in STRIKE_ORDER:
                ceiling = max(target[role], 0)
                if ledger[role] <= ceiling or role in blocked:
                    continue
                keep = ceiling
                if role.is_long:
                    if role.pair in blocked:
                        continue  # a short order of unknown outcome may still fill: keep the wing
                    open_short = -min(ledger[role.pair], 0)
                    if broker is not None:
                        open_short = max(open_short, -min(broker[role.pair], 0))
                    keep = max(keep, open_short)
                if ledger[role] > keep:
                    sells.append((role, Side.SELL, ledger[role] - keep))
            sells = capped(sells)
            if sells:
                spend(self._execute_group(basket, sells, buffer, stop_on_unresolved=False))
            if basket.exposure() != target:
                self.clock.sleep(self.config.rebalance_retry_seconds)
        broker, matched = self._sync_exposure(basket)
        return matched and broker == target

    # ----------------------------------------------------------- order flow
    def _execute_group(
        self, basket: Basket, instructions: list[Instruction], buffer: float, stop_on_unresolved: bool
    ) -> GroupResult:
        placed: list[tuple[LegRole, str]] = []
        unresolved: list[str] = []
        for role, side, quantity in instructions:
            if quantity <= 0:
                continue
            contract = basket.legs[role]
            quote = self._quote(contract)
            price = self._limit_price(contract, side, quote, buffer)
            if price is None:
                self.journal.record("order_not_priced", role=role, side=side, quantity=quantity,
                                    bid=quote.bid if quote else None, ask=quote.ask if quote else None)
                continue
            role_unresolved = False
            for slice_qty in self._slices(quantity, contract.lot_size):
                request = OrderRequest(contract, side, slice_qty, price, self.config.product, self._new_tag())
                order_ids = self._submit(role, request)
                if order_ids is None:
                    unresolved.append(request.tag)
                    role_unresolved = True
                    break
                placed.extend((role, oid) for oid in order_ids)
            if role_unresolved and stop_on_unresolved:
                break

        statuses = self._await_terminal([oid for _, oid in placed])
        filled: dict[LegRole, int] = defaultdict(int)
        for role, oid in placed:
            status = statuses.get(oid)
            if status is None or not status.state.terminal:
                unresolved.append(oid)
                self.unresolved[oid] = role
                self.journal.record("order_unresolved", role=role, order_id=oid,
                                    last_status=_status_dict(status) if status else None)
                continue
            self.journal.record("order_final", role=role, **_status_dict(status))
            if status.filled_quantity > 0:
                self._record(basket, role, status)
                filled[role] += status.filled_quantity
        return GroupResult(dict(filled), unresolved)

    def _submit(self, role: LegRole, request: OrderRequest) -> list[str] | None:
        """Order ids for the request; ``[]`` if rejected; ``None`` if the outcome is unknown."""
        self.journal.record("order_submit", role=role, tradingsymbol=request.contract.tradingsymbol,
                            side=request.side, quantity=request.quantity, price=request.price, tag=request.tag)
        try:
            return [self.gateway.place_order(request)]
        except OrderRejected as exc:
            self.journal.record("order_rejected", role=role, tag=request.tag, error=str(exc))
            return []
        except OrderOutcomeUnknown as exc:
            self.journal.record("order_outcome_unknown", role=role, tag=request.tag, error=str(exc))
        found = self._find_by_tag(request.tag)
        if found:
            self.journal.record("order_found_by_tag", role=role, tag=request.tag, order_ids=found)
            return found
        self.unresolved[request.tag] = role
        self.alerter.alert(AlertLevel.WARNING, "Order outcome unknown and not found by tag; not retrying",
                           {"role": role.value, "tag": request.tag, "tradingsymbol": request.contract.tradingsymbol})
        return None

    def _find_by_tag(self, tag: str) -> list[str]:
        deadline = self._deadline(self.config.unknown_order_reconcile_seconds)
        while True:
            try:
                matches = self.gateway.orders_by_tag(tag)
            except Exception as exc:
                self.journal.record("orders_lookup_error", tag=tag, error=repr(exc))
                matches = []
            if matches:
                return [m.order_id for m in matches]
            if self.clock.now() >= deadline:
                return []
            self.clock.sleep(self.config.poll_interval_seconds)

    def _await_terminal(self, order_ids: list[str]) -> dict[str, OrderStatus]:
        last: dict[str, OrderStatus] = {}
        pending = set(order_ids)

        def poll() -> None:
            for oid in sorted(pending):
                try:
                    status = self.gateway.order_status(oid)
                except Exception as exc:
                    self.journal.record("order_status_error", order_id=oid, error=repr(exc))
                    continue
                last[oid] = status
                if status.state.terminal:
                    pending.discard(oid)

        deadline = self._deadline(self.config.fill_timeout_seconds)
        while True:
            poll()
            if not pending or self.clock.now() >= deadline:
                break
            self.clock.sleep(self.config.poll_interval_seconds)
        if pending:
            for oid in sorted(pending):
                self.journal.record("order_cancel", order_id=oid, reason="fill timeout")
                try:
                    self.gateway.cancel_order(oid)
                except Exception as exc:
                    self.journal.record("order_cancel_error", order_id=oid, error=repr(exc))
            deadline = self._deadline(self.config.cancel_confirm_timeout_seconds)
            while True:
                poll()
                if not pending or self.clock.now() >= deadline:
                    break
                self.clock.sleep(self.config.poll_interval_seconds)
        return last

    def _record(self, basket: Basket, role: LegRole, status: OrderStatus) -> None:
        charges = self.costs.order_charges(status.side, status.filled_quantity, status.average_price).total
        added = basket.record(Execution(role, status.order_id, status.side, status.filled_quantity,
                                        status.average_price, self.clock.now(), charges))
        if not added:
            # A fill we cannot add to the ledger makes the ledger wrong: stop trading this leg.
            self.unresolved[status.order_id] = role
            self.journal.record("duplicate_order_id", role=role, **_status_dict(status))
            self.alerter.alert(AlertLevel.WARNING, "Fill with an already-recorded order id; leg blocked",
                               {"order_id": status.order_id, "role": role.value})

    # ------------------------------------------------------------ positions
    def _broker_exposure(self, basket: Basket) -> dict[LegRole, int]:
        by_symbol: dict[str, int] = defaultdict(int)
        exchanges = {c.exchange for c in basket.legs.values()}
        for p in self.gateway.positions():
            if p.exchange in exchanges and p.product == basket.product:
                by_symbol[p.tradingsymbol] += p.quantity
        return {role: by_symbol.get(c.tradingsymbol, 0) for role, c in basket.legs.items()}

    def _existing_positions(self, basket: Basket) -> dict[str, int]:
        symbols = {c.tradingsymbol for c in basket.legs.values()}
        existing: dict[str, int] = {}
        for p in self.gateway.positions():
            if p.tradingsymbol in symbols and p.quantity:
                existing[f"{p.tradingsymbol}/{p.product}"] = p.quantity
        return existing

    def _sync_exposure(self, basket: Basket) -> tuple[dict[LegRole, int] | None, bool]:
        """Latest broker exposure, and whether it agrees with the fill ledger.

        Positions can lag order completion briefly, so wait up to the sync timeout.
        """
        deadline = self._deadline(self.config.position_sync_timeout_seconds)
        exposure = None
        while True:
            try:
                exposure = self._broker_exposure(basket)
            except Exception as exc:
                self.journal.record("positions_error", error=repr(exc))
            if exposure is not None and exposure == basket.exposure():
                return exposure, True
            if self.clock.now() >= deadline:
                break
            self.clock.sleep(self.config.poll_interval_seconds)
        self.journal.record("exposure_mismatch", broker=_named(exposure) if exposure else None,
                            ledger=_named(basket.exposure()))
        return exposure, False

    # -------------------------------------------------------------- helpers
    def _quote(self, contract: OptionContract) -> Quote | None:
        try:
            return self.market.quotes([contract]).get(contract.key)
        except Exception as exc:
            self.journal.record("quote_error", tradingsymbol=contract.tradingsymbol, error=repr(exc))
            return None

    @staticmethod
    def _limit_price(contract: OptionContract, side: Side, quote: Quote | None, buffer: float) -> float | None:
        if quote is None:
            return None
        tick = contract.tick_size
        if side is Side.BUY:
            if not quote.ask or quote.ask <= 0:
                return None
            return round_to_tick(quote.ask * (1 + buffer), tick, up=True)
        if not quote.bid or quote.bid <= 0:
            return None
        return max(tick, round_to_tick(quote.bid * (1 - buffer), tick, up=False))

    def _slices(self, quantity: int, lot_size: int) -> list[int]:
        max_qty = self.config.max_order_lots * lot_size
        slices = []
        while quantity > 0:
            q = min(quantity, max_qty)
            slices.append(q)
            quantity -= q
        return slices

    def _new_tag(self) -> str:
        prefix = self.config.tag_prefix
        return prefix + uuid.uuid4().hex[: 20 - len(prefix)]

    def _deadline(self, seconds: float) -> datetime:
        return self.clock.now() + timedelta(seconds=seconds)


def _named(exposure: dict[LegRole, int] | None) -> dict[str, int] | None:
    return {role.value: qty for role, qty in exposure.items()} if exposure is not None else None


def _status_dict(status: OrderStatus) -> dict:
    return {
        "order_id": status.order_id,
        "tag": status.tag,
        "tradingsymbol": status.tradingsymbol,
        "side": status.side.value,
        "quantity": status.quantity,
        "filled_quantity": status.filled_quantity,
        "average_price": status.average_price,
        "state": status.state.value,
        "message": status.message,
    }
