# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Stooq EOD fallback — the floor the whole system stands on (PRP-002 inv. 3).

If this adapter silently returns None, positions go unpriced. Marking then
carries them at cost, which is safe, but exits lose their price signal — so
its failure modes are worth pinning down. No network: httpx.MockTransport.
"""
from __future__ import annotations

import httpx
import pytest

from croupier.data.base import DataHealth
from croupier.data.stooq import StooqMarketData

CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-08-26,2.70,2.90,2.65,2.81,1200000\n"
    "2026-08-27,2.81,3.05,2.78,2.96,1500000\n"
)


@pytest.fixture
def mock_http(monkeypatch):
    def _install(handler):
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        def factory(**kwargs):
            kwargs.pop("transport", None)
            return real(transport=transport, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
    return _install


def _responds(status=200, text=CSV):
    return lambda request: httpx.Response(status, text=text)


async def test_quote_parses_the_last_row(mock_http):
    mock_http(_responds())
    q = await StooqMarketData().quote("ACME")
    assert q is not None
    assert q.price == pytest.approx(2.96)          # last row, not first
    assert q.ticker == "ACME" and q.source == "stooq"
    assert q.health == DataHealth.DEGRADED
    assert q.as_of.date().isoformat() == "2026-08-27"
    assert q.as_of.tzinfo is not None               # never naive


async def test_ticker_is_lowercased_into_the_url(mock_http):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text=CSV)

    mock_http(handler)
    await StooqMarketData().quote("ACME")
    assert "s=acme.us" in seen["url"]


@pytest.mark.parametrize("status,text", [
    (404, CSV),                       # not found
    (500, CSV),                       # upstream error
    (200, "<html>blocked</html>"),    # rate-limited HTML, not CSV
    (200, "Date,Open,High,Low,Close,Volume\n"),   # header only, no rows
])
async def test_bad_responses_return_none_rather_than_raising(mock_http, status, text):
    mock_http(_responds(status, text))
    assert await StooqMarketData().quote("ACME") is None


async def test_health_is_always_degraded():
    """Stooq is EOD by definition; it is never the FRESH source."""
    assert StooqMarketData().health() == DataHealth.DEGRADED


# --- the floor is observed, not assumed (PRP-002 inv. 3) --------------------

async def test_a_refusal_routes_dead_not_degraded(mock_http):
    """The distinction decides whether exits run.

    Stooq answers a refused client with HTML and a 200 or a 404, never an
    error. health() used to return DEGRADED unconditionally, which under
    PRP-002 means "exits proceed on EOD prices" — against prices that do not
    exist, with every position carried at cost so the drawdown halt could
    never fire, and a reassuring DEGRADED banner in the journal.
    """
    mock_http(_responds(404, "<meta charset=utf-8><title>Stooq</title>gone"))
    feed = StooqMarketData()
    assert feed.health() == DataHealth.DEGRADED       # not yet asked
    assert await feed.quote("SPY") is None
    assert feed.health() == DataHealth.DEAD


async def test_a_proof_of_work_page_also_routes_dead(mock_http):
    """The other shape of the same refusal, served to a browser-like client."""
    mock_http(_responds(200, "<!DOCTYPE html>This site requires JavaScript"))
    feed = StooqMarketData()
    await feed.quote("SPY")
    assert feed.health() == DataHealth.DEAD


async def test_the_router_floor_goes_dead_with_it(mock_http):
    from croupier.data.router import DataRouter
    mock_http(_responds(404, "<html>nope</html>"))
    feed = StooqMarketData()
    router = DataRouter(None, feed)
    await feed.quote("SPY")
    assert router.health() == DataHealth.DEAD


async def test_a_working_feed_stays_degraded_and_recovers(mock_http):
    """DEGRADED is the healthy state here — Stooq is end-of-day by definition,
    so it is a floor, never FRESH."""
    mock_http(_responds())
    feed = StooqMarketData()
    assert await feed.quote("ACME") is not None
    assert feed.health() == DataHealth.DEGRADED


async def test_an_empty_series_is_not_a_refusal(mock_http):
    """Well-formed CSV with no rows means this ticker has no data — the source
    is answering. Marking it DEAD would take the whole floor down over one
    delisted name."""
    mock_http(_responds(200, "Date,Open,High,Low,Close,Volume\n"))
    feed = StooqMarketData()
    assert await feed.quote("DELISTED") is None
    assert feed.health() == DataHealth.DEGRADED


async def test_a_transport_error_routes_dead(mock_http):
    import httpx as _httpx

    def _boom(request):
        raise _httpx.ConnectError("no route to host")

    mock_http(_boom)
    feed = StooqMarketData()
    assert await feed.quote("SPY") is None            # never raises into trading
    assert feed.health() == DataHealth.DEAD
