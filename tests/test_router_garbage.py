# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""A garbage quote is a miss: it must fall through to the fallback source."""
import pytest
from conftest import FakeMarketData

from croupier.data.base import DataHealth
from croupier.data.router import DataRouter

BAD = [float("nan"), float("inf"), float("-inf"), 0.0, -1.0]


@pytest.mark.parametrize("bad", BAD)
async def test_a_garbage_primary_quote_falls_back_to_the_secondary(bad):
    router = DataRouter(FakeMarketData({"ACME": bad}),
                        FakeMarketData({"ACME": 2.5}, DataHealth.DEGRADED))
    q = await router.quote("ACME")
    assert q is not None and q.price == 2.5


@pytest.mark.parametrize("bad", BAD)
async def test_garbage_from_every_source_is_a_miss(bad):
    router = DataRouter(FakeMarketData({"ACME": bad}),
                        FakeMarketData({"ACME": bad}, DataHealth.DEGRADED))
    assert await router.quote("ACME") is None
