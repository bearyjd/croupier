# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Shared fixtures. No test may touch the network: market data is faked."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from croupier.data.base import DataHealth, Quote
from croupier.data.router import DataRouter


class FakeMarketData:
    """In-memory MarketData. A ticker absent from ``prices`` quotes as None."""

    def __init__(self, prices: dict[str, float], health: DataHealth = DataHealth.FRESH):
        self.name = "fake"
        self.prices = dict(prices)
        self._health = health

    def health(self) -> DataHealth:
        return self._health

    async def quote(self, ticker: str) -> Quote | None:
        price = self.prices.get(ticker.upper())
        if price is None:
            return None
        return Quote(ticker=ticker.upper(), price=price,
                     as_of=datetime.now(UTC), source="fake", health=self._health)


@pytest.fixture
def fake_router():
    def _make(prices: dict[str, float], health: DataHealth = DataHealth.FRESH):
        return DataRouter(FakeMarketData(prices, health), FakeMarketData({}, DataHealth.DEAD))
    return _make
