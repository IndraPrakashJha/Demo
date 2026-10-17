"""Command line: ``oss run | backtest | kite-login | live-status | synthetic-data``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from .alerts import CompositeAlerter, FileAlerter, LogAlerter, WebhookAlerter
from .config import AppConfig, load_config
from .costs import ZerodhaOptionsCostModel
from .execution import BasketExecutor
from .journal import Journal
from .safety import LiveTradingBlocked, LiveTradingGuard
from .session import DayOutcome, TradingSession
from .state import DayStateStore
from .strategies.iron_condor_v1 import IronCondorV1
from .timeutil import SystemClock

EXIT_HALTED = 3
EXIT_BLOCKED = 2


def _alerter(config: AppConfig, run_dir: Path) -> CompositeAlerter:
    alerters = [LogAlerter(), FileAlerter(run_dir / "alerts.jsonl")]
    url = os.environ.get(config.alerts.webhook_url_env)
    if url:
        alerters.append(WebhookAlerter(url))
    return CompositeAlerter(alerters)


def cmd_run(args) -> int:
    from .brokers.kite import KiteDataSource, KiteOrderGateway, ReadOnlyKite, build_kite_client
    from .brokers.simulated import SimulatedOrderGateway

    config = load_config(args.config)
    if config.mode not in ("shadow", "live"):
        print("`oss run` needs mode = \"shadow\" or \"live\"; use `oss backtest` for backtests", file=sys.stderr)
        return EXIT_BLOCKED
    clock = SystemClock()
    run_dir = Path(config.paths.run_dir) / config.mode
    today = clock.now().date()
    journal = Journal(run_dir / "journal" / f"{config.strategy.name}_{today.isoformat()}.jsonl", clock,
                      f"{config.mode}-{uuid.uuid4().hex[:8]}", config.mode)
    alerter = _alerter(config, run_dir)

    guard = None
    if config.mode == "live":
        guard = LiveTradingGuard(config.live, config.strategy.name, clock)
        reasons = guard.blocking_reasons()
        if reasons:
            journal.record("live_blocked", reasons=reasons)
            print("Live trading is blocked:\n  - " + "\n  - ".join(reasons), file=sys.stderr)
            return EXIT_BLOCKED

    try:
        kite = build_kite_client(config.kite)
    except RuntimeError as exc:
        print(f"cannot connect to Kite: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    data = KiteDataSource(ReadOnlyKite(kite), config.kite)
    if config.mode == "shadow":
        sim = config.simulation
        gateway = SimulatedOrderGateway(data, clock, sim.fill_model, sim.slippage_pct, sim.slippage_ticks)
    else:
        try:
            gateway = KiteOrderGateway(kite, guard)
        except LiveTradingBlocked as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_BLOCKED

    costs = ZerodhaOptionsCostModel(config.costs)
    strategy = IronCondorV1(config.strategy, config.execution, data, data, costs, journal)
    executor = BasketExecutor(gateway, data, clock, journal, alerter, costs, config.execution)
    session = TradingSession(strategy, executor, data, gateway, clock, journal, alerter,
                             DayStateStore(run_dir / "state"), costs, config.strategy, config.execution,
                             mode=config.mode)
    result = session.run_day()
    print(json.dumps(result.to_row(), indent=2, default=str))
    return EXIT_HALTED if result.outcome is DayOutcome.HALTED else 0


def cmd_backtest(args) -> int:
    from .backtest.engine import run_backtest

    config = load_config(args.config)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=183)
    out = Path(args.out) if args.out else Path(config.paths.run_dir) / "backtest" / f"{start}_{end}"

    def progress(result) -> None:
        row = result.to_row()
        print(f"{row['date']}  {row['outcome']:<10} {row.get('reason') or '':<28} net={row.get('net_pnl')}")

    result = run_backtest(config, Path(args.data), start, end, out, progress=progress)
    print(json.dumps(result.summary, indent=2))
    print(f"report written to {out}")
    return 0


def cmd_kite_login(args) -> int:
    from kiteconnect import KiteConnect

    config = load_config(args.config)
    api_key = os.environ.get(config.kite.api_key_env)
    api_secret = os.environ.get(config.kite.api_secret_env)
    if not api_key or not api_secret:
        print(f"set {config.kite.api_key_env} and {config.kite.api_secret_env}", file=sys.stderr)
        return EXIT_BLOCKED
    kite = KiteConnect(api_key=api_key)
    print("1. Open and log in:", kite.login_url())
    print("2. Copy request_token from the redirect URL.")
    request_token = input("request_token: ").strip()
    session = kite.generate_session(request_token, api_secret=api_secret)
    path = Path(config.kite.access_token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session["access_token"], encoding="utf-8")
    path.chmod(0o600)
    print(f"access token saved to {path} (valid until Kite's daily expiry)")
    return 0


def cmd_live_status(args) -> int:
    config = load_config(args.config)
    reasons = LiveTradingGuard(config.live, config.strategy.name, SystemClock()).blocking_reasons()
    if reasons:
        print("Live trading: BLOCKED\n  - " + "\n  - ".join(reasons))
        return EXIT_BLOCKED
    print("Live trading: all safeguards satisfied")
    return 0


def cmd_synthetic(args) -> int:
    from .backtest.synthetic import generate

    days = generate(Path(args.out), date.fromisoformat(args.start), days=args.days, seed=args.seed)
    print(f"wrote {len(days)} SYNTHETIC days to {args.out} ({days[0]} .. {days[-1]})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oss", description="Option Selling System")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run today's session in the configured mode (shadow or live)")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("backtest", help="replay the strategy over historical minute data")
    p.add_argument("--config", required=True)
    p.add_argument("--data", required=True, help="data directory (see docs/IRON_CONDOR_V1.md)")
    p.add_argument("--start", help="YYYY-MM-DD (default: 183 days before --end)")
    p.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
    p.add_argument("--out", help="report directory")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("kite-login", help="exchange a Kite request_token for today's access token")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_kite_login)

    p = sub.add_parser("live-status", help="show whether live-trading safeguards are satisfied")
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_live_status)

    p = sub.add_parser("synthetic-data", help="write a SYNTHETIC dataset for pipeline testing")
    p.add_argument("--out", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_synthetic)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)
