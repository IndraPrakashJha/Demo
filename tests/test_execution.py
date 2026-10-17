from datetime import time

from helpers import LOT, TRADE_DATE, Harness, fast_execution

from option_selling.alerts import AlertLevel
from option_selling.basket import BasketStatus, ExitReason
from option_selling.brokers.base import OrderOutcomeUnknown
from option_selling.models import STRIKE_ORDER, LegRole, Position, Side
from option_selling.timeutil import ist


class ScriptedGateway:
    """Wraps the simulated gateway to inject unknown outcomes and position anomalies."""

    def __init__(self, inner):
        self.inner = inner
        self.place_calls = []
        self.unknown_for = None  # request -> None | "lost" | "ghost"
        self.phantom: dict[str, int] = {}

    def place_order(self, request):
        self.place_calls.append(request)
        mode = self.unknown_for(request) if self.unknown_for else None
        if mode == "lost":
            raise OrderOutcomeUnknown("timeout; nothing reached the exchange")
        if mode == "ghost":
            self.inner.place_order(request)
            raise OrderOutcomeUnknown("timeout after the order was accepted")
        return self.inner.place_order(request)

    def order_status(self, order_id):
        return self.inner.order_status(order_id)

    def cancel_order(self, order_id):
        return self.inner.cancel_order(order_id)

    def orders_by_tag(self, tag):
        return self.inner.orders_by_tag(tag)

    def positions(self):
        rows = list(self.inner.positions())
        for symbol, qty in self.phantom.items():
            rows.append(Position(symbol, "NFO", "NRML", qty))
        return rows

    def restore_positions(self, positions):
        self.inner.restore_positions(positions)


def entered(h: Harness):
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    return plan, h.executor.enter(plan, "B1")


def broker_exposure(h: Harness, basket):
    return h.executor._broker_exposure(basket)


def submits_after(h: Harness, event_index: int):
    return [(e["role"], e["side"], e["quantity"]) for e in h.journal.events[event_index:] if e["event"] == "order_submit"]


def test_protective_legs_are_bought_before_any_short_leg():
    h = Harness()
    plan, basket = entered(h)
    assert basket.status is BasketStatus.OPEN
    assert basket.units == plan.units
    roles = [r for r, _ in h.submitted()]
    assert set(roles[:2]) == {"LONG_PUT", "LONG_CALL"}
    assert set(roles[2:]) == {"SHORT_PUT", "SHORT_CALL"}
    qty = plan.units * LOT
    assert broker_exposure(h, basket) == {LegRole.LONG_PUT: qty, LegRole.SHORT_PUT: -qty,
                                          LegRole.SHORT_CALL: -qty, LegRole.LONG_CALL: qty}
    assert basket.exposure() == broker_exposure(h, basket)


def test_partial_protective_fill_limits_shorts_and_trims_the_other_wing():
    h = Harness(execution_config=fast_execution(fill_timeout_seconds=0))
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    h.market.depth[plan.legs[LegRole.LONG_CALL].contract.key] = 2 * LOT
    basket = h.executor.enter(plan, "B1")
    assert basket.status is BasketStatus.OPEN
    assert basket.units == 2
    short_orders = [e for e in h.journal.of("order_submit") if e["side"] == "SELL" and e["role"].startswith("SHORT")]
    assert all(e["quantity"] == 2 * LOT for e in short_orders)
    assert broker_exposure(h, basket) == {LegRole.LONG_PUT: 2 * LOT, LegRole.SHORT_PUT: -2 * LOT,
                                          LegRole.SHORT_CALL: -2 * LOT, LegRole.LONG_CALL: 2 * LOT}
    assert AlertLevel.WARNING in h.alerter.levels()


