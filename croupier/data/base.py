"""Market data protocol (PRP-002).

Health is a first-class return value, never an exception into trading
paths: stale data degrades behavior explicitly instead of crashing exits.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class DataHealth(enum.StrEnum):
    FRESH = "fresh"          # live/near-live quotes
    DEGRADED = "degraded"    # EOD fallback only
    DEAD = "dead"            # no data source available


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float
    as_of: datetime
    source: str              # "schwab" | "stooq"
    health: DataHealth


class MarketData(Protocol):
    name: str

    async def quote(self, ticker: str) -> Quote | None: ...
    def health(self) -> DataHealth: ...
