# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Twelve Data EOD — the floor the whole system stands on (PRP-002 inv. 3).

If this adapter silently returns None, positions go unpriced. Marking then
carries them at cost, which is safe, but exits lose their price signal — so
its failure modes are worth pinning down, and above all the difference between
"this ticker has no data" and "this source is not answering us".

Response shapes are copied from live probes on 2026-08-29, not from the docs.
No network: httpx.MockTransport.
"""
from __future__ import annotations

import time

import httpx
import pytest

from croupier.data.base import DataHealth
from croupier.data.router import DataRouter
from croupier.data.twelvedata import TwelveDataMarketData

OK = {
    "meta": {"symbol": "ACME", "interval": "1day", "exchange": "NASDAQ"},
    "values": [
        # Newest first, as the live feed returns them.
        {"datetime": "2026-08-27", "open": "2.81", "high": "3.05",
         "low": "2.78", "close": "2.96", "volume": "1500000"},
        {"datetime": "2026-08-26", "open": "2.70", "high": "2.90",
         "low": "2.65", "close": "2.81", "volume": "1200000"},
    ],
    "status": "ok",
}


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


def _responds(status=200, json=None):
    return lambda request: httpx.Response(status, json=json or OK)


def _feed():
    # Pacing off by default so tests that make several calls on one instance
    # don't pay real wall-clock time; the pacing behaviour itself is tested
    # explicitly below with it turned back on.
    return TwelveDataMarketData("test-key", min_interval_s=0)


async def test_quote_takes_the_newest_row(mock_http):
    mock_http(_responds())
    q = await _feed().quote("ACME")
    assert q is not None
    assert q.price == pytest.approx(2.96)          # newest, not oldest
    assert q.ticker == "ACME" and q.source == "twelvedata"
    assert q.health == DataHealth.DEGRADED
    assert q.as_of.date().isoformat() == "2026-08-27"
    assert q.as_of.tzinfo is not None               # never naive


async def test_class_share_punctuation_is_left_alone(mock_http):
    """Symbol spelling belongs to the feed, not to the security. Yahoo wants
    `BRK-B`; this feed wants `BRK.B`, verified against a live key. Rewriting
    the dot — as an earlier version did, from a Yahoo-verified rule — turned a
    working symbol into a 404."""
    seen = {}

    def handler(request):
        seen["symbol"] = request.url.params["symbol"]
        return httpx.Response(200, json=OK)

    mock_http(handler)
    await _feed().quote("brk.b")
    assert seen["symbol"] == "BRK.B"


async def test_marking_asks_for_one_bar_not_a_history(mock_http):
    """The free tier is metered: 800 credits a day. Marking needs the latest
    close, so a full series per ticker would be waste with a cost."""
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=OK)

    mock_http(handler)
    await _feed().quote("ACME")
    assert seen["outputsize"] == "1"


async def test_a_missing_key_is_a_configuration_error():
    """It must fail where it can be fixed, never become a source that
    answers 'no data for any ticker'."""
    with pytest.raises(ValueError):
        TwelveDataMarketData("")


async def test_health_is_never_fresh():
    """End-of-day by definition: a floor, not a live feed, even when working."""
    assert _feed().health() == DataHealth.DEGRADED


# --- the floor is observed, not assumed (PRP-002 inv. 3) --------------------

async def test_a_refusal_routes_dead_not_degraded(mock_http):
    """The distinction decides whether exits run.

    Under PRP-002 DEGRADED means "exits proceed on EOD prices" and DEAD means
    "place nothing without explicit human instruction". A source that can
    price nothing while reporting DEGRADED invites exits against prices that
    do not exist, carries every position at cost so the drawdown halt cannot
    fire, and prints a reassuring banner in the journal while doing it.
    """
    mock_http(_responds(401, {"code": 401, "status": "error",
                              "message": "**apikey** parameter is incorrect"}))
    feed = _feed()
    assert feed.health() == DataHealth.DEGRADED       # not yet asked
    assert await feed.quote("SPY") is None
    assert feed.health() == DataHealth.DEAD


# 429 is deliberately absent: it is retried, and has its own tests below.
@pytest.mark.parametrize("code", [500, 403, 999])
async def test_an_unrecognised_error_also_routes_dead(mock_http, code):
    """Quota exhaustion, an outage, or a code we have never seen all mean we
    know nothing. Guessing 'no data' instead is what let a dead feed look
    like a quiet market."""
    mock_http(_responds(400, {"code": code, "status": "error", "message": "new"}))
    feed = _feed()
    await feed.quote("SPY")
    assert feed.health() == DataHealth.DEAD


async def test_a_silent_granularity_downgrade_routes_dead(mock_http):
    """A source may answer a question you did not ask. The rows are
    well-formed; only the declared interval gives it away (PRP-004)."""
    mock_http(_responds(200, {**OK, "meta": {"symbol": "ACME",
                                             "interval": "1week"}}))
    feed = _feed()
    assert await feed.quote("ACME") is None
    assert feed.health() == DataHealth.DEAD


async def test_an_unknown_symbol_is_not_a_refusal(mock_http):
    """The source answered; the answer is that it has no such ticker. Marking
    the floor DEAD would take it down over one delisted name."""
    mock_http(_responds(404, {"code": 404, "status": "error",
                              "message": "symbol not found"}))
    feed = _feed()
    assert await feed.quote("BRCM") is None
    assert feed.health() == DataHealth.DEGRADED


async def test_an_empty_series_is_not_a_refusal(mock_http):
    mock_http(_responds(200, {**OK, "values": []}))
    feed = _feed()
    assert await feed.quote("DELISTED") is None
    assert feed.health() == DataHealth.DEGRADED


async def test_a_transport_error_routes_dead(mock_http):
    def _boom(request):
        raise httpx.ConnectError("no route to host")

    mock_http(_boom)
    feed = _feed()
    assert await feed.quote("SPY") is None
    assert feed.health() == DataHealth.DEAD


async def test_a_dead_feed_recovers_when_it_answers_again(mock_http):
    """Observed, not latched: the floor comes back when the source does.

    One handler that changes its mind, rather than two installs — installing
    the fixture twice chains the factories and the first transport keeps
    answering, which would make this pass without testing anything.
    """
    refusing = True

    def handler(request):
        if refusing:
            return httpx.Response(401, json={"code": 401, "status": "error",
                                             "message": "no"})
        return httpx.Response(200, json=OK)

    mock_http(handler)
    feed = _feed()
    await feed.quote("SPY")
    assert feed.health() == DataHealth.DEAD

    refusing = False
    assert await feed.quote("ACME") is not None
    assert feed.health() == DataHealth.DEGRADED


async def test_a_per_minute_rate_limit_is_waited_out_not_surrendered_to(mock_http):
    """This key is shared with the sidecar that fills the same sleeve. A
    backfill there can rate-limit a mark here; routing the floor DEAD over a
    transient that clears in a minute would halt exits for an hour."""
    limited = True

    def handler(request):
        nonlocal limited
        if limited:
            limited = False
            return httpx.Response(429, json={"code": 429, "status": "error",
                                             "message": "out of API credits "
                                                        "for the current minute"})
        return httpx.Response(200, json=OK)

    mock_http(handler)
    feed = TwelveDataMarketData("test-key", backoff_s=0)
    assert await feed.quote("ACME") is not None
    assert feed.health() == DataHealth.DEGRADED


async def test_a_rate_limit_that_does_not_clear_still_routes_dead(mock_http):
    """The daily ceiling does not clear by waiting."""
    mock_http(_responds(429, {"code": 429, "status": "error",
                              "message": "out of API credits"}))
    feed = TwelveDataMarketData("test-key", backoff_s=0)
    assert await feed.quote("ACME") is None
    assert feed.health() == DataHealth.DEAD


async def test_the_router_floor_goes_dead_with_it(mock_http):
    mock_http(_responds(401, {"code": 401, "status": "error", "message": "no"}))
    feed = _feed()
    router = DataRouter(None, feed)
    await feed.quote("SPY")
    assert router.health() == DataHealth.DEAD


async def test_no_configured_floor_is_dead_not_degraded():
    """An unconfigured deployment has no price source at all. Reporting
    DEGRADED over nothing would let exits run against prices no source is
    providing — the failure invariant 3 was rewritten to prevent."""
    assert DataRouter(None, None).health() == DataHealth.DEAD
    assert await DataRouter(None, None).quote("SPY") is None


async def test_a_plan_gated_symbol_is_logged_not_silent(mock_http, caplog):
    """The real 530-ticker backfill found live large-cap tickers (CTRA, AMI)
    gated behind a paid plan, returning the same HTTP 404 as a genuinely
    unknown symbol. Both leave the floor DEGRADED — one dead ticker is not a
    dead feed either way — but silently discarding which one happened would
    make a coverage gap invisible to whoever reads the logs."""
    mock_http(_responds(404, {"code": 404, "status": "error",
                              "message": "This symbol is available starting "
                                         "with the Pro or Venture plan"}))
    with caplog.at_level("INFO"):
        assert await _feed().quote("CTRA") is None
    assert any("plan-gated" in r.message for r in caplog.records)


# --- non-dict JSON must not raise into a trading path -----------------------

async def test_a_non_dict_json_response_is_a_refusal_not_a_crash(mock_http):
    """The file's own promise is unconditional: 'never raises into a trading
    path'. Anything between us and the vendor — a CDN error page, a WAF
    block — can hand back JSON that isn't the vendor's own shape, and every
    `payload.get(...)` call assumes a dict."""
    # _responds()'s `json or OK` treats an empty list as falsy and would
    # silently substitute a valid payload, so build the response directly.
    mock_http(lambda request: httpx.Response(200, json=[]))  # well-formed, wrong shape
    feed = _feed()
    assert await feed.quote("ACME") is None
    assert feed.health() == DataHealth.DEAD


async def test_a_non_dict_json_after_a_429_retry_is_also_a_refusal(mock_http):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"code": 429, "status": "error",
                                             "message": "rate limited"})
        return httpx.Response(200, json="not an object either")

    mock_http(handler)
    feed = TwelveDataMarketData("test-key", backoff_s=0)
    assert await feed.quote("ACME") is None
    assert feed.health() == DataHealth.DEAD


# --- the router does not pay for a fallback it already knows is dead --------

async def test_the_router_does_not_query_a_fallback_already_known_dead(mock_http):
    """Distinct from `test_the_router_floor_goes_dead_with_it` below: this
    checks quote(), not health() — a second call after the first has already
    proven the source dead must not make a second request to relearn it."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"code": 401, "status": "error",
                                         "message": "bad key"})

    mock_http(handler)
    feed = _feed()
    router = DataRouter(None, feed)

    assert await router.quote("SPY") is None
    assert feed.health() == DataHealth.DEAD
    assert calls["n"] == 1

    assert await router.quote("AAPL") is None
    assert calls["n"] == 1, "router queried a fallback it already knew was dead"


