"""Operator alerting. Alert delivery failures are logged, never raised."""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from .journal import to_jsonable

log = logging.getLogger("option_selling.alerts")


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"  # manual intervention required


class Alerter(Protocol):
    def alert(self, level: AlertLevel, title: str, details: dict | None = None) -> None: ...


class LogAlerter:
    _LEVELS = {AlertLevel.INFO: logging.INFO, AlertLevel.WARNING: logging.WARNING, AlertLevel.CRITICAL: logging.CRITICAL}

    def alert(self, level: AlertLevel, title: str, details: dict | None = None) -> None:
        log.log(self._LEVELS[level], "[%s] %s %s", level.value, title, json.dumps(to_jsonable(details or {})))


class FileAlerter:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def alert(self, level: AlertLevel, title: str, details: dict | None = None) -> None:
        entry = {"ts": datetime.now().astimezone().isoformat(), "level": level.value, "title": title,
                 "details": to_jsonable(details or {})}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


class WebhookAlerter:
    """POSTs ``{"text": ..., "level": ..., "details": ...}`` as JSON (Slack-style incoming webhooks)."""

    def __init__(self, url: str, timeout_seconds: float = 5.0, min_level: AlertLevel = AlertLevel.WARNING) -> None:
        self.url = url
        self.timeout = timeout_seconds
        self.min_level = min_level

    def alert(self, level: AlertLevel, title: str, details: dict | None = None) -> None:
        order = list(AlertLevel)
        if order.index(level) < order.index(self.min_level):
            return
        body = json.dumps({"text": f"[{level.value}] {title}", "level": level.value,
                           "details": to_jsonable(details or {})}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout):
            pass


class MemoryAlerter:
    def __init__(self) -> None:
        self.alerts: list[tuple[AlertLevel, str, dict]] = []

    def alert(self, level: AlertLevel, title: str, details: dict | None = None) -> None:
        self.alerts.append((level, title, details or {}))

    def levels(self) -> list[AlertLevel]:
        return [a[0] for a in self.alerts]


class CompositeAlerter:
    def __init__(self, alerters: Sequence[Alerter]) -> None:
        self.alerters = list(alerters)

    def alert(self, level: AlertLevel, title: str, details: dict | None = None) -> None:
        for alerter in self.alerters:
            try:
                alerter.alert(level, title, details)
            except Exception:  # an alert channel failing must not stop risk handling
                log.exception("alert delivery failed via %s", type(alerter).__name__)
