"""Mark-to-market of an open basket, net of charges paid and estimated exit charges."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .basket import Basket
from .costs import ZerodhaOptionsCostModel
from .models import LegRole, Quote, Side


@dataclass(frozen=True)
class PnlSnapshot:
    at: datetime
    gross: float
    charges_paid: float
    estimated_exit_charges: float
    net: float
    marks: dict[LegRole, float]

    def to_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "gross": round(self.gross, 2),
            "charges_paid": round(self.charges_paid, 2),
            "estimated_exit_charges": round(self.estimated_exit_charges, 2),
            "net": round(self.net, 2),
            "marks": {r.value: p for r, p in self.marks.items()},
        }


def mark_basket(
    basket: Basket,
    quotes: Mapping[str, Quote],
    method: str,
    costs: ZerodhaOptionsCostModel,
    max_order_quantity: int,
    now: datetime,
    max_quote_age_seconds: float | None,
) -> PnlSnapshot | None:
    """``None`` when any open leg cannot be marked (missing, stale or one-sided quote)."""
    marks: dict[LegRole, float] = {}
    exit_charges = 0.0
    for role, qty in basket.exposure().items():
        if qty == 0:
            continue
        quote = quotes.get(basket.legs[role].key)
        if quote is None:
            return None
        if max_quote_age_seconds and quote.timestamp is not None:
            if (now - quote.timestamp).total_seconds() > max_quote_age_seconds:
                return None
        if method == "mid":
            price = quote.mid
        else:
            # What closing would get: sell longs at bid, buy back shorts at ask.
            price = quote.bid if qty > 0 else quote.ask
            if qty > 0 and not price:
                price = 0.0  # no bid on a long wing: it is worth nothing to close
        if price is None or (qty < 0 and price <= 0):
            return None
        marks[role] = price
        exit_side = Side.SELL if qty > 0 else Side.BUY
        exit_charges += costs.sliced_charges(exit_side, abs(qty), price, max_order_quantity).total
    gross = basket.gross_pnl(marks)
    paid = basket.charges()
    return PnlSnapshot(now, gross, paid, exit_charges, gross - paid - exit_charges, marks)
