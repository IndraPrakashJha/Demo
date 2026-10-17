from datetime import date, time, timedelta

import pytest
from helpers import EXPIRY, LOT, TRADE_DATE, FakeMarket, bs_quotes, fast_execution, make_contract

from option_selling.basket import ExitReason
from option_selling.config import StrategyConfig
from option_selling.costs import ZerodhaOptionsCostModel
from option_selling.journal import Journal
from option_selling.models import STRIKE_ORDER, LegRole, OptionType
from option_selling.strategies.base import Skip
from option_selling.strategies.iron_condor_v1 import SPEC, EntryPlan, IronCondorV1
from option_selling.timeutil import SimClock, ist

NOW = ist(TRADE_DATE, time(10, 0))


def build(market=None, config=None, funds=1_000_000.0, margin_per_lot=50_000.0):
    clock = SimClock(NOW)
    if market is None:
        contracts, quotes = bs_quotes(25000.0, EXPIRY, NOW)
        market = FakeMarket(clock, 25000.0, contracts, quotes, funds, margin_per_lot)
    journal = Journal(None, clock, "t", "test")
    strategy = IronCondorV1(config or StrategyConfig(), fast_execution(), market, market,
                            ZerodhaOptionsCostModel(), journal)
    return strategy, market, journal


def test_spec_values_are_the_fixed_v1_rules():
    assert SPEC.name == "IRON_CONDOR_V1"
    assert (SPEC.entry_time, SPEC.max_entry_delay_seconds, SPEC.exit_time) == (time(10, 0), 60, time(15, 25))
    assert [SPEC.target_delta(r) for r in STRIKE_ORDER] == [0.05, 0.25, 0.25, 0.05]
    assert SPEC.max_allocation_fraction == 0.90
    assert SPEC.profit_target_fraction == 0.0025
    assert SPEC.stop_loss_fraction == 0.0050


def test_expiry_needs_minimum_days_and_is_configurable():
    strategy, _, _ = build()
    contracts = [make_contract(25000, OptionType.CE, TRADE_DATE + timedelta(days=n)) for n in (1, 8, 15, 22)]
    assert strategy.select_expiry(contracts, TRADE_DATE) == TRADE_DATE + timedelta(days=15)
    relaxed, _, _ = build(config=StrategyConfig(min_days_to_expiry=7))
    assert relaxed.select_expiry(contracts, TRADE_DATE) == TRADE_DATE + timedelta(days=8)
    strict, _, _ = build(config=StrategyConfig(min_days_to_expiry=30))
    assert strict.select_expiry(contracts, TRADE_DATE) is None


def test_plan_selects_target_deltas_with_strict_strike_order():
    strategy, _, journal = build()
    plan = strategy.plan_entry(NOW)
    assert isinstance(plan, EntryPlan)
    strikes = [plan.legs[r].contract.strike for r in STRIKE_ORDER]
    assert strikes == sorted(strikes) and len(set(strikes)) == 4
    for role in STRIKE_ORDER:
        leg = plan.legs[role]
        tol = 0.03 if role.is_long else 0.05
        assert abs(abs(leg.delta) - SPEC.target_delta(role)) <= tol
        assert leg.contract.expiry == EXPIRY
        assert (leg.delta < 0) == (role.option_type is OptionType.PE)
    assert plan.legs[LegRole.SHORT_PUT].contract.strike < plan.forward < plan.legs[LegRole.SHORT_CALL].contract.strike
    assert plan.credit_per_unit_touch > 0
    assert plan.max_loss_per_unit > 0
    assert journal.of("chain_snapshot") and journal.of("entry_plan")


def test_sizing_uses_90_percent_of_funds_and_whole_units():
    strategy, market, _ = build(funds=1_000_000.0, margin_per_lot=50_000.0)
    plan = strategy.plan_entry(NOW)
    assert plan.units == 18  # floor(900000 / 50000)
    assert plan.margin_basis == 900_000.0
    assert plan.allocation_cap == pytest.approx(900_000.0)
    assert plan.margin_basis <= 0.9 * plan.funds


