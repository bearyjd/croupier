# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Schwab market-data adapter and its token lifecycle (PRP-002).

The 7-day refresh-token hard expiry is load-bearing: `auth-status`, the
T-24h re-auth nag, and the DEGRADED fallback all key off it. In particular
the refresh lifetime must NOT reset when an access token is renewed — if it
did, the nag would never fire and a lapse would surprise the operator over a
catalyst weekend. That subtlety is the main thing these tests pin.

No network: httpx.MockTransport throughout.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from croupier.data.base import DataHealth
from croupier.data.schwab import (
    REFRESH_TOKEN_LIFETIME,
    SchwabMarketData,
    TokenState,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


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


def _tokens(tmp_path, *, issued_days_ago=1.0, access_valid=True,
            base: datetime | None = None) -> TokenState:
    """Write a token file.

    Defaults to real wall-clock: the adapter calls datetime.now() itself, so
    tokens pinned to a fixed NOW would read as long expired and every quote
    would take the refresh path.
    """
    base = base or datetime.now(UTC)
    issued = base - timedelta(days=issued_days_ago)
    access_exp = base + timedelta(minutes=30) if access_valid else base - timedelta(minutes=1)
    t = TokenState("access-abc", "refresh-xyz", access_exp, issued)
    t.save(tmp_path / "tok.json")
    return t


def _adapter(tmp_path) -> SchwabMarketData:
    return SchwabMarketData("key", "secret", tmp_path / "tok.json")


# --- TokenState ------------------------------------------------------------

def test_token_file_is_written_owner_only(tmp_path):
    """It holds a live credential; it must not be group- or world-readable."""
    _tokens(tmp_path)
    assert (tmp_path / "tok.json").stat().st_mode & 0o077 == 0

def test_round_trips_through_disk(tmp_path):
    _tokens(tmp_path)
    loaded = TokenState.load(tmp_path / "tok.json")
    assert loaded is not None
    assert loaded.access_token == "access-abc" and loaded.refresh_token == "refresh-xyz"

@pytest.mark.parametrize("body", ['{"bad": "shape"}', "not json at all", ""])
def test_unreadable_token_file_loads_as_none(tmp_path, body):
    p = tmp_path / "tok.json"
    p.write_text(body)
    assert TokenState.load(p) is None

def test_missing_token_file_loads_as_none(tmp_path):
    assert TokenState.load(tmp_path / "nope.json") is None

def test_refresh_expiry_is_seven_days_after_issue(tmp_path):
    t = _tokens(tmp_path, issued_days_ago=0, base=NOW)
    assert t.refresh_expires_at == NOW + REFRESH_TOKEN_LIFETIME
    assert t.days_until_reauth(NOW) == pytest.approx(7.0)
    assert t.days_until_reauth(NOW + timedelta(days=6.5)) == pytest.approx(0.5)
    assert t.days_until_reauth(NOW + timedelta(days=8)) < 0     # negative when lapsed


# --- health ----------------------------------------------------------------

def test_health_is_dead_without_tokens(tmp_path):
    assert _adapter(tmp_path).health() == DataHealth.DEAD

def test_health_is_dead_once_the_refresh_token_expired(tmp_path):
    _tokens(tmp_path, issued_days_ago=8)
    assert _adapter(tmp_path).health() == DataHealth.DEAD

def test_health_is_fresh_while_the_refresh_token_lives(tmp_path):
    _tokens(tmp_path, issued_days_ago=1)
    assert _adapter(tmp_path).health() == DataHealth.FRESH


# --- quotes ----------------------------------------------------------------

def _quote_body(price=3.21, key="lastPrice", ticker="ACME"):
    return {ticker: {"quote": {key: price}}}

async def test_quote_uses_a_valid_access_token_without_refreshing(tmp_path, mock_http):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=_quote_body())

    mock_http(handler)
    _tokens(tmp_path)
    q = await _adapter(tmp_path).quote("ACME")
    assert q is not None and q.price == pytest.approx(3.21)
    assert q.source == "schwab" and q.health == DataHealth.FRESH
    assert all("oauth/token" not in u for u in calls), "should not have refreshed"

async def test_quote_falls_back_to_mark_when_lastprice_absent(tmp_path, mock_http):
    mock_http(lambda r: httpx.Response(200, json=_quote_body(key="mark")))
    _tokens(tmp_path)
    q = await _adapter(tmp_path).quote("ACME")
    assert q is not None and q.price == pytest.approx(3.21)

@pytest.mark.parametrize("status,payload", [
    (401, {}),                          # token rejected
    (500, {"error": "boom"}),           # upstream error
    (200, {"ACME": {"quote": {}}}),     # 200 but no usable price
    (200, {}),                          # 200 but ticker absent
])
async def test_quote_returns_none_rather_than_raising(tmp_path, mock_http,
                                                      status, payload):
    """Market data never raises into a trading path (PRP-002)."""
    mock_http(lambda r: httpx.Response(status, json=payload))
    _tokens(tmp_path)
    assert await _adapter(tmp_path).quote("ACME") is None

async def test_quote_is_none_when_refresh_token_has_lapsed(tmp_path, mock_http):
    mock_http(lambda r: httpx.Response(200, json=_quote_body()))
    _tokens(tmp_path, issued_days_ago=8)
    assert await _adapter(tmp_path).quote("ACME") is None


# --- the refresh subtlety --------------------------------------------------

async def test_expired_access_token_triggers_a_refresh(tmp_path, mock_http):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "oauth/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "new-token",
                                             "expires_in": 1800})
        return httpx.Response(200, json=_quote_body())

    mock_http(handler)
    _tokens(tmp_path, access_valid=False)
    a = _adapter(tmp_path)
    q = await a.quote("ACME")
    assert q is not None
    assert any("oauth/token" in u for u in seen)
    assert a._tokens.access_token == "new-token"

async def test_refreshing_does_not_extend_the_seven_day_lifetime(tmp_path, mock_http):
    """The nag depends on this: a refresh must not silently renew the clock."""
    def handler(request):
        if "oauth/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "new", "expires_in": 1800})
        return httpx.Response(200, json=_quote_body())

    mock_http(handler)
    original = _tokens(tmp_path, issued_days_ago=6, access_valid=False)
    a = _adapter(tmp_path)
    await a.quote("ACME")
    assert a._tokens.refresh_issued_at == original.refresh_issued_at
    assert a._tokens.refresh_expires_at == original.refresh_expires_at

async def test_failed_refresh_degrades_instead_of_raising(tmp_path, mock_http):
    mock_http(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    _tokens(tmp_path, access_valid=False)
    assert await _adapter(tmp_path).quote("ACME") is None
