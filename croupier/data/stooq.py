"""Stooq EOD fallback — free, no auth, always available (PRP-002 inv. 3)."""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import httpx

from croupier.data.base import DataHealth, Quote

STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"


class StooqMarketData:
    name = "stooq"

    def health(self) -> DataHealth:
        return DataHealth.DEGRADED

    async def quote(self, ticker: str) -> Quote | None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(STOOQ_URL.format(sym=ticker.lower()))
        if resp.status_code != 200 or not resp.text.startswith("Date"):
            return None
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows:
            return None
        last = rows[-1]
        return Quote(ticker=ticker, price=float(last["Close"]),
                     as_of=datetime.fromisoformat(last["Date"]).replace(tzinfo=UTC),
                     source="stooq", health=DataHealth.DEGRADED)
