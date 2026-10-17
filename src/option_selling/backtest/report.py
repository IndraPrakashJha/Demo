"""Backtest report: per-day CSV, summary JSON and a short Markdown summary."""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from ..session import DayOutcome, DayResult


def summarize(results: Sequence[DayResult], missing_weekdays: Sequence[date], synthetic: bool) -> dict:
    traded = [r for r in results if r.outcome is DayOutcome.TRADED and r.net_pnl is not None]
    pnl = [r.net_pnl for r in traded]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    pct = [r.net_pnl / r.basket.margin_basis * 100 for r in traded if r.basket and r.basket.margin_basis]
    return {
        "data_is_synthetic": synthetic,
        "period": {
            "first_day": results[0].trade_date.isoformat() if results else None,
            "last_day": results[-1].trade_date.isoformat() if results else None,
        },
        "days_with_data": len(results),
        "weekdays_without_data": [d.isoformat() for d in missing_weekdays],
        "outcomes": dict(Counter(r.outcome.value for r in results)),
        "skip_reasons": dict(Counter(r.reason for r in results if r.outcome is DayOutcome.SKIPPED)),
        "exit_reasons": dict(Counter(r.reason for r in traded)),
        "halted_days": [r.trade_date.isoformat() for r in results if r.outcome is DayOutcome.HALTED],
        "trades": len(traded),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(traded), 4) if traded else None,
        "net_pnl_total": round(sum(pnl), 2),
        "net_pnl_avg": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "best_day": round(max(pnl), 2) if pnl else None,
        "worst_day": round(min(pnl), 2) if pnl else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "max_drawdown": round(max_dd, 2),
        "avg_net_pct_of_margin": round(sum(pct) / len(pct), 4) if pct else None,
        "charges_total": round(sum(r.basket.charges() for r in results if r.basket), 2),
    }


def write_report(results: Sequence[DayResult], missing_weekdays: Sequence[date], out_dir: Path,
                 synthetic: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [r.to_row() for r in results]
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with open(out_dir / "days.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns or ["date"])
        writer.writeheader()
        writer.writerows(rows)

    summary = summarize(results, missing_weekdays, synthetic)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(_markdown(summary), encoding="utf-8")
    return summary


def _markdown(s: dict) -> str:
    lines = ["# IRON_CONDOR_V1 backtest", ""]
    if s["data_is_synthetic"]:
        lines += ["> **SYNTHETIC DATA. These numbers only exercise the pipeline and say nothing about the strategy.**", ""]
    lines += [
        f"- Period: {s['period']['first_day']} to {s['period']['last_day']}",
        f"- Days with data: {s['days_with_data']} (weekdays without data: {len(s['weekdays_without_data'])})",
        f"- Outcomes: {s['outcomes']}",
        f"- Trades: {s['trades']}, wins {s['wins']}, losses {s['losses']}, win rate {s['win_rate']}",
        f"- Net P&L total: {s['net_pnl_total']} (avg {s['net_pnl_avg']}, avg win {s['avg_win']}, avg loss {s['avg_loss']})",
        f"- Best / worst day: {s['best_day']} / {s['worst_day']}; max drawdown {s['max_drawdown']}",
        f"- Profit factor: {s['profit_factor']}; avg net % of margin: {s['avg_net_pct_of_margin']}",
        f"- Exit reasons: {s['exit_reasons']}",
        f"- Skip reasons: {s['skip_reasons']}",
        f"- Charges total: {s['charges_total']}",
    ]
    if s["halted_days"]:
        lines.append(f"- HALTED days (inspect journals): {s['halted_days']}")
    return "\n".join(lines) + "\n"
