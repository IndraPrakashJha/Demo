"""Replays IRON_CONDOR_V1 over historical days using the live session and executor code."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..alerts import MemoryAlerter
from ..brokers.simulated import SimulatedOrderGateway
from ..config import AppConfig
from ..costs import ZerodhaOptionsCostModel
from ..execution import BasketExecutor
from ..journal import Journal
from ..session import DayResult, TradingSession
from ..state import DayStateStore
from ..strategies.iron_condor_v1 import SPEC, IronCondorV1
from ..timeutil import SimClock, ist
from .data import BacktestAccount, HistoricalDay, HistoricalMarketData, available_days
from .report import write_report

log = logging.getLogger("option_selling.backtest")

SYNTHETIC_MARKER = "SYNTHETIC_DATA"


@dataclass
class BacktestResult:
    days: list[DayResult]
    missing_weekdays: list[date]
    summary: dict
    out_dir: Path


def run_day(config: AppConfig, data_dir: Path, d: date, journal_dir: Path | None, run_id: str) -> DayResult:
    bt = config.backtest
    day = HistoricalDay.load(data_dir, d, bt)
    clock = SimClock(ist(d, bt.session_start))
    market = HistoricalMarketData(day, clock, bt, underlying=SPEC.underlying, exchange=config.kite.exchange)
    account = BacktestAccount(bt, market)
    gateway = SimulatedOrderGateway(market, clock, bt.fill_model, bt.slippage_pct, bt.slippage_ticks)
    journal = Journal(journal_dir / f"{d.isoformat()}.jsonl" if journal_dir else None, clock, run_id, "backtest")
    alerter = MemoryAlerter()
    costs = ZerodhaOptionsCostModel(config.costs)
    strategy = IronCondorV1(config.strategy, config.execution, market, account, costs, journal)
    executor = BasketExecutor(gateway, market, clock, journal, alerter, costs, config.execution)
    session = TradingSession(
        strategy, executor, market, gateway, clock, journal, alerter, DayStateStore(None), costs,
        config.strategy, config.execution, mode="backtest", monitor_interval_seconds=bt.monitor_interval_seconds,
    )
    return session.run_day()


def run_backtest(
    config: AppConfig,
    data_dir: Path,
    start: date,
    end: date,
    out_dir: Path,
    progress: Callable[[DayResult], None] | None = None,
) -> BacktestResult:
    days = available_days(data_dir, start, end)
    run_id = f"bt-{uuid.uuid4().hex[:8]}"
    journal_dir = out_dir / "journal"
    results = []
    for d in days:
        result = run_day(config, data_dir, d, journal_dir, run_id)
        results.append(result)
        if progress:
            progress(result)
    have = set(days)
    missing = [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if (start + timedelta(days=i)).weekday() < 5 and (start + timedelta(days=i)) not in have
    ]
    synthetic = (data_dir / SYNTHETIC_MARKER).exists()
    summary = write_report(results, missing, out_dir, synthetic=synthetic)
    return BacktestResult(results, missing, summary, out_dir)
