"""IST time helpers and clock abstractions.

Everything that waits or reads the time goes through a ``Clock`` so the same
session code runs against the wall clock (shadow/live) and a simulated clock
(backtests and tests).
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SECONDS_PER_YEAR = 365.0 * 24 * 3600


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall clock in IST."""

    def now(self) -> datetime:
        return datetime.now(IST)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


class SimClock:
    """Manually advanced clock; ``sleep`` moves time forward instantly."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock requires a timezone-aware start time")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        self._now = value


def ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def as_ist(value: datetime) -> datetime:
    """Attach IST to naive datetimes (Kite returns naive IST timestamps)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def wait_until(clock: Clock, target: datetime, max_chunk_seconds: float = 30.0) -> None:
    while True:
        remaining = (target - clock.now()).total_seconds()
        if remaining <= 0:
            return
        clock.sleep(min(remaining, max_chunk_seconds))


def year_fraction(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / SECONDS_PER_YEAR
