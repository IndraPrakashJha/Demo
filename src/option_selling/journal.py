"""Append-only JSONL journal of decisions, quotes, orders, fills, P&L and exceptions."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path

from .timeutil import Clock

log = logging.getLogger("option_selling.journal")


def _default(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def to_jsonable(value):
    return json.loads(json.dumps(value, default=_default))


class Journal:
    """Writes one JSON object per line. With ``path=None`` events are kept in memory only."""

    def __init__(self, path: Path | None, clock: Clock, run_id: str, mode: str) -> None:
        self.path = path
        self.clock = clock
        self.run_id = run_id
        self.mode = mode
        self.events: list[dict] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **payload) -> dict:
        entry = {
            "ts": self.clock.now().isoformat(),
            "run_id": self.run_id,
            "mode": self.mode,
            "event": event,
            **payload,
        }
        line = json.dumps(entry, default=_default, sort_keys=False)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        self.events.append(json.loads(line))
        log.debug("%s %s", event, line)
        return entry

    def of(self, event: str) -> list[dict]:
        return [e for e in self.events if e["event"] == event]
