# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""PRP-002: the Schwab adapter is read-only, and CI proves it.

`croupier/data/schwab.py` claims in its docstring to contain "no
order-related code paths". While the developer app is registered with the
Market Data product only, that claim is belt-and-braces — Schwab itself
refuses to place an order. If the Accounts & Trading product is ever granted,
the docstring becomes the load-bearing guarantee, and a docstring cannot fail
a build. These tests can.

They inspect the module's syntax tree rather than its text, so prose about
orders (in docstrings and comments, which the module has) never trips them,
and a real order path cannot hide behind a comment.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from croupier.data import schwab
from croupier.gates.pipeline import check
from croupier.models import AccountSnapshot, OrderIntent
from croupier.policy import load

REPO = Path(__file__).resolve().parent.parent
SCHWAB_SRC = Path(schwab.__file__).read_text()
SCHWAB_AST = ast.parse(SCHWAB_SRC)

# Substrings that would indicate a trading surface rather than a data one.
ORDER_WORDS = ("order", "trade", "buy", "sell", "place", "cancel",
               "replace", "position", "account", "transaction")
# Schwab's trading surface lives under these path prefixes.
TRADING_PATHS = ("/trader/", "/accounts", "/orders", "/v1/accounts")


def _string_constants() -> list[str]:
    return [n.value for n in ast.walk(SCHWAB_AST)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _defined_names() -> list[str]:
    out = []
    for n in ast.walk(SCHWAB_AST):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.append(n.name)
    return out


def test_module_reaches_only_market_data_endpoints():
    urls = [s for s in _string_constants() if s.startswith("http")]
    assert urls, "expected the adapter to declare at least one endpoint"
    for url in urls:
        assert url.startswith("https://api.schwabapi.com/"), url
        assert "marketdata" in url or "oauth/token" in url, (
            f"{url} is neither a market-data nor a token endpoint")


def test_module_declares_no_trading_endpoint():
    for s in _string_constants():
        if s.startswith("http") or s.startswith("/"):
            for bad in TRADING_PATHS:
                assert bad not in s, f"trading path {bad!r} appears in {s!r}"


def test_module_defines_no_order_shaped_callable():
    """Prose about orders is fine; a function named for one is not."""
    for name in _defined_names():
        low = name.lower()
        assert not any(w in low for w in ORDER_WORDS), (
            f"{name!r} looks like a trading surface in a read-only adapter")


def test_public_attributes_expose_no_trading_surface():
    for name in dir(schwab.SchwabMarketData):
        if name.startswith("_"):
            continue
        low = name.lower()
        assert not any(w in low for w in ORDER_WORDS), name


def test_adapter_makes_no_post_except_token_refresh():
    """The only write to Schwab is an OAuth refresh — never an order."""
    posts = [n for n in ast.walk(SCHWAB_AST)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("post", "put", "patch", "delete")]
    assert len(posts) == 1, f"expected exactly one write call, found {len(posts)}"
    (call,) = posts
    assert call.func.attr == "post"
    target = call.args[0] if call.args else None
    assert isinstance(target, ast.Name) and target.id == "TOKEN_URL", (
        "the single write call must target TOKEN_URL")


# --- the venue gate, against the configuration this repo actually ships ----

def _snap() -> AccountSnapshot:
    return AccountSnapshot(total_value=100_000.0, cash=50_000.0)


def _intent(venue: str) -> OrderIntent:
    return OrderIntent(sleeve="event_driven", ticker="ACME", side="buy",
                       qty=100, limit_price=2.80,
                       signal_refs=("8-K 0001234-26-000042",), thesis="t",
                       venue=venue)


@pytest.fixture
def shipped_policy(tmp_path):
    import shutil
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "policy.example.yaml",
                tmp_path / "config" / "policy.yaml")
    return load(tmp_path / "config" / "policy.yaml",
                tmp_path / "data" / "sleeve_state.yaml",
                tmp_path / "config" / "catalysts.yaml")


def test_shipped_config_does_not_name_schwab_as_execution_venue(shipped_policy):
    assert shipped_policy.config.execution_venue != "schwab"
    assert shipped_policy.config.execution_venue == "robinhood"


def test_schwab_addressed_order_is_rejected_under_shipped_config(shipped_policy):
    v = check(_intent("schwab"), shipped_policy.config, _snap())
    assert v.approved is False and v.approval_id is None
    assert any(d.gate == "venue" and not d.passed for d in v.decisions)


def test_the_configured_venue_is_accepted(shipped_policy):
    """Guards against the test above passing because everything is rejected."""
    v = check(_intent("robinhood"), shipped_policy.config, _snap())
    assert any(d.gate == "venue" and d.passed for d in v.decisions)
