"""Transaction charges for NSE index options traded through Zerodha.

Rates are configuration, not constants: exchanges and the government revise
them. Verify the defaults against Zerodha's current charges page before
relying on P&L numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Side


@dataclass(frozen=True)
class CostConfig:
    brokerage_per_order: float = 20.0  # flat per executed order for F&O options
    stt_sell_rate: float = 0.0015  # on sell-side premium; 0.15% applies from 1 Apr 2026 (was 0.1%)
    exchange_txn_rate: float = 0.0003503  # NSE options, on premium turnover
    sebi_rate: float = 0.000001  # Rs 10 per crore
    stamp_buy_rate: float = 0.00003  # on buy-side premium
    gst_rate: float = 0.18  # on brokerage + exchange + SEBI charges


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def __add__(self, other: ChargeBreakdown) -> ChargeBreakdown:
        return ChargeBreakdown(
            self.brokerage + other.brokerage,
            self.stt + other.stt,
            self.exchange + other.exchange,
            self.sebi + other.sebi,
            self.stamp + other.stamp,
            self.gst + other.gst,
        )


class ZerodhaOptionsCostModel:
    def __init__(self, config: CostConfig | None = None) -> None:
        self.config = config or CostConfig()

    def order_charges(self, side: Side, quantity: int, price: float) -> ChargeBreakdown:
        """Charges for one executed order (brokerage is per order, not per fill)."""
        if quantity <= 0:
            return ChargeBreakdown()
        c = self.config
        turnover = quantity * price
        brokerage = c.brokerage_per_order
        exchange = turnover * c.exchange_txn_rate
        sebi = turnover * c.sebi_rate
        return ChargeBreakdown(
            brokerage=brokerage,
            stt=turnover * c.stt_sell_rate if side is Side.SELL else 0.0,
            exchange=exchange,
            sebi=sebi,
            stamp=turnover * c.stamp_buy_rate if side is Side.BUY else 0.0,
            gst=(brokerage + exchange + sebi) * c.gst_rate,
        )

    def sliced_charges(
        self, side: Side, quantity: int, price: float, max_order_quantity: int
    ) -> ChargeBreakdown:
        """Charges when ``quantity`` is split into orders of at most ``max_order_quantity``."""
        total = ChargeBreakdown()
        if quantity <= 0:
            return total
        slices = math.ceil(quantity / max_order_quantity)
        remaining = quantity
        for _ in range(slices):
            q = min(remaining, max_order_quantity)
            total = total + self.order_charges(side, q, price)
            remaining -= q
        return total