# --- pre-emptive pacing toward the 8/minute ceiling --------------------------

async def test_calls_are_paced_at_least_min_interval_apart(mock_http):
    """marking.py quotes tickers one at a time with no batching, so any
    sleeve past 8 positions hits the 8/minute ceiling in the ordinary case.
    Pre-emptive pacing turns that into a steady per-call wait instead of a
    reactive 61s stall once every 8 calls."""
    mock_http(_responds())
    feed = TwelveDataMarketData("test-key", min_interval_s=0.05)

    t0 = time.monotonic()
    await feed.quote("A")
    await feed.quote("B")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05, f"second call did not wait for the pacing floor: {elapsed}"


async def test_pacing_does_not_stack_with_the_reactive_backoff(mock_http):
    """The two delays serve different purposes and must not compound: pacing
    prevents this process's own calls from tripping the limit; backoff
    recovers from a limit tripped by something else sharing the key."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"code": 429, "status": "error",
                                             "message": "rate limited"})
        return httpx.Response(200, json=OK)

    mock_http(handler)
    feed = TwelveDataMarketData("test-key", min_interval_s=0, backoff_s=0.05)
    t0 = time.monotonic()
    assert await feed.quote("ACME") is not None
    elapsed = time.monotonic() - t0
    assert 0.05 <= elapsed < 0.5, f"expected ~backoff_s, got {elapsed}"
