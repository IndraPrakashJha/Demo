"""Basket state and accounting from broker-confirmed executions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .models import Execution, LegRole, OptionContract


class BasketStatus(str, Enum):
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"  # trade completed, broker positions confirmed flat
    ABORTED = "ABORTED"  # entry abandoned, broker positions confirmed flat
    HALTED = "HALTED"  # automation stopped; manual intervention required


class ExitReason(str, Enum):
    TARGET = "TARGET"
    STOP_LOSS = "STOP_LOSS"
    TIME = "TIME"
    DATA_FAILURE = "DATA_FAILURE"  # marks unavailable too long to enforce the stop


@dataclass
class Basket:
    basket_id: str
    strategy: str
    trade_date: date
    expiry: date
    lot_size: int
    legs: dict[LegRole, OptionContract]
    planned_units: int
    product: str
    units: int = 0
    margin_basis: float = 0.0
    funds: float = 0.0
    status: BasketStatus = BasketStatus.ENTERING
    executions: list[Execution] = field(default_factory=list)
    entered_at: datetime | None = None
    exit_reason: ExitReason | None = None
    exit_started_at: datetime | None = None
    closed_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def record(self, execution: Execution) -> bool:
        """Adds the execution; False if this order id is already recorded."""
        if any(e.order_id == execution.order_id for e in self.executions):
            return False
        self.executions.append(execution)
        return True

    def net_quantity(self, role: LegRole) -> int:
        return sum(e.side.sign * e.quantity for e in self.executions if e.role is role)

    def exposure(self) -> dict[LegRole, int]:
        return {role: self.net_quantity(role) for role in self.legs}

    def is_flat(self) -> bool:
        return all(q == 0 for q in self.exposure().values())

    def cash_flow(self) -> float:
        """Premium received minus premium paid, before charges."""
        return -sum(e.side.sign * e.quantity * e.price for e in self.executions)

    def charges(self) -> float:
        return sum(e.charges for e in self.executions)

    def gross_pnl(self, marks: Mapping[LegRole, float]) -> float:
        value = 0.0
        for role, qty in self.exposure().items():
            if qty:
                value += qty * marks[role]
        return self.cash_flow() + value

    def realized_net_pnl(self) -> float:
        if not self.is_flat():
            raise ValueError("basket still has open quantity")
        return self.cash_flow() - self.charges()

    def entry_executions(self) -> list[Execution]:
        return [e for e in self.executions if e.side is e.role.entry_side]

    def role_for_symbol(self, tradingsymbol: str) -> LegRole | None:
        for role, contract in self.legs.items():
            if contract.tradingsymbol == tradingsymbol:
                return role
        return None

    def to_dict(self) -> dict:
        return {
            "basket_id": self.basket_id,
            "strategy": self.strategy,
            "trade_date": self.trade_date.isoformat(),
            "expiry": self.expiry.isoformat(),
            "lot_size": self.lot_size,
            "legs": {role.value: c.to_dict() for role, c in self.legs.items()},
            "planned_units": self.planned_units,
            "product": self.product,
            "units": self.units,
            "margin_basis": self.margin_basis,
            "funds": self.funds,
            "status": self.status.value,
            "executions": [e.to_dict() for e in self.executions],
            "entered_at": _iso(self.entered_at),
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "exit_started_at": _iso(self.exit_started_at),
            "closed_at": _iso(self.closed_at),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Basket:
        return cls(
            basket_id=data["basket_id"],
            strategy=data["strategy"],
            trade_date=date.fromisoformat(data["trade_date"]),
            expiry=date.fromisoformat(data["expiry"]),
            lot_size=int(data["lot_size"]),
            legs={LegRole(k): OptionContract.from_dict(v) for k, v in data["legs"].items()},
            planned_units=int(data["planned_units"]),
            product=data["product"],
            units=int(data["units"]),
            margin_basis=float(data["margin_basis"]),
            funds=float(data["funds"]),
            status=BasketStatus(data["status"]),
            executions=[Execution.from_dict(e) for e in data["executions"]],
            entered_at=_parse(data.get("entered_at")),
            exit_reason=ExitReason(data["exit_reason"]) if data.get("exit_reason") else None,
            exit_started_at=_parse(data.get("exit_started_at")),
            closed_at=_parse(data.get("closed_at")),
            notes=list(data.get("notes", [])),
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
