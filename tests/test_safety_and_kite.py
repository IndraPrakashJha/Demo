import json
from datetime import date, datetime, time
from pathlib import Path

import pytest
from kiteconnect import exceptions as kex

from option_selling.brokers.base import OrderOutcomeUnknown, OrderRejected
from option_selling.brokers.kite import KiteDataSource, KiteOrderGateway, ReadOnlyKite
from option_selling.config import ConfigError, KiteConfig, LiveConfig, config_from_dict, load_config
from option_selling.models import OptionType, OrderRequest, OrderState, Side
from option_selling.safety import AUTHORIZATION_ENV, REQUIRED_LIVE_CHECKS, LiveTradingBlocked, LiveTradingGuard
from option_selling.timeutil import SimClock, ist

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 19)
CODE = "auth-code-1234567890"


def verification(tmp_path, **changes) -> Path:
    record = {
        "strategy": "IRON_CONDOR_V1",
        "checks": {name: {"passed": True, "verified_by": "ops", "verified_on": "2026-10-01"} for name in REQUIRED_LIVE_CHECKS},
        "authorization": {"authorized_by": "owner", "authorized_on": "2026-10-02", "valid_until": "2026-12-31",
                          "authorization_code": CODE},
    }
    for key, value in changes.items():
        record[key] = value
    path = tmp_path / "live_verification.json"
    path.write_text(json.dumps(record))
    return path


def guard(path, enabled=True, env=None):
    return LiveTradingGuard(LiveConfig(enabled=enabled, verification_file=str(path)), "IRON_CONDOR_V1",
                            SimClock(ist(TODAY, time(9, 0))), environ={AUTHORIZATION_ENV: CODE} if env is None else env)


class FakeKite:
    def __init__(self):
        self.calls = []
        self.raise_on_place = None

    def place_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        if self.raise_on_place:
            raise self.raise_on_place
        return "220101000000001"

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs))

    def modify_order(self, **kwargs):
        self.calls.append(("modify_order", kwargs))

    def quote(self, keys):
        self.calls.append(("quote", keys))
        return {k: {"last_price": 101.5, "timestamp": datetime(2026, 10, 19, 10, 0, 1),
                    "depth": {"buy": [{"price": 101.0, "quantity": 650, "orders": 3}, {"price": 0, "quantity": 0}],
                              "sell": [{"price": 102.0, "quantity": 325, "orders": 1}]}} for k in keys}

    def instruments(self, exchange):
        return [
            {"instrument_token": 1, "tradingsymbol": "NIFTY26N0325000CE", "name": "NIFTY", "expiry": date(2026, 11, 3),
             "strike": 25000.0, "tick_size": 0.05, "lot_size": 65, "instrument_type": "CE", "exchange": "NFO"},
            {"instrument_token": 2, "tradingsymbol": "NIFTY26NOVFUT", "name": "NIFTY", "expiry": date(2026, 11, 24),
             "strike": 0.0, "tick_size": 0.1, "lot_size": 65, "instrument_type": "FUT", "exchange": "NFO"},
            {"instrument_token": 3, "tradingsymbol": "BANKNIFTY26N0350000PE", "name": "BANKNIFTY",
             "expiry": date(2026, 11, 3), "strike": 50000.0, "tick_size": 0.05, "lot_size": 30,
             "instrument_type": "PE", "exchange": "NFO"},
        ]

    def margins(self, segment):
        return {"net": 812345.5, "available": {"live_balance": 812345.5}}

    def basket_order_margins(self, params, consider_positions=True, mode=None):
        self.calls.append(("basket_order_margins", params, consider_positions))
        return {"initial": {"total": 150000.0}, "final": {"total": 48000.0}}

    def order_history(self, order_id):
        return [{"order_id": order_id, "status": "OPEN", "tradingsymbol": "X", "transaction_type": "BUY",
                 "quantity": 65, "filled_quantity": 0, "average_price": 0},
                {"order_id": order_id, "status": "CANCELLED", "tradingsymbol": "X", "transaction_type": "BUY",
                 "quantity": 130, "filled_quantity": 65, "average_price": 12.35, "tag": "IC1abc"}]


def request(contract=None):
    from helpers import make_contract

    return OrderRequest(contract or make_contract(25000, OptionType.CE), Side.BUY, 65, 10.0, "NRML", "IC1abc")


# -- guard --------------------------------------------------------------------
def test_guard_blocks_by_default(tmp_path):
    reasons = guard(tmp_path / "missing.json", enabled=False).blocking_reasons()
    assert "live.enabled is false" in reasons
    assert any("not found" in r for r in reasons)


def test_guard_allows_only_with_complete_record_and_matching_env(tmp_path):
    path = verification(tmp_path)
    guard(path).assert_live_allowed()
    with pytest.raises(LiveTradingBlocked):
        guard(path, enabled=False).assert_live_allowed()
    with pytest.raises(LiveTradingBlocked):
        guard(path, env={}).assert_live_allowed()
    with pytest.raises(LiveTradingBlocked):
        guard(path, env={AUTHORIZATION_ENV: "wrong-code-0000000"}).assert_live_allowed()


