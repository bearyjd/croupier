# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Twelve Data EOD — the floor beneath the Schwab feed (PRP-002 inv. 3).

Replaces Stooq, which began refusing plain HTTP clients on 2026-08-29 and has
no path back: it answers a browser-shaped client with a JavaScript
proof-of-work page, and solving that is not something a well-behaved consumer
of a free source does. The full account is in PRP-002 invariant 3; the choice
of replacement, the candidates measured, and why not Yahoo, are in PRP-004.

**The floor is observed, not assumed.** health() reports what this adapter has
seen. It starts optimistic — an adapter that has never been asked is not yet
known to be down — a refusal flips it to DEAD, and a successful fetch flips it
back. That distinction carries weight: under PRP-002, DEGRADED means "no new
AUTO entries, exits proceed on EOD prices", while DEAD means "place nothing
without explicit human instruction". A source that cannot price anything while
reporting DEGRADED invites exits against prices that do not exist.

**A refusal is not an absence.** A well-formed answer with no rows means this
ticker has no data and the source is healthy; an auth failure, a quota
exhaustion or an unrecognised error means we know nothing at all. Anything not
explicitly "no such symbol" is treated as a refusal, because guessing the
other way is what made a dead feed look like a quiet market.

Never FRESH: this is end-of-day data, so it is a floor and not a live feed
even when it is working perfectly.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from croupier.data.base import DataHealth, Quote

log = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com/time_series"
INTERVAL = "1day"
SYMBOL_NOT_FOUND = 404


class TwelveDataMarketData:
    name = "twelvedata"

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            # A missing key is a deployment fault and is raised where it can be
            # fixed. It must never become a source that answers "no data".
            raise ValueError("TWELVEDATA_API_KEY is required")
        self._key = api_key
        self._timeout = timeout
        self._reachable = True

    def health(self) -> DataHealth:
        """DEGRADED while this source can serve, DEAD once it cannot."""
        return DataHealth.DEGRADED if self._reachable else DataHealth.DEAD

    async def quote(self, ticker: str) -> Quote | None:
        # Marking needs the latest bar, not a history: one credit per ticker.
        params = {
            "symbol": _resolve(ticker),
            "interval": INTERVAL,
            "outputsize": "1",
            "apikey": self._key,
            "format": "JSON",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(BASE_URL, params=params)
            payload = resp.json()
        except httpx.HTTPError as exc:
            # Market data never raises into a trading path (PRP-002).
            self._mark_unreachable(f"request failed: {exc}")
            return None
        except ValueError as exc:
            self._mark_unreachable(f"unparseable response: {exc}")
            return None

        if payload.get("status") == "error":
            code = payload.get("code")
            message = str(payload.get("message", ""))[:200]
            if code == SYMBOL_NOT_FOUND:
                # The source answered; the answer is that it has no such
                # ticker. One dead ticker is not a dead feed.
                self._reachable = True
                return None
            self._mark_unreachable(f"HTTP {code}: {message}")
            return None

        meta = payload.get("meta") or {}
        if meta.get("interval") != INTERVAL:
            # A source may answer a question you did not ask; a downgraded
            # granularity is well-formed and wrong (PRP-004).
            self._mark_unreachable(
                f"asked for {INTERVAL!r}, got {meta.get('interval')!r}")
            return None

        values = payload.get("values") or []
        self._reachable = True
        if not values:
            # Well-formed and empty is a genuinely empty series for this
            # ticker, not a refusal — the source is answering.
            return None

        latest = values[0]                 # newest first
        try:
            price = float(latest["close"])
            as_of = datetime.fromisoformat(str(latest["datetime"])[:10])
        except (KeyError, TypeError, ValueError) as exc:
            self._mark_unreachable(f"unreadable row {latest!r}: {exc}")
            return None

        return Quote(ticker=ticker, price=price,
                     as_of=as_of.replace(tzinfo=UTC),
                     source=self.name, health=DataHealth.DEGRADED)

    def _mark_unreachable(self, why: str) -> None:
        if self._reachable:
            log.warning("twelvedata is not serving data (%s); routing DEAD "
                        "until it answers again — exits must not run on prices "
                        "that do not exist", why)
        self._reachable = False


def _resolve(ticker: str) -> str:
    """Filed ticker -> quoted ticker.

    A dot never appears in a bare US ticker; it only ever separates a class
    suffix, which this feed spells with a dash (BRK.B -> BRK-B). Croupier sees
    tickers from sleeve configs rather than from disclosures, so it needs the
    punctuation rule but not Filature's rename map.
    """
    return ticker.strip().upper().replace(".", "-")