def test_partial_short_fill_trims_to_complete_units_buying_back_shorts_first():
    h = Harness(execution_config=fast_execution(fill_timeout_seconds=0))
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    h.market.depth[plan.legs[LegRole.SHORT_PUT].contract.key] = 1 * LOT
    basket = h.executor.enter(plan, "B1")
    assert basket.status is BasketStatus.OPEN and basket.units == 1
    unbalanced_at = next(i for i, e in enumerate(h.journal.events) if e["event"] == "basket_unbalanced")
    trims = submits_after(h, unbalanced_at)
    assert trims[0][:2] == ("SHORT_CALL", "BUY")
    assert {t[0] for t in trims[1:]} == {"LONG_PUT", "LONG_CALL"} and all(t[1] == "SELL" for t in trims[1:])
    assert broker_exposure(h, basket) == {r: r.sign * LOT for r in STRIKE_ORDER}


def test_unfilled_protective_leg_aborts_without_selling_any_short():
    h = Harness()
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    h.market.no_ask.add(plan.legs[LegRole.LONG_PUT].contract.key)
    basket = h.executor.enter(plan, "B1")
    assert basket.status is BasketStatus.ABORTED
    assert not any(e["role"].startswith("SHORT") for e in h.journal.of("order_submit"))
    assert all(q == 0 for q in broker_exposure(h, basket).values())


def test_unknown_short_order_outcome_is_not_retried_and_wing_is_kept():
    h = Harness(gateway_factory=ScriptedGateway)
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    short_put = plan.legs[LegRole.SHORT_PUT].contract
    h.gateway.unknown_for = lambda req: "lost" if req.contract == short_put else None
    basket = h.executor.enter(plan, "B1")
    assert basket.status is BasketStatus.HALTED
    assert sum(1 for r in h.gateway.place_calls if r.contract == short_put) == 1
    exposure = broker_exposure(h, basket)
    assert exposure[LegRole.LONG_PUT] == plan.units * LOT  # protection for a possible late short fill
    assert exposure[LegRole.LONG_CALL] == 0
    assert exposure[LegRole.SHORT_CALL] == 0
    assert AlertLevel.CRITICAL in h.alerter.levels()
    assert h.journal.of("manual_intervention_required")


def test_unknown_outcome_found_by_tag_is_tracked_not_resubmitted():
    h = Harness(gateway_factory=ScriptedGateway)
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    long_call = plan.legs[LegRole.LONG_CALL].contract
    h.gateway.unknown_for = lambda req: "ghost" if req.contract == long_call else None
    basket = h.executor.enter(plan, "B1")
    assert basket.status is BasketStatus.OPEN
    assert sum(1 for r in h.gateway.place_calls if r.contract == long_call) == 1
    assert h.journal.of("order_found_by_tag")


def test_close_buys_back_shorts_before_selling_wings_and_confirms_flat():
    h = Harness()
    plan, basket = entered(h)
    mark = len(h.journal.events)
    basket = h.executor.close(basket, ExitReason.TIME)
    assert basket.status is BasketStatus.CLOSED
    exits = submits_after(h, mark)
    assert {r for r, s, _ in exits[:2]} == {"SHORT_PUT", "SHORT_CALL"} and all(s == "BUY" for _, s, _ in exits[:2])
    assert {r for r, s, _ in exits[2:]} == {"LONG_PUT", "LONG_CALL"} and all(s == "SELL" for _, s, _ in exits[2:])
    assert all(q == 0 for q in broker_exposure(h, basket).values())
    assert basket.realized_net_pnl() == basket.cash_flow() - basket.charges()


def test_unclosable_short_halts_and_keeps_its_wing():
    h = Harness()
    plan, basket = entered(h)
    short_call = plan.legs[LegRole.SHORT_CALL].contract
    h.market.no_ask.add(short_call.key)
    basket = h.executor.close(basket, ExitReason.STOP_LOSS)
    assert basket.status is BasketStatus.HALTED
    exposure = broker_exposure(h, basket)
    qty = plan.units * LOT
    assert exposure[LegRole.SHORT_CALL] == -qty
    assert exposure[LegRole.LONG_CALL] == qty
    assert exposure[LegRole.SHORT_PUT] == 0 and exposure[LegRole.LONG_PUT] == 0
    assert AlertLevel.CRITICAL in h.alerter.levels()