def test_sizing_steps_down_when_full_basket_margin_exceeds_cap():
    strategy, market, _ = build(funds=1_000_000.0, margin_per_lot=50_000.0)
    market.margin_surcharge = lambda lots: 30_000.0 if lots > 1 else 0.0
    plan = strategy.plan_entry(NOW)
    assert plan.units == 17
    assert plan.margin_basis == 17 * 50_000 + 30_000
    assert plan.margin_basis <= 900_000


def test_max_units_cap():
    strategy, _, _ = build(config=StrategyConfig(max_units=2))
    assert strategy.plan_entry(NOW).units == 2


def test_insufficient_funds_skips():
    strategy, _, _ = build(funds=50_000.0, margin_per_lot=50_000.0)
    result = strategy.plan_entry(NOW)
    assert isinstance(result, Skip) and result.reason == "INSUFFICIENT_FUNDS"


def test_no_eligible_expiry_skips():
    clock = SimClock(NOW)
    contracts, quotes = bs_quotes(25000.0, TRADE_DATE + timedelta(days=6), NOW)
    market = FakeMarket(clock, 25000.0, contracts, quotes)
    strategy, _, _ = build(market=market)
    result = strategy.plan_entry(NOW)
    assert isinstance(result, Skip) and result.reason == "NO_ELIGIBLE_EXPIRY"


def test_illiquid_strikes_are_not_selected():
    strategy, market, _ = build()
    first = strategy.plan_entry(NOW)
    chosen = first.legs[LegRole.SHORT_CALL].contract
    bid, ask = market.base[chosen.key]
    market.base[chosen.key] = (bid, ask + 20)  # spread far beyond limits
    second = strategy.plan_entry(NOW)
    assert second.legs[LegRole.SHORT_CALL].contract.key != chosen.key


def test_skips_when_no_strike_near_target_delta():
    strategy, market, _ = build()
    # Remove all puts except deep OTM ones
    market.contracts = [c for c in market.contracts if c.option_type is OptionType.CE or c.strike < 23000]
    result = strategy.plan_entry(NOW)
    assert isinstance(result, Skip)
    assert result.reason in ("NO_STRIKE_NEAR_TARGET_DELTA", "NO_PARITY_FORWARD")


def test_incompatible_lot_sizes_skip():
    clock = SimClock(NOW)
    contracts, quotes = bs_quotes(25000.0, EXPIRY, NOW)
    odd = [make_contract(c.strike, c.option_type, c.expiry, lot=75) if c.option_type is OptionType.CE else c
           for c in contracts]
    quotes = {odd_c.key: quotes[orig.key] for odd_c, orig in zip(odd, contracts)}
    market = FakeMarket(clock, 25000.0, odd, quotes)
    strategy, _, _ = build(market=market)
    result = strategy.plan_entry(NOW)
    assert isinstance(result, Skip) and result.reason == "INCOMPATIBLE_LOT_SIZES"


def test_exit_signal_thresholds():
    strategy, _, _ = build()
    margin = 400_000.0
    assert strategy.exit_signal(999.0, margin, NOW) is None
    assert strategy.exit_signal(1000.0, margin, NOW) is ExitReason.TARGET  # +0.25%
    assert strategy.exit_signal(-1999.0, margin, NOW) is None
    assert strategy.exit_signal(-2000.0, margin, NOW) is ExitReason.STOP_LOSS  # -0.50%
    assert strategy.exit_signal(0.0, margin, ist(TRADE_DATE, time(15, 25))) is ExitReason.TIME


def test_trading_day_calendar():
    strategy, _, _ = build(config=StrategyConfig(holidays=(date(2026, 10, 20),)))
    assert strategy.is_trading_day(TRADE_DATE)
    assert not strategy.is_trading_day(date(2026, 10, 20))
    assert not strategy.is_trading_day(date(2026, 10, 24))  # Saturday


def test_lot_size_constant_matches_helpers():
    strategy, _, _ = build()
    assert strategy.plan_entry(NOW).lot_size == LOT
