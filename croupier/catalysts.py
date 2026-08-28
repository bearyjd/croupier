# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Catalyst calendar and the pre-readout freeze.

A typical sleeve protocol reads: "5 trading days before any
binary readout: FREEZE adds, confirm position sizing vs LOSS_TOLERANCE,
re-confirm human approval to hold through event."

A freeze escalates: inside the window a *buy* in that ticker requires human
confirmation whatever mode its sleeve is in. It never de-escalates — a
CONFIRM sleeve stays CONFIRM — and it never touches sells, because an exit
into a catalyst must not need a calendar's permission.

Trading days are counted as weekdays. Market holidays are NOT modelled, so a
holiday inside the run-up shortens the window by a day; widen ``window_start``
in config/catalysts.yaml for events where that matters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

DEFAULT_CATALYST_PATH = Path("config/catalysts.yaml")
DEFAULT_FREEZE_TRADING_DAYS = 5
_SATURDAY = 5


def minus_trading_days(day: date, count: int) -> date:
    """Step back ``count`` weekdays from ``day``. Holidays are not modelled."""
    out = day
    remaining = count
    while remaining > 0:
        out -= timedelta(days=1)
        if out.weekday() < _SATURDAY:
            remaining -= 1
    return out


@dataclass(frozen=True)
class CatalystEvent:
    ticker: str
    event_type: str
    window_start: date
    window_end: date
    source_url: str
    verified: bool = False
    note: str = ""

    def freeze_start(self, trading_days: int) -> date:
        return minus_trading_days(self.window_start, trading_days)

    def is_frozen_on(self, day: date, trading_days: int) -> bool:
        """Frozen from T-N trading days through the end of the event window."""
        return self.freeze_start(trading_days) <= day <= self.window_end

    def describe(self, trading_days: int) -> str:
        return (f"{self.ticker} {self.event_type} "
                f"{self.window_start.isoformat()}..{self.window_end.isoformat()} "
                f"(freeze from {self.freeze_start(trading_days).isoformat()})")


@dataclass(frozen=True)
class CatalystCalendar:
    events: tuple[CatalystEvent, ...] = ()
    freeze_trading_days: int = DEFAULT_FREEZE_TRADING_DAYS

    @classmethod
    def empty(cls) -> CatalystCalendar:
        return cls()

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CATALYST_PATH) -> CatalystCalendar:
        p = Path(path)
        if not p.exists():
            return cls.empty()
        raw = yaml.safe_load(p.read_text()) or {}
        events = tuple(
            CatalystEvent(
                ticker=str(e["ticker"]).upper(),
                event_type=str(e["event_type"]),
                window_start=_as_date(e["window_start"]),
                window_end=_as_date(e["window_end"]),
                source_url=str(e["source_url"]),
                verified=bool(e.get("verified", False)),
                note=str(e.get("note", "")),
            )
            for e in (raw.get("events") or [])
        )
        return cls(
            events=events,
            freeze_trading_days=int(
                raw.get("freeze_trading_days", DEFAULT_FREEZE_TRADING_DAYS)),
        )

    def freeze_for(self, ticker: str, day: date) -> CatalystEvent | None:
        """The soonest-ending active freeze for ``ticker`` on ``day``."""
        active = [
            e for e in self.events
            if e.ticker == ticker.upper() and e.is_frozen_on(day, self.freeze_trading_days)
        ]
        return min(active, key=lambda e: (e.window_end, e.window_start)) if active else None

    def freezes_on(self, day: date) -> tuple[CatalystEvent, ...]:
        return tuple(sorted(
            (e for e in self.events if e.is_frozen_on(day, self.freeze_trading_days)),
            key=lambda e: (e.ticker, e.window_start),
        ))


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
