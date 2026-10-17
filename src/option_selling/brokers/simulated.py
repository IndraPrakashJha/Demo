"""Hypothetical order fills for shadow trading and backtests.

``SimulatedOrderGateway`` has no reference to any broker client: it reads
quotes from a ``MarketData`` source and keeps its own orders and positions.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import OrderRequest, OrderState, OrderStatus, Position, Quote, Side, round_to_tick
from ..timeutil import Clock
from .base import MarketData, OrderRejected


@dataclass
class _SimOrder:
    order_id: str
    request: OrderRequest
    filled: int = 0
    notional: float = 0.0
    state: OrderState = OrderState.OPEN
    message: str = ""

    def status(self) -> OrderStatus:
        r = self.request
        return OrderStatus(
            order_id=self.order_id,
            tag=r.tag,
            tradingsymbol=r.contract.tradingsymbol,
            side=r.side,
            quantity=r.quantity,
            filled_quantity=self.filled,
            average_price=self.notional / self.filled if self.filled else 0.0,
            state=self.state,
            message=self.message,
        )


class SimulatedOrderGateway:
    def __init__(
        self,
        market: MarketData,
        clock: Clock,
        fill_model: str = "depth",
        slippage_pct: float = 0.0,
        slippage_ticks: int = 0,
    ) -> None:
        if fill_model not in ("depth", "touch"):
            raise ValueError(f"unknown fill model {fill_model!r}")
        self.market = market
        self.clock = clock
        self.fill_model = fill_model
        self.slippage_pct = slippage_pct
        self.slippage_ticks = slippage_ticks
        self._orders: dict[str, _SimOrder] = {}
        self._positions: dict[tuple[str, str, str], int] = defaultdict(int)
        # Depth consumed per contract for the current quote snapshot, so repeated
        # polls of one snapshot cannot fill against the same liquidity twice.
        self._book: dict[tuple[str, Side], tuple[object, list[list[float]]]] = {}

    # -- OrderGateway -------------------------------------------------------
    def place_order(self, request: OrderRequest) -> str:
        c = request.contract
        if request.quantity <= 0 or request.quantity % c.lot_size:
            raise OrderRejected(f"quantity {request.quantity} is not a positive multiple of lot size {c.lot_size}")
        if request.price <= 0:
            raise OrderRejected("limit price must be positive")
        order_id = f"SIM{uuid.uuid4().hex[:16]}"  # unique across restarts, like broker ids
        order = _SimOrder(order_id, request)
        self._orders[order_id] = order
        self._try_fill(order)
        return order_id

    def order_status(self, order_id: str) -> OrderStatus:
        order = self._orders[order_id]
        if order.state is OrderState.OPEN:
            self._try_fill(order)
        return order.status()

    def cancel_order(self, order_id: str) -> None:
        order = self._orders[order_id]
        if order.state is OrderState.OPEN:
            order.state = OrderState.CANCELLED
            order.message = "cancelled"

    def orders_by_tag(self, tag: str) -> list[OrderStatus]:
        return [o.status() for o in self._orders.values() if o.request.tag == tag]

    def positions(self) -> list[Position]:
        return [Position(sym, exch, product, qty) for (sym, exch, product), qty in self._positions.items()]

    # -- helpers ------------------------------------------------------------
    def restore_positions(self, positions: Iterable[Position]) -> None:
        """Seed positions when a shadow session resumes after a restart."""
        for p in positions:
            self._positions[(p.tradingsymbol, p.exchange, p.product)] = p.quantity

    def _levels(self, key: str, quote: Quote, side: Side) -> list[list[float]]:
        if self.fill_model == "touch":
            price = quote.ask if side is Side.BUY else quote.bid
            return [[price, math.inf]] if price and price > 0 else []
        depth = quote.ask_depth if side is Side.BUY else quote.bid_depth
        identity = (quote.timestamp, side, depth)
        cached = self._book.get((key, side))
        if cached is None or cached[0] != identity:
            levels = [[float(p), float(q)] for p, q in depth if p and p > 0 and q and q > 0]
            self._book[(key, side)] = (identity, levels)
            cached = self._book[(key, side)]
        return cached[1]

    def _slipped(self, price: float, side: Side, tick: float) -> float:
        if side is Side.BUY:
            return round_to_tick(price * (1 + self.slippage_pct) + self.slippage_ticks * tick, tick, up=True)
        return max(tick, round_to_tick(price * (1 - self.slippage_pct) - self.slippage_ticks * tick, tick, up=False))

    def _try_fill(self, order: _SimOrder) -> None:
        r = order.request
        c = r.contract
        quote = self.market.quotes([c]).get(c.key)
        if quote is None:
            return
        remaining = r.quantity - order.filled
        for level in self._levels(c.key, quote, r.side):
            if remaining <= 0:
                break
            price, available = level
            marketable = price <= r.price if r.side is Side.BUY else price >= r.price
            if not marketable:
                break
            take = int(min(remaining, available))
            take -= take % c.lot_size
            if take <= 0:
                continue
            fill_price = self._slipped(price, r.side, c.tick_size)
            # A limit order never fills worse than its limit.
            fill_price = min(fill_price, r.price) if r.side is Side.BUY else max(fill_price, r.price)
            order.filled += take
            order.notional += take * fill_price
            remaining -= take
            level[1] = available - take
            self._positions[(c.tradingsymbol, c.exchange, r.product)] += r.side.sign * take
        if order.filled == r.quantity:
            order.state = OrderState.COMPLETE