def test_basket_not_marked_closed_until_broker_is_flat():
    h = Harness(gateway_factory=ScriptedGateway)
    plan, basket = entered(h)
    h.gateway.phantom[plan.legs[LegRole.SHORT_PUT].contract.tradingsymbol] = -LOT
    mark = len(h.journal.events)
    basket = h.executor.close(basket, ExitReason.TARGET)
    assert basket.status is BasketStatus.HALTED
    qty = plan.units * LOT
    # Our confirmed shorts are bought back once; one lot of the put wing stays to cover
    # the unexplained short the broker reports, for the operator to resolve.
    exits = {(r, s): q for r, s, q in submits_after(h, mark)}
    assert exits == {("SHORT_PUT", "BUY"): qty, ("SHORT_CALL", "BUY"): qty,
                     ("LONG_PUT", "SELL"): qty - LOT, ("LONG_CALL", "SELL"): qty}
    assert broker_exposure(h, basket) == {LegRole.LONG_PUT: LOT, LegRole.SHORT_PUT: -LOT,
                                          LegRole.SHORT_CALL: 0, LegRole.LONG_CALL: 0}


def test_stale_positions_feed_does_not_cause_repeated_orders():
    h = Harness(gateway_factory=ScriptedGateway)
    plan, basket = entered(h)
    frozen = list(h.gateway.inner.positions())
    h.gateway.positions = lambda: frozen  # positions endpoint stops updating
    mark = len(h.journal.events)
    basket = h.executor.close(basket, ExitReason.STOP_LOSS)
    assert basket.status is BasketStatus.HALTED
    # Shorts bought back exactly once; wings kept because the broker still shows shorts.
    assert submits_after(h, mark) == [("SHORT_PUT", "BUY", plan.units * LOT), ("SHORT_CALL", "BUY", plan.units * LOT)]
    assert basket.exposure() == {LegRole.LONG_PUT: plan.units * LOT, LegRole.SHORT_PUT: 0,
                                 LegRole.SHORT_CALL: 0, LegRole.LONG_CALL: plan.units * LOT}
    assert AlertLevel.CRITICAL in h.alerter.levels()


def test_preexisting_position_in_selected_contract_skips_without_orders():
    h = Harness()
    h.clock.set(ist(TRADE_DATE, time(10, 0)))
    plan = h.strategy.plan_entry(h.clock.now())
    c = plan.legs[LegRole.SHORT_CALL].contract
    h.gateway.restore_positions([Position(c.tradingsymbol, c.exchange, "MIS", -LOT)])
    basket = h.executor.enter(plan, "B1")
    assert basket.status is BasketStatus.ABORTED
    assert "PREEXISTING_POSITION" in basket.notes
    assert not h.journal.of("order_submit")


def test_orders_are_sliced_below_freeze_limit():
    h = Harness(execution_config=fast_execution(max_order_lots=5))
    plan, basket = entered(h)
    assert plan.units == 18
    long_put_orders = [e["quantity"] for e in h.journal.of("order_submit") if e["role"] == "LONG_PUT"]
    assert long_put_orders == [5 * LOT, 5 * LOT, 5 * LOT, 3 * LOT]
    assert basket.units == 18


def test_entry_charges_recorded_per_executed_order():
    h = Harness()
    _, basket = entered(h)
    assert len(basket.executions) == 4
    assert all(e.charges > 20 for e in basket.executions)
    sells = [e for e in basket.executions if e.side is Side.SELL]
    buys = [e for e in basket.executions if e.side is Side.BUY]
    assert len(sells) == 2 and len(buys) == 2


def test_ledger_that_drops_fills_cannot_cause_repeated_orders():
    h = Harness()
    plan, basket = entered(h)
    basket.record = lambda execution: False  # every exit fill "already recorded"
    mark = len(h.journal.events)
    basket = h.executor.close(basket, ExitReason.TIME)
    assert basket.status is BasketStatus.HALTED
    qty = plan.units * LOT
    exits = submits_after(h, mark)
    # Each short bought back once; wings kept because the ledger still shows the shorts.
    assert exits == [("SHORT_PUT", "BUY", qty), ("SHORT_CALL", "BUY", qty)]
    assert h.journal.of("duplicate_order_id")
