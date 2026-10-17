"""Live-trading safeguards.

Real orders can only be sent through ``brokers.kite.KiteOrderGateway``, which
re-evaluates ``LiveTradingGuard`` on construction and before every order
action. The guard fails closed: live trading is allowed only when ALL hold:

1. ``[live] enabled = true`` in the configuration;
2. the verification file exists, names this strategy, records every check in
   ``REQUIRED_LIVE_CHECKS`` as passed with a verifier and date, and carries an
   explicit authorization (who, when, valid-until date not in the past);
3. the environment variable ``OSS_LIVE_AUTHORIZATION`` equals that file's
   ``authorization_code``.

There is no flag, CLI option or strategy hook that skips these checks.
Shadow mode never constructs a live gateway, and its Kite client is wrapped
in ``ReadOnlyKite`` so order endpoints are unreachable from it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from .config import LiveConfig
from .timeutil import Clock

AUTHORIZATION_ENV = "OSS_LIVE_AUTHORIZATION"

REQUIRED_LIVE_CHECKS = (
    "shadow_sessions_reviewed",
    "order_lifecycle_tested",
    "partial_fill_trim_and_unwind_tested",
    "unknown_order_outcome_reconciliation_tested",
    "flat_confirmation_tested",
    "manual_intervention_alerting_tested",
    "charges_and_margin_reconciled_with_contract_notes",
    "risk_limits_and_sizing_reviewed",
)


class LiveTradingBlocked(PermissionError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("live trading is blocked: " + "; ".join(reasons))


class LiveTradingGuard:
    def __init__(
        self,
        config: LiveConfig,
        strategy: str,
        clock: Clock,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.clock = clock
        self.environ = os.environ if environ is None else environ

    def blocking_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.config.enabled:
            reasons.append("live.enabled is false")
        path = Path(self.config.verification_file)
        if not path.exists():
            reasons.append(f"verification file {path} not found")
            return reasons
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"verification file unreadable: {exc}")
            return reasons
        if record.get("strategy") != self.strategy:
            reasons.append(f"verification file is for {record.get('strategy')!r}, not {self.strategy!r}")
        checks = record.get("checks") or {}
        for name in REQUIRED_LIVE_CHECKS:
            check = checks.get(name) or {}
            if check.get("passed") is not True or not check.get("verified_by") or not check.get("verified_on"):
                reasons.append(f"safety check {name!r} not recorded as passed")
        auth = record.get("authorization") or {}
        if not auth.get("authorized_by") or not auth.get("authorized_on"):
            reasons.append("explicit authorization (authorized_by / authorized_on) missing")
        try:
            valid_until = date.fromisoformat(str(auth.get("valid_until")))
            if valid_until < self.clock.now().date():
                reasons.append(f"authorization expired on {valid_until}")
        except ValueError:
            reasons.append("authorization.valid_until missing or not a YYYY-MM-DD date")
        code = auth.get("authorization_code")
        if not code or len(str(code)) < 12:
            reasons.append("authorization_code missing or shorter than 12 characters")
        elif self.environ.get(AUTHORIZATION_ENV) != code:
            reasons.append(f"{AUTHORIZATION_ENV} does not match the recorded authorization_code")
        return reasons

    def assert_live_allowed(self) -> None:
        reasons = self.blocking_reasons()
        if reasons:
            raise LiveTradingBlocked(reasons)
