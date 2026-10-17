"""Strategy interface for the Option Selling System.

A strategy decides *what* to trade and *when to exit*; it never places orders.
Execution, scheduling, persistence and safety live outside the strategy so
future strategies (and a regime engine / strategy selector choosing between
them) plug into the same session and executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Protocol

from ..basket import ExitReason
from ..models import LegRole, OptionContract


@dataclass(frozen=True)
class Skip:
    """A decision not to trade, with a machine-readable reason."""

    reason: str
    details: dict = field(default_factory=dict)


class EntryPlanLike(Protocol):
    strategy: str
    decided_at: datetime
    expiry: date
    lot_size: int
    units: int
    funds: float
    margin_basis: float

    def contracts(self) -> dict[LegRole, OptionContract]: ...

    def to_dict(self) -> dict: ...


class Strategy(Protocol):
    name: str
    entry_time: time
    max_entry_delay_seconds: int
    exit_time: time

    def is_trading_day(self, d: date) -> bool: ...

    def plan_entry(self, now: datetime) -> EntryPlanLike | Skip: ...

    def exit_signal(self, net_pnl: float, margin_basis: float, now: datetime) -> ExitReason | None: ...

    def margin_basis_for_units(self, plan: EntryPlanLike, units: int) -> float: ...
