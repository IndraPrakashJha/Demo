"""Per-day persisted state enforcing one logical basket entry per trading day.

``entry_attempted`` is written before any entry work starts, so a crash or
restart can never produce a second attempt on the same day.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class DayState:
    strategy: str
    trade_date: date
    entry_attempted: bool = False
    entry_attempted_at: str | None = None
    outcome: str | None = None
    reason: str | None = None
    basket: dict | None = None
    halted: bool = False
    halt_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "trade_date": self.trade_date.isoformat(),
            "entry_attempted": self.entry_attempted,
            "entry_attempted_at": self.entry_attempted_at,
            "outcome": self.outcome,
            "reason": self.reason,
            "basket": self.basket,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DayState:
        return cls(
            strategy=data["strategy"],
            trade_date=date.fromisoformat(data["trade_date"]),
            entry_attempted=bool(data.get("entry_attempted")),
            entry_attempted_at=data.get("entry_attempted_at"),
            outcome=data.get("outcome"),
            reason=data.get("reason"),
            basket=data.get("basket"),
            halted=bool(data.get("halted")),
            halt_reason=data.get("halt_reason"),
        )


class DayStateStore:
    """JSON file per strategy and day; ``directory=None`` keeps state in memory (backtests)."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self._memory: dict[tuple[str, date], dict] = {}
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def _path(self, strategy: str, d: date) -> Path:
        assert self.directory is not None
        return self.directory / f"{strategy}_{d.isoformat()}.json"

    def load(self, strategy: str, d: date) -> DayState:
        if self.directory is None:
            data = self._memory.get((strategy, d))
            return DayState.from_dict(data) if data else DayState(strategy, d)
        path = self._path(strategy, d)
        if not path.exists():
            return DayState(strategy, d)
        return DayState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, state: DayState) -> None:
        data = state.to_dict()
        if self.directory is None:
            self._memory[(state.strategy, state.trade_date)] = json.loads(json.dumps(data))
            return
        path = self._path(state.strategy, state.trade_date)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
