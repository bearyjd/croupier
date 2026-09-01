"""Health-aware data routing (PRP-002): schwab FRESH -> EOD DEGRADED -> DEAD.

Trading semantics of each state (enforced by venue/data gates + AGENT.md):
  FRESH    — normal operation
  DEGRADED — no new AUTO entries; exits allowed and flagged; CONFIRM orders
             carry the health banner
  DEAD     — nothing trades except explicit human instructions
"""
from __future__ import annotations

import logging

from croupier.data.base import DataHealth, MarketData, Quote

log = logging.getLogger(__name__)


class DataRouter:
    """Routes to the freshest source that will actually answer.

    The fallback is optional because it can be genuinely absent: it needs an
    API key now (PRP-004), and an unconfigured deployment has no floor at all.
    That state is DEAD and must say so. Defaulting to DEGRADED instead would
    let exits run against prices no source is providing — the failure PRP-002
    invariant 3 was rewritten to prevent.
    """

    def __init__(self, primary: MarketData | None, fallback: MarketData | None):
        self.primary, self.fallback = primary, fallback

    def health(self) -> DataHealth:
        if self.primary is not None and self.primary.health() == DataHealth.FRESH:
            return DataHealth.FRESH
        if self.fallback is not None and self.fallback.health() != DataHealth.DEAD:
            return DataHealth.DEGRADED
        return DataHealth.DEAD

    async def quote(self, ticker: str) -> Quote | None:
        if self.primary is not None and self.primary.health() == DataHealth.FRESH:
            q = await self.primary.quote(ticker)
            if q is not None:
                return q
            log.warning("primary quote miss for %s; falling back", ticker)
        if self.fallback is None or self.fallback.health() == DataHealth.DEAD:
            # Already proven this source will not answer; paying for a request
            # to re-learn that is not "observed, not assumed" — it is a
            # request spent on a question already answered. The next call
            # that queries health() elsewhere still re-observes normally.
            return None
        return await self.fallback.quote(ticker)
