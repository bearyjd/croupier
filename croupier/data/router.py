"""Health-aware data routing (PRP-002): schwab FRESH -> stooq DEGRADED -> DEAD.

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
    def __init__(self, primary: MarketData | None, fallback: MarketData):
        self.primary, self.fallback = primary, fallback

    def health(self) -> DataHealth:
        if self.primary is not None and self.primary.health() == DataHealth.FRESH:
            return DataHealth.FRESH
        if self.fallback.health() != DataHealth.DEAD:
            return DataHealth.DEGRADED
        return DataHealth.DEAD

    async def quote(self, ticker: str) -> Quote | None:
        if self.primary is not None and self.primary.health() == DataHealth.FRESH:
            q = await self.primary.quote(ticker)
            if q is not None:
                return q
            log.warning("primary quote miss for %s; falling back", ticker)
        return await self.fallback.quote(ticker)
