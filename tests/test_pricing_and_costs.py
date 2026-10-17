import math

import pytest

from option_selling.costs import CostConfig, ZerodhaOptionsCostModel
from option_selling.greeks import black76_price, delta, implied_forward, implied_vol
from option_selling.models import OptionType, Side, round_to_tick


@pytest.mark.parametrize("ot", [OptionType.CE, OptionType.PE])
@pytest.mark.parametrize("strike", [23500, 25000, 26500])
def test_implied_vol_round_trip(ot, strike):
    f, t, r, sigma = 25100.0, 14 / 365, 0.06, 0.14
    price = black76_price(f, strike, t, sigma, r, ot)
    assert implied_vol(price, f, strike, t, r, ot) == pytest.approx(sigma, abs=1e-5)


def test_implied_vol_rejects_prices_outside_bounds():
    assert implied_vol(0.0, 25000, 24000, 0.04, 0.06, OptionType.PE) is None
    # Below intrinsic value
    assert implied_vol(500.0, 25000, 24000, 0.04, 0.06, OptionType.CE) is None


def test_delta_conventions():
    f, t, sigma = 25000.0, 14 / 365, 0.13
    call = delta(f, 26000, t, sigma, OptionType.CE)
    put = delta(f, 24000, t, sigma, OptionType.PE)
    assert 0 < call < 0.5
    assert -0.5 < put < 0
    # Put-call delta relationship at the same strike
    assert delta(f, 25200, t, sigma, OptionType.CE) - delta(f, 25200, t, sigma, OptionType.PE) == pytest.approx(1.0)


def test_parity_forward_recovers_forward():
    f, t, r, sigma = 25080.0, 10 / 365, 0.06, 0.12
    pairs = [(k, black76_price(f, k, t, sigma, r, OptionType.CE), black76_price(f, k, t, sigma, r, OptionType.PE))
             for k in (25000, 25050, 25100)]
    assert implied_forward(pairs, r, t) == pytest.approx(f, abs=1e-6)


def test_order_charges_match_rate_card():
    model = ZerodhaOptionsCostModel(CostConfig())
    sell = model.order_charges(Side.SELL, 1040, 100.0)
    turnover = 104_000
    assert sell.brokerage == 20
    assert sell.stt == pytest.approx(turnover * 0.0015)
    assert sell.stamp == 0
    assert sell.exchange == pytest.approx(turnover * 0.0003503)
    assert sell.gst == pytest.approx(0.18 * (20 + turnover * 0.0003503 + turnover * 0.000001))
    buy = model.order_charges(Side.BUY, 1040, 100.0)
    assert buy.stt == 0
    assert buy.stamp == pytest.approx(turnover * 0.00003)
    assert model.order_charges(Side.BUY, 0, 100.0).total == 0


def test_sliced_charges_count_brokerage_per_order():
    model = ZerodhaOptionsCostModel()
    one = model.order_charges(Side.BUY, 1300, 10.0)
    sliced = model.sliced_charges(Side.BUY, 1300, 10.0, max_order_quantity=650)
    assert sliced.brokerage == 40
    assert sliced.total == pytest.approx(one.total + 20 * 1.18)


def test_round_to_tick():
    assert round_to_tick(10.01, 0.05, up=True) == 10.05
    assert round_to_tick(10.04, 0.05, up=False) == 10.0
    assert round_to_tick(10.05, 0.05, up=True) == 10.05
    assert math.isclose(round_to_tick(0.07, 0.05, up=False), 0.05)
