"""Core domain types shared by strategies, execution, shadow trading and backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


def round_to_tick(price: float, tick: float, up: bool) -> float:
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return round(steps * tick, 2)


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class LegRole(str, Enum):
    LONG_PUT = "LONG_PUT"
    SHORT_PUT = "SHORT_PUT"
    SHORT_CALL = "SHORT_CALL"
    LONG_CALL = "LONG_CALL"

    @property
    def option_type(self) -> OptionType:
        return OptionType.PE if self in (LegRole.LONG_PUT, LegRole.SHORT_PUT) else OptionType.CE

    @property
    def is_long(self) -> bool:
        return self in (LegRole.LONG_PUT, LegRole.LONG_CALL)

    @property
    def sign(self) -> int:
        """+1 for legs held long, -1 for legs held short."""
        return 1 if self.is_long else -1

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self.is_long else Side.SELL

    @property
    def pair(self) -> LegRole:
        """The other leg on the same side of the condor (long wing <-> short leg)."""
        return _PAIRS[self]


_PAIRS = {
    LegRole.LONG_PUT: LegRole.SHORT_PUT,
    LegRole.SHORT_PUT: LegRole.LONG_PUT,
    LegRole.SHORT_CALL: LegRole.LONG_CALL,
    LegRole.LONG_CALL: LegRole.SHORT_CALL,
}

# Strikes must be strictly increasing in this order.
STRIKE_ORDER = (LegRole.LONG_PUT, LegRole.SHORT_PUT, LegRole.SHORT_CALL, LegRole.LONG_CALL)
PROTECTIVE_LEGS = (LegRole.LONG_PUT, LegRole.LONG_CALL)
SHORT_LEGS = (LegRole.SHORT_PUT, LegRole.SHORT_CALL)


@dataclass(frozen=True)
class OptionContract:
    tradingsymbol: str
    exchange: str
    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    lot_size: int
    tick_size: float = 0.05
    instrument_token: int | None = None

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    def to_dict(self) -> dict:
        return {
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type.value,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "instrument_token": self.instrument_token,
        }

    @classmethod
    def from_dict(cls, data: dict) -> OptionContract:
        return cls(
            tradingsymbol=data["tradingsymbol"],
            exchange=data["exchange"],
            underlying=data["underlying"],
            expiry=date.fromisoformat(data["expiry"]),
            strike=float(data["strike"]),
            option_type=OptionType(data["option_type"]),
            lot_size=int(data["lot_size"]),
            tick_size=float(data["tick_size"]),
            instrument_token=data.get("instrument_token"),
        )


@dataclass(frozen=True)
class Quote:
    bid: float | None
    ask: float | None
    last: float | None = None
    timestamp: datetime | None = None
    # Market depth as (price, quantity) levels, best first.
    bid_depth: tuple[tuple[float, int], ...] = ()
    ask_depth: tuple[tuple[float, int], ...] = ()

    @property
    def two_sided(self) -> bool:
        return bool(self.bid and self.ask and self.bid > 0 and self.ask >= self.bid)

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.two_sided else None

    @property
    def spread(self) -> float | None:
        return self.ask - self.bid if self.two_sided else None


@dataclass(frozen=True)
class OrderRequest:
    contract: OptionContract
    side: Side
    quantity: int
    price: float  # limit price
    product: str
    tag: str


class OrderState(str, Enum):
    OPEN = "OPEN"  # accepted, not yet in a final state
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"  # may carry a partial fill
    REJECTED = "REJECTED"

    @property
    def terminal(self) -> bool:
        return self is not OrderState.OPEN


@dataclass(frozen=True)
class OrderStatus:
    order_id: str
    tag: str | None
    tradingsymbol: str
    side: Side
    quantity: int
    filled_quantity: int
    average_price: float
    state: OrderState
    message: str = ""


@dataclass(frozen=True)
class Position:
    tradingsymbol: str
    exchange: str
    product: str
    quantity: int  # signed net quantity


@dataclass(frozen=True)
class MarginResult:
    total: float
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Execution:
    """The broker-confirmed final fill of one order."""

    role: LegRole
    order_id: str
    side: Side
    quantity: int
    price: float
    timestamp: datetime
    charges: float

    def to_dict(self) -> dict:
        return {
            "role": self.role.value,
            "order_id": self.order_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "timestamp": self.timestamp.isoformat(),
            "charges": self.charges,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Execution:
        return cls(
            role=LegRole(data["role"]),
            order_id=data["order_id"],
            side=Side(data["side"]),
            quantity=int(data["quantity"]),
            price=float(data["price"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            charges=float(data["charges"]),
        )
