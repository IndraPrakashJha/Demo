import json
from datetime import date, time

from option_selling.backtest.data import HistoricalDay, HistoricalMarketData, available_days
from option_selling.backtest.engine import run_backtest
from option_selling.backtest.synthetic import generate
from option_selling.config import AppConfig, BacktestConfig
from option_selling.models import OptionType
from option_selling.session import DayOutcome
from option_selling.timeutil import SimClock, ist


def coarse_config() -> AppConfig:
    return AppConfig(mode="backtest", backtest=BacktestConfig(bar_seconds=300, max_bar_age_seconds=600,
                                                              monitor_interval_seconds=300))


def test_backtest_runs_every_day_and_labels_synthetic_reports(tmp_path):
    data = tmp_path / "data"
    days = generate(data, date(2026, 4, 20), days=3, strike_step=100, bar_seconds=300)
    out = tmp_path / "report"
    result = run_backtest(coarse_config(), data, date(2026, 4, 20), date(2026, 4, 24), out)
    assert [r.trade_date for r in result.days] == days
    assert all(r.outcome in (DayOutcome.TRADED, DayOutcome.SKIPPED) for r in result.days)
    assert result.missing_weekdays == [date(2026, 4, 23), date(2026, 4, 24)]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["data_is_synthetic"] is True
    assert summary["days_with_data"] == 3
    assert "SYNTHETIC" in (out / "summary.md").read_text()
    assert (out / "days.csv").read_text().count("\n") == 4
    assert len(list((out / "journal").glob("*.jsonl"))) == 3
    for r in result.days:
        if r.outcome is DayOutcome.TRADED:
            assert r.basket.is_flat() and r.net_pnl is not None


def _write_day(root, rows_opt, rows_und, d="2026-04-20"):
    (root / "options").mkdir(parents=True)
    (root / "underlying").mkdir(parents=True)
    (root / "options" / f"{d}.csv").write_text("timestamp,expiry,strike,option_type,close\n" + "\n".join(rows_opt) + "\n")
    (root / "underlying" / f"{d}.csv").write_text("timestamp,price\n" + "\n".join(rows_und) + "\n")


def test_bar_start_timestamps_do_not_leak_future_prices(tmp_path):
    _write_day(tmp_path,
               ["2026-04-20 09:59:00,2026-05-05,25000,CE,100", "2026-04-20 10:00:00,2026-05-05,25000,CE,200"],
               ["2026-04-20 09:59:00,24990", "2026-04-20 10:00:00,25010"])
    config = BacktestConfig()
    day = HistoricalDay.load(tmp_path, date(2026, 4, 20), config)
    clock = SimClock(ist(date(2026, 4, 20), time(10, 0)))
    market = HistoricalMarketData(day, clock, config)
    [contract] = market.option_contracts("NIFTY")
    assert contract.option_type is OptionType.CE and contract.lot_size == 65
    assert market.quotes([contract])[contract.key].last == 100  # the 10:00 bar closes at 10:01
    assert market.underlying_quote().last == 24990
    clock.set(ist(date(2026, 4, 20), time(10, 1)))
    assert market.quotes([contract])[contract.key].last == 200
    q = market.quotes([contract])[contract.key]
    assert q.bid < 200 < q.ask  # synthetic spread when the data has no bid/ask


def test_stale_bars_are_not_quoted(tmp_path):
    _write_day(tmp_path, ["2026-04-20 09:15:00,2026-05-05,25000,CE,100"], ["2026-04-20 09:15:00,25000"])
    config = BacktestConfig(max_bar_age_seconds=120)
    day = HistoricalDay.load(tmp_path, date(2026, 4, 20), config)
    market = HistoricalMarketData(day, SimClock(ist(date(2026, 4, 20), time(10, 0))), config)
    [contract] = market.option_contracts("NIFTY")
    assert market.quotes([contract]) == {}
    assert market.underlying_quote() is None


def test_available_days_needs_both_files(tmp_path):
    (tmp_path / "options").mkdir()
    (tmp_path / "underlying").mkdir()
    for d in ("2026-04-20", "2026-04-21"):
        (tmp_path / "options" / f"{d}.csv").write_text("x")
    (tmp_path / "underlying" / "2026-04-20.csv").write_text("x")
    assert available_days(tmp_path, date(2026, 4, 1), date(2026, 4, 30)) == [date(2026, 4, 20)]
