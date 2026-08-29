"""Stooq EOD fallback — the floor beneath the Schwab feed (PRP-002).

**The floor is observed, not assumed.** PRP-002 originally called this source
"always available", and `health()` returned DEGRADED unconditionally to match.
That was a promise about a third party, and on 2026-08-29 it stopped being
true: Stooq began refusing plain HTTP clients, answering with HTML rather than
CSV — HTTP 404 to a bare client, or HTTP 200 with a JavaScript proof-of-work
page to a browser-shaped one. Neither is an error, so every quote returned
None while health() still said DEGRADED.

That combination is worse than an outage. Under PRP-002, DEGRADED means "no
new AUTO entries; exits proceed on EOD prices"; DEAD means "place nothing
without explicit human instruction". A source that cannot price anything while
reporting DEGRADED therefore invites exits against prices that do not exist,
carries every position at cost so the drawdown halt can never fire, and prints
a reassuring banner in the journal while doing it.

So health() now reports what this adapter has actually observed. It starts
optimistic — an adapter that has never been asked is not yet known to be down
— and a failed fetch flips it to DEAD until a fetch succeeds again.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime

import httpx

from croupier.data.base import DataHealth, Quote

log = logging.getLogger(__name__)

STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"


class StooqMarketData:
    name = "stooq"

    def __init__(self) -> None:
        # Optimistic until proven otherwise: never having been asked is not
        # the same as having been asked and failed.
        self._reachable = True

    def health(self) -> DataHealth:
        """DEGRADED while this source can serve, DEAD once it cannot.

        Never FRESH — Stooq is end-of-day by definition, so it is a floor and
        not a live feed even when working.
        """
        return DataHealth.DEGRADED if self._reachable else DataHealth.DEAD

    async def quote(self, ticker: str) -> Quote | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(STOOQ_URL.format(sym=ticker.lower()))
        except httpx.HTTPError as exc:
            # Market data never raises into a trading path (PRP-002).
            self._mark_unreachable(f"request failed: {exc}")
            return None

        if resp.status_code != 200 or not resp.text.startswith("Date"):
            # A refusal arrives as HTML with a 200 or a 404, not as an error.
            # Treating it as "this ticker has no data" is what let a dead feed
            # look like a quiet market.
            self._mark_unreachable(
                f"HTTP {resp.status_code}, body starts {resp.text[:32]!r}")
            return None

        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows:
            # Well-formed CSV with no rows is a genuinely empty series for this
            # ticker, not a refusal — the source is answering.
            self._reachable = True
            return None

        self._reachable = True
        last = rows[-1]
        return Quote(ticker=ticker, price=float(last["Close"]),
                     as_of=datetime.fromisoformat(last["Date"]).replace(tzinfo=UTC),
                     source="stooq", health=DataHealth.DEGRADED)

    def _mark_unreachable(self, why: str) -> None:
        if self._reachable:
            log.warning("stooq is not serving data (%s); routing DEAD until it "
                        "answers again — exits must not run on prices that do "
                        "not exist", why)
        self._reachable = False