@pytest.mark.parametrize("change", [
    {"strategy": "OTHER"},
    {"checks": {name: {"passed": True, "verified_by": "ops", "verified_on": "x"} for name in REQUIRED_LIVE_CHECKS[1:]}},
    {"authorization": {"authorized_by": "owner", "authorized_on": "2026-10-02", "valid_until": "2026-10-18",
                       "authorization_code": CODE}},
    {"authorization": {"authorized_by": "", "authorized_on": "", "valid_until": "2026-12-31", "authorization_code": CODE}},
])
def test_guard_blocks_incomplete_or_expired_records(tmp_path, change):
    assert guard(verification(tmp_path, **change)).blocking_reasons()


def test_example_verification_template_keeps_live_blocked():
    g = LiveTradingGuard(LiveConfig(enabled=True, verification_file=str(ROOT / "config/live_verification.example.json")),
                         "IRON_CONDOR_V1", SimClock(ist(TODAY, time(9, 0))), environ={})
    assert len(g.blocking_reasons()) > len(REQUIRED_LIVE_CHECKS)


# -- Kite read-only / gateway -------------------------------------------------
def test_read_only_kite_refuses_order_endpoints():
    fake = FakeKite()
    ro = ReadOnlyKite(fake)
    for name in ("place_order", "cancel_order", "modify_order", "exit_order", "place_gtt", "convert_position"):
        with pytest.raises(LiveTradingBlocked):
            getattr(ro, name)
    assert fake.calls == []
    ro.quote(["NFO:X"])
    assert fake.calls[0][0] == "quote"


def test_live_gateway_cannot_be_built_when_blocked(tmp_path):
    with pytest.raises(LiveTradingBlocked):
        KiteOrderGateway(FakeKite(), guard(tmp_path / "missing.json"))


def test_live_gateway_rechecks_guard_before_every_order(tmp_path):
    env = {AUTHORIZATION_ENV: CODE}
    fake = FakeKite()
    gw = KiteOrderGateway(fake, guard(verification(tmp_path), env=env))
    assert gw.place_order(request()) == "220101000000001"
    env.clear()  # authorization withdrawn mid-session
    with pytest.raises(LiveTradingBlocked):
        gw.place_order(request())
    assert [c[0] for c in fake.calls] == ["place_order"]


def test_live_gateway_refuses_read_only_client(tmp_path):
    with pytest.raises(TypeError):
        KiteOrderGateway(ReadOnlyKite(FakeKite()), guard(verification(tmp_path)))


@pytest.mark.parametrize("error, expected", [
    (kex.NetworkException("timeout"), OrderOutcomeUnknown),
    (kex.GeneralException("502"), OrderOutcomeUnknown),
    (TimeoutError("read timeout"), OrderOutcomeUnknown),
    (kex.InputException("bad price"), OrderRejected),
    (kex.OrderException("rejected"), OrderRejected),
])
def test_live_gateway_classifies_placement_errors(tmp_path, error, expected):
    fake = FakeKite()
    fake.raise_on_place = error
    gw = KiteOrderGateway(fake, guard(verification(tmp_path)))
    with pytest.raises(expected):
        gw.place_order(request())
    assert len(fake.calls) == 1  # never retried internally


def test_live_gateway_reads_final_order_state(tmp_path):
    gw = KiteOrderGateway(FakeKite(), guard(verification(tmp_path)))
    status = gw.order_status("1")
    assert status.state is OrderState.CANCELLED
    assert status.filled_quantity == 65 and status.average_price == 12.35


def test_kite_data_source_parses_instruments_quotes_funds_and_margin():
    fake = FakeKite()
    data = KiteDataSource(ReadOnlyKite(fake), KiteConfig())
    contracts = data.option_contracts("NIFTY")
    assert [c.tradingsymbol for c in contracts] == ["NIFTY26N0325000CE"]
    c = contracts[0]
    assert (c.expiry, c.strike, c.lot_size, c.option_type) == (date(2026, 11, 3), 25000.0, 65, OptionType.CE)
    q = data.quotes(contracts)[c.key]
    assert (q.bid, q.ask, q.ask_depth) == (101.0, 102.0, ((102.0, 325),))
    assert q.timestamp.utcoffset().total_seconds() == 19800
    assert data.available_funds() == 812345.5
    margin = data.basket_margin([request(c)])
    assert margin.total == 48000.0
    assert fake.calls[-1][2] is False  # consider_positions off by default


# -- configuration ------------------------------------------------------------
def test_example_config_loads_in_shadow_mode_with_live_disabled():
    config = load_config(ROOT / "config/iron_condor_v1.example.toml")
    assert config.mode == "shadow"
    assert config.live.enabled is False
    assert config.strategy.min_days_to_expiry == 10
    assert config.backtest.session_start == time(9, 15)


def test_config_rejects_unknown_keys_and_bad_values():
    with pytest.raises(ConfigError, match="unknown key"):
        config_from_dict({"strategy": {"profit_target_pct": 1.0}})
    with pytest.raises(ConfigError, match="mode"):
        config_from_dict({"mode": "paper"})
    with pytest.raises(ConfigError):
        config_from_dict({"strategy": {"min_days_to_expiry": "ten"}})
