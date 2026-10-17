from datetime import date, time

from helpers import TRADE_DATE, Harness

from option_selling.basket import BasketStatus
from option_selling.config import StrategyConfig
from option_selling.session import ENTERING_MARKER, DayOutcome
from option_selling.state import DayStateStore
from option_selling.timeutil import ist


def at(hh, mm, ss=0):
    return ist(TRADE_DATE, time(hh, mm, ss))


def roomy() -> dict:
    # Large margin per lot -> wide target/stop in rupees, so only the time exit fires.
    return dict(funds=10_000_000.0, margin_per_lot=2_000_000.0)


def test_waits_for_10am_and_exits_at_1525():
    h = Harness(start=at(9, 30), **roomy())
    result = h.session.run_day()
    assert result.outcome is DayOutcome.TRADED and result.reason == "TIME"
    attempt = h.journal.of("entry_attempt")[0]
    assert attempt["actual"] == at(10, 0).isoformat()
    assert result.basket.status is BasketStatus.CLOSED
    assert result.basket.exit_started_at == at(15, 25)
    assert result.net_pnl is not None


def test_late_scheduler_within_60_seconds_still_enters():
    h = Harness(start=at(10, 0, 45), **roomy())
    assert h.session.run_day().outcome is DayOutcome.TRADED


def test_more_than_60_seconds_late_skips_the_day_and_never_retries():
    h = Harness(start=at(10, 1, 1))
    first = h.session.run_day()
    assert first.outcome is DayOutcome.SKIPPED and first.reason == "ENTRY_WINDOW_MISSED"
    h.clock.set(at(11, 0))
    assert h.session.run_day().outcome is DayOutcome.ALREADY_ATTEMPTED
    assert not h.journal.of("order_submit")


def test_profit_target_exit_and_no_re_entry():
    h = Harness(start=at(9, 59))
    h.market.shift = lambda c, now: 0.6 if now >= at(11, 0) else 1.0
    result = h.session.run_day()
    assert result.outcome is DayOutcome.TRADED and result.reason == "TARGET"
    assert result.basket.exit_started_at == at(11, 0)
    assert result.net_pnl > 0
    orders = len(h.journal.of("order_submit"))
    h.clock.set(at(11, 30))
    assert h.session.run_day().outcome is DayOutcome.ALREADY_ATTEMPTED
    assert len(h.journal.of("order_submit")) == orders


def test_stop_loss_exit():
    h = Harness(start=at(9, 59))
    h.market.shift = lambda c, now: 1.3 if now >= at(10, 30) else 1.0
    result = h.session.run_day()
    assert result.outcome is DayOutcome.TRADED and result.reason == "STOP_LOSS"
    assert result.net_pnl < 0
    signal = h.journal.of("exit_signal")[0]
    assert signal["snapshot"]["net"] <= -0.005 * result.basket.margin_basis


def test_skip_reason_is_recorded_and_blocks_re_entry():
    h = Harness(start=at(9, 59), funds=10_000.0)
    result = h.session.run_day()
    assert result.outcome is DayOutcome.SKIPPED and result.reason == "INSUFFICIENT_FUNDS"
    assert h.session.run_day().outcome is DayOutcome.ALREADY_ATTEMPTED


def test_weekend_and_holidays_are_not_trading_days():
    sat = Harness(start=ist(date(2026, 10, 24), time(9, 30)))
    assert sat.session.run_day().outcome is DayOutcome.NOT_TRADING_DAY
    hol = Harness(start=at(9, 30), strategy_config=StrategyConfig(holidays=(TRADE_DATE,)))
    assert hol.session.run_day().outcome is DayOutcome.NOT_TRADING_DAY


def test_resume_after_restart_manages_open_basket_without_new_entry(tmp_path):
    store = DayStateStore(tmp_path)
    first = Harness(start=at(9, 59), store=store, **roomy())

    def crash(c, now):
        if now >= at(11, 0):
            raise KeyboardInterrupt  # process killed mid-session
        return 1.0

    first.market.shift = crash
    try:
        first.session.run_day()
    except KeyboardInterrupt:
        pass
    saved = store.load("IRON_CONDOR_V1", TRADE_DATE)
    assert saved.entry_attempted and saved.basket["status"] == "OPEN"

    second = Harness(start=at(11, 5), store=store, **roomy())
    result = second.session.run_day()
    assert result.outcome is DayOutcome.TRADED and result.reason == "TIME"
    assert second.journal.of("session_resume")
    assert {e["side"] for e in second.journal.of("order_submit")} == {"BUY", "SELL"}
    assert len(second.journal.of("order_submit")) == 4  # exit orders only
    assert result.basket.status is BasketStatus.CLOSED


def test_halted_day_stays_halted(tmp_path):
    store = DayStateStore(tmp_path)
    h = Harness(start=at(9, 30), store=store)
    state = store.load("IRON_CONDOR_V1", TRADE_DATE)
    state.halted, state.halt_reason = True, "operator investigating"
    store.save(state)
    assert h.session.run_day().outcome is DayOutcome.HALTED
    assert not h.journal.of("order_submit")


def test_crash_during_entry_halts_instead_of_re_entering(tmp_path):
    store = DayStateStore(tmp_path)
    state = store.load("IRON_CONDOR_V1", TRADE_DATE)
    state.entry_attempted, state.outcome = True, ENTERING_MARKER
    store.save(state)
    h = Harness(start=at(10, 0), store=store)
    result = h.session.run_day()
    assert result.outcome is DayOutcome.HALTED
    assert not h.journal.of("order_submit")


def test_planning_exception_skips_day():
    h = Harness(start=at(9, 59))

    def broken():
        raise ConnectionError("margins endpoint down")

    h.market.available_funds = broken
    result = h.session.run_day()
    assert result.outcome is DayOutcome.SKIPPED and result.reason == "ENTRY_PLANNING_ERROR"
