"""Interfaces between strategy/execution code and a broker (real, simulated or historical).

Market and account data are read-only. Order placement lives behind a separate
``OrderGateway`` so a data source can never place orders.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..models import MarginResult, OptionContract, OrderRequest, OrderStatus, Position, Quote


class OrderRejected(Exception):
    """The broker definitively refused the order; no order exists."""


class OrderOutcomeUnknown(Exception):
    """Placement may or may not have reached the exchange (timeout, network error)."""


class MarketData(Protocol):
    def option_contracts(self, underlying: str) -> list[OptionContract]: ...

    def quotes(self, contracts: Sequence[OptionContract]) -> dict[str, Quote]:
        """Quotes keyed by ``contract.key``; contracts without a quote are omitted."""
        ...

    def underlying_quote(self) -> Quote | None: ...


class AccountData(Protocol):
    def available_funds(self) -> float: ...

    def basket_margin(self, orders: Sequence[OrderRequest]) -> MarginResult: ...


class OrderGateway(Protocol):
    def place_order(self, request: OrderRequest) -> str:
        """Returns the broker order id. Raises OrderRejected or OrderOutcomeUnknown."""
        ...

    def order_status(self, order_id: str) -> OrderStatus: ...

    def cancel_order(self, order_id: str) -> None: ...

    def orders_by_tag(self, tag: str) -> list[OrderStatus]: ...

    def positions(self) -> list[Position]: ...
