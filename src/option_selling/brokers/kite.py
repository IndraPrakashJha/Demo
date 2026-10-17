"""Zerodha Kite Connect adapters.

* ``ReadOnlyKite``      - wraps a KiteConnect client and exposes only read endpoints.
* ``KiteDataSource``    - genuine Kite market data, funds and basket margins (read-only).
* ``KiteOrderGateway``  - real order placement, guarded by ``LiveTradingGuard`` on every call.

Kite returns naive IST timestamps and parsed ``date`` expiries; both are
normalised here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from ..config import KiteConfig
from ..models import MarginResult, OptionContract, OptionType, OrderRequest, OrderState, OrderStatus, Position, Quote, Side
from ..safety import LiveTradingBlocked, LiveTradingGuard
from ..timeutil import as_ist
from .base import OrderOutcomeUnknown, OrderRejected

log = logging.getLogger("option_selling.kite")

QUOTE_BATCH = 500  # Kite's per-request instrument limit for quote()


class ReadOnlyKite:
    """Proxy that refuses every Kite method not on the read-only allowlist."""

    ALLOWED = frozenset({
        "instruments", "quote", "ltp", "ohlc", "margins", "basket_order_margins", "order_margins",
        "positions", "orders", "order_history", "order_trades", "trades", "holdings", "profile",
        "historical_data",
    })

    def __init__(self, kite) -> None:
        object.__setattr__(self, "_kite", kite)

    def __getattr__(self, name: str):
        if name not in ReadOnlyKite.ALLOWED:
            raise LiveTradingBlocked([f"Kite method {name!r} is not available on a read-only client"])
        return getattr(object.__getattribute__(self, "_kite"), name)

    def __setattr__(self, name, value) -> None:
        raise AttributeError("ReadOnlyKite is immutable")


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _depth(levels) -> tuple[tuple[float, int], ...]:
    return tuple(
        (float(lvl["price"]), int(lvl["quantity"]))
        for lvl in levels or ()
        if lvl.get("price") and lvl.get("quantity")
    )


def parse_kite_quote(raw: dict) -> Quote:
    depth = raw.get("depth") or {}
    bids = _depth(depth.get("buy"))
    asks = _depth(depth.get("sell"))
    ts = raw.get("timestamp") or raw.get("last_trade_time")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return Quote(
        bid=bids[0][0] if bids else None,
        ask=asks[0][0] if asks else None,
        last=float(raw["last_price"]) if raw.get("last_price") is not None else None,
        timestamp=as_ist(ts) if isinstance(ts, datetime) else None,
        bid_depth=bids,
        ask_depth=asks,
    )


class KiteDataSource:
    """MarketData + AccountData over a (read-only) Kite client."""

    def __init__(self, kite, config: KiteConfig) -> None:
        self.kite = kite
        self.config = config
        self._contracts: dict[str, list[OptionContract]] = {}

    def option_contracts(self, underlying: str) -> list[OptionContract]:
        if underlying not in self._contracts:
            rows = self.kite.instruments(self.config.exchange)
            self._contracts[underlying] = [
                OptionContract(
                    tradingsymbol=r["tradingsymbol"],
                    exchange=r["exchange"],
                    underlying=r["name"],
                    expiry=_to_date(r["expiry"]),
                    strike=float(r["strike"]),
                    option_type=OptionType(r["instrument_type"]),
                    lot_size=int(r["lot_size"]),
                    tick_size=float(r["tick_size"]),
                    instrument_token=int(r["instrument_token"]),
                )
                for r in rows
                if r.get("name") == underlying and r.get("instrument_type") in ("CE", "PE")
            ]
        return self._contracts[underlying]

    def quotes(self, contracts: Sequence[OptionContract]) -> dict[str, Quote]:
        keys = [c.key for c in contracts]
        out: dict[str, Quote] = {}
        for i in range(0, len(keys), QUOTE_BATCH):
            raw = self.kite.quote(keys[i:i + QUOTE_BATCH])
            for key, value in raw.items():
                out[key] = parse_kite_quote(value)
        return out

    def underlying_quote(self) -> Quote | None:
        raw = self.kite.quote([self.config.underlying_quote_key]).get(self.config.underlying_quote_key)
        return parse_kite_quote(raw) if raw else None

    def available_funds(self) -> float:
        margins = self.kite.margins("equity")
        return float(margins[self.config.funds_field])

    def basket_margin(self, orders: Sequence[OrderRequest]) -> MarginResult:
        params = [
            {
                "exchange": o.contract.exchange,
                "tradingsymbol": o.contract.tradingsymbol,
                "transaction_type": o.side.value,
                "variety": "regular",
                "product": o.product,
                "order_type": "LIMIT",
                "quantity": o.quantity,
                "price": o.price,
                "trigger_price": 0,
            }
            for o in orders
        ]
        raw = self.kite.basket_order_margins(params, consider_positions=self.config.margin_consider_positions)
        return MarginResult(total=float(raw["final"]["total"]), raw=raw)


_KITE_TERMINAL = {"COMPLETE": OrderState.COMPLETE, "CANCELLED": OrderState.CANCELLED, "REJECTED": OrderState.REJECTED}


def _kite_status(row: dict) -> OrderStatus:
    return OrderStatus(
        order_id=str(row["order_id"]),
        tag=row.get("tag"),
        tradingsymbol=row["tradingsymbol"],
        side=Side(row["transaction_type"]),
        quantity=int(row["quantity"]),
        filled_quantity=int(row.get("filled_quantity") or 0),
        average_price=float(row.get("average_price") or 0.0),
        state=_KITE_TERMINAL.get(str(row.get("status")).upper(), OrderState.OPEN),
        message=row.get("status_message") or "",
    )


class KiteOrderGateway:
    """Real Kite orders. Every mutating call re-checks the live-trading guard first."""

    def __init__(self, kite, guard: LiveTradingGuard, variety: str = "regular") -> None:
        guard.assert_live_allowed()
        if isinstance(kite, ReadOnlyKite):
            raise TypeError("KiteOrderGateway needs a full KiteConnect client")
        self._kite = kite
        self._guard = guard
        self._variety = variety

    def place_order(self, request: OrderRequest) -> str:
        self._guard.assert_live_allowed()
        from kiteconnect import exceptions as kex

        try:
            order_id = self._kite.place_order(
                variety=self._variety,
                exchange=request.contract.exchange,
                tradingsymbol=request.contract.tradingsymbol,
                transaction_type=request.side.value,
                quantity=request.quantity,
                product=request.product,
                order_type="LIMIT",
                price=request.price,
                validity="DAY",
                tag=request.tag,
            )
        except (kex.InputException, kex.OrderException, kex.PermissionException, kex.TokenException) as exc:
            raise OrderRejected(str(exc)) from exc
        except Exception as exc:  # network errors, timeouts, 5xx: the order may exist
            raise OrderOutcomeUnknown(str(exc)) from exc
        return str(order_id)

    def order_status(self, order_id: str) -> OrderStatus:
        history = self._kite.order_history(order_id)
        if not history:
            raise LookupError(f"no history for order {order_id}")
        return _kite_status(history[-1])

    def cancel_order(self, order_id: str) -> None:
        self._guard.assert_live_allowed()
        try:
            self._kite.cancel_order(variety=self._variety, order_id=order_id)
        except Exception as exc:  # e.g. already complete; the caller re-reads the status
            log.warning("cancel of %s failed: %s", order_id, exc)

    def orders_by_tag(self, tag: str) -> list[OrderStatus]:
        return [_kite_status(o) for o in self._kite.orders() if o.get("tag") == tag]

    def positions(self) -> list[Position]:
        rows = self._kite.positions()["net"]
        return [Position(r["tradingsymbol"], r["exchange"], r["product"], int(r["quantity"])) for r in rows]


def load_access_token(config: KiteConfig) -> str:
    token = os.environ.get(config.access_token_env)
    if token:
        return token.strip()
    path = Path(config.access_token_file)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        f"no Kite access token: set {config.access_token_env} or run `oss kite-login` to create {path}"
    )


def build_kite_client(config: KiteConfig):
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise RuntimeError("kiteconnect is not installed: pip install 'option-selling-system[kite]'") from exc
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise RuntimeError(f"environment variable {config.api_key_env} is not set")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(load_access_token(config))
    return kite
