"""Configuration loading (TOML) and validation.

The fixed IRON_CONDOR_V1 rules (deltas, times, allocation ceiling, target and
stop) live in ``strategies.iron_condor_v1.SPEC`` and cannot be changed from
configuration. Configuration covers the minimum days to expiry, implementation
tolerances, execution mechanics, charges and environment wiring.

Unknown keys are rejected so a typo cannot silently fall back to a default.
"""

from __future__ import annotations

import dataclasses
import tomllib
import typing
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

from .costs import CostConfig

MODES = ("shadow", "live", "backtest")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "IRON_CONDOR_V1"
    min_days_to_expiry: int = 10
    # How far the chosen strike's |delta| may be from the target before the day is skipped.
    short_delta_tolerance: float = 0.05
    long_delta_tolerance: float = 0.03
    risk_free_rate: float = 0.06
    # Only strikes within this fraction of the underlying are quoted and considered.
    strike_window_pct: float = 0.15
    # A strike is tradeable when ask - bid <= max(max_spread_abs, max_spread_pct * mid).
    max_spread_abs: float = 0.5
    max_spread_pct: float = 0.05
    max_quote_age_seconds: float = 30.0
    # "exit_side" marks longs at bid and shorts at ask (what closing would get); "mid" uses mids.
    mark_method: str = "exit_side"
    monitor_interval_seconds: float = 5.0
    # Consecutive failed marks (no quotes / data errors) before a DATA_FAILURE exit.
    max_consecutive_mark_failures: int = 12
    mtm_journal_interval_seconds: float = 60.0
    # Optional hard cap on basket units (0 = only the funds-based limit applies).
    max_units: int = 0
    holidays: tuple[date, ...] = ()


@dataclass(frozen=True)
class ExecutionConfig:
    # NRML avoids racing Zerodha's MIS auto square-off around the 15:25 time exit.
    product: str = "NRML"
    # Limit price = touch price moved this fraction through the market.
    limit_buffer_pct: float = 0.02
    # Extra buffer added per attempt when trimming or unwinding.
    retry_buffer_step_pct: float = 0.03
    fill_timeout_seconds: float = 20.0
    poll_interval_seconds: float = 1.0
    cancel_confirm_timeout_seconds: float = 10.0
    unknown_order_reconcile_seconds: float = 15.0
    position_sync_timeout_seconds: float = 15.0
    max_rebalance_attempts: int = 5
    rebalance_retry_seconds: float = 3.0
    # Orders are sliced to at most this many lots; keep it below the exchange freeze quantity.
    max_order_lots: int = 20
    tag_prefix: str = "IC1"


@dataclass(frozen=True)
class SimulationConfig:
    """Hypothetical fills used in shadow mode."""

    fill_model: str = "depth"  # "depth" walks the quoted book; "touch" fills fully at best price
    slippage_pct: float = 0.0
    slippage_ticks: int = 0


@dataclass(frozen=True)
class KiteConfig:
    api_key_env: str = "KITE_API_KEY"
    api_secret_env: str = "KITE_API_SECRET"
    access_token_env: str = "KITE_ACCESS_TOKEN"
    access_token_file: str = "runs/.kite_access_token"
    exchange: str = "NFO"
    underlying_quote_key: str = "NSE:NIFTY 50"
    # Field of kite.margins("equity") used as broker-confirmed available funds.
    funds_field: str = "net"
    margin_consider_positions: bool = False


@dataclass(frozen=True)
class PathsConfig:
    run_dir: str = "runs"


@dataclass(frozen=True)
class AlertsConfig:
    webhook_url_env: str = "OSS_ALERT_WEBHOOK_URL"


@dataclass(frozen=True)
class LiveConfig:
    enabled: bool = False
    verification_file: str = "config/live_verification.json"


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 1_000_000.0
    lot_size: int = 65  # used when the data has no lot_size column
    tick_size: float = 0.05
    # "start": a bar stamped 10:00 closes at 10:00 + bar_seconds (vendor norm); "end": stamped at close.
    bar_timestamp: str = "start"
    bar_seconds: int = 60
    max_bar_age_seconds: float = 180.0
    # Spread synthesised around close when the data has no bid/ask columns.
    synthetic_spread_pct: float = 0.01
    fill_model: str = "touch"
    slippage_pct: float = 0.0
    slippage_ticks: int = 1
    # Approximate basket margin = widest wing * qty * margin_wing_factor + margin_exposure_pct * spot * qty.
    margin_wing_factor: float = 1.0
    margin_exposure_pct: float = 0.0
    monitor_interval_seconds: float = 60.0
    session_start: time = time(9, 15)


@dataclass(frozen=True)
class AppConfig:
    mode: str = "shadow"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    kite: KiteConfig = field(default_factory=KiteConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    def validate(self) -> None:
        errors: list[str] = []
        s, e, b = self.strategy, self.execution, self.backtest
        if self.mode not in MODES:
            errors.append(f"mode must be one of {MODES}, got {self.mode!r}")
        if s.name != "IRON_CONDOR_V1":
            errors.append(f"unknown strategy {s.name!r}")
        if s.min_days_to_expiry < 0:
            errors.append("strategy.min_days_to_expiry must be >= 0")
        for name in ("short_delta_tolerance", "long_delta_tolerance"):
            if not 0 < getattr(s, name) < 0.25:
                errors.append(f"strategy.{name} must be in (0, 0.25)")
        if not 0 < s.strike_window_pct < 0.5:
            errors.append("strategy.strike_window_pct must be in (0, 0.5)")
        if s.mark_method not in ("exit_side", "mid"):
            errors.append("strategy.mark_method must be 'exit_side' or 'mid'")
        if s.monitor_interval_seconds <= 0:
            errors.append("strategy.monitor_interval_seconds must be > 0")
        if s.max_units < 0:
            errors.append("strategy.max_units must be >= 0")
        if e.product not in ("NRML", "MIS"):
            errors.append("execution.product must be NRML or MIS")
        if not 0 <= e.limit_buffer_pct < 0.5:
            errors.append("execution.limit_buffer_pct must be in [0, 0.5)")
        if e.max_order_lots < 1:
            errors.append("execution.max_order_lots must be >= 1")
        if e.max_rebalance_attempts < 1:
            errors.append("execution.max_rebalance_attempts must be >= 1")
        if not (e.tag_prefix.isalnum() and len(e.tag_prefix) <= 6):
            errors.append("execution.tag_prefix must be alphanumeric, at most 6 characters")
        for fm in (self.simulation.fill_model, b.fill_model):
            if fm not in ("depth", "touch"):
                errors.append(f"fill_model must be 'depth' or 'touch', got {fm!r}")
        if b.bar_timestamp not in ("start", "end"):
            errors.append("backtest.bar_timestamp must be 'start' or 'end'")
        if b.capital <= 0:
            errors.append("backtest.capital must be > 0")
        if errors:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))


def _coerce(value, annotation, where: str):
    origin = typing.get_origin(annotation)
    if annotation is time:
        if isinstance(value, time):
            return value
        return time.fromisoformat(str(value))
    if annotation is date:
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    if origin is tuple:
        (inner, _ellipsis) = typing.get_args(annotation)
        return tuple(_coerce(v, inner, where) for v in value)
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number")
        return float(value)
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer")
        return value
    if annotation is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false")
        return value
    if annotation is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
        return value
    return value


def _build(cls, data: dict, section: str):
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, value in data.items():
        annotation = hints[name]
        if dataclasses.is_dataclass(annotation):
            kwargs[name] = _build(annotation, value, name)
        else:
            kwargs[name] = _coerce(value, annotation, f"{section}.{name}")
    return cls(**kwargs)


def config_from_dict(data: dict) -> AppConfig:
    config = _build(AppConfig, data, "root")
    config.validate()
    return config


def load_config(path: str | Path) -> AppConfig:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return config_from_dict(data)
