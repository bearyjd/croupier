# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Daily mark-to-market and the automatic 25% drawdown halt. No network."""
from datetime import UTC, date, datetime

import pytest

from croupier.audit import AuditLog
from croupier.data.base import DataHealth
from croupier.ledger import Fill, Ledger
from croupier.marking import mark_to_market
from croupier.state import SleeveState

SLEEVE = "event_driven"
D1, D2 = date(2026, 8, 26), date(2026, 8, 27)


def _fill(led, approval_id, side, qty, price, day, ticker="ACME"):
    led.record_fill(Fill(approval_id, SLEEVE, ticker, side, qty, price,
                         datetime(day.year, day.month, day.day, 14, tzinfo=UTC)))


@pytest.fixture
def led(tmp_path):
    with Ledger(tmp_path / "ledger.db") as ledger:
        yield ledger


async def test_marks_open_positions_at_the_quoted_price(led, fake_router, tmp_path):
    _fill(led, "a1", "buy", 1000, 2.80, D1)
    result = await mark_to_market(led, fake_router({"ACME": 3.20}), SleeveState.empty(),
                                  max_drawdown_pct=25.0, as_of=D1)
    (m,) = result.marks
    assert m.market_value == pytest.approx(3200.0)
    assert m.cost_basis == pytest.approx(2800.0)
    assert m.net_flow == pytest.approx(2800.0)
    assert result.data_health == DataHealth.FRESH


async def test_drawdown_beyond_ceiling_halts_the_sleeve(led, fake_router, tmp_path,
                                                        monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fill(led, "a1", "buy", 1000, 2.80, D1)
    await mark_to_market(led, fake_router({"ACME": 2.80}), SleeveState.empty(),
                         max_drawdown_pct=25.0, as_of=D1)

    audit = AuditLog(tmp_path / "audit.jsonl")
    result = await mark_to_market(led, fake_router({"ACME": 2.00}), SleeveState.empty(),
                                  max_drawdown_pct=25.0, as_of=D2, audit=audit)
    (m,) = result.marks
    assert round(m.point.drawdown_pct, 2) == pytest.approx(28.57, abs=0.01)
    assert m.halted_now is True
    assert result.state.is_halted(SLEEVE)
    assert "guardrails" in result.state.halts[SLEEVE].reason
    assert "sleeve_halted" in (tmp_path / "audit.jsonl").read_text()


async def test_drawdown_within_ceiling_does_not_halt(led, fake_router, tmp_path,
                                                     monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fill(led, "a1", "buy", 1000, 2.80, D1)
    await mark_to_market(led, fake_router({"ACME": 2.80}), SleeveState.empty(),
                         max_drawdown_pct=25.0, as_of=D1)
    result = await mark_to_market(led, fake_router({"ACME": 2.20}), SleeveState.empty(),
                                  max_drawdown_pct=25.0, as_of=D2)   # -21.4%
    (m,) = result.marks
    assert m.halted_now is False and not result.state.is_halted(SLEEVE)


async def test_selling_a_winner_does_not_trip_the_halt(led, fake_router, tmp_path,
                                                       monkeypatch):
    """Regression: raw market value would read a profitable exit as -100%."""
    monkeypatch.chdir(tmp_path)
    _fill(led, "a1", "buy", 1000, 2.80, D1)
    await mark_to_market(led, fake_router({"ACME": 4.00}), SleeveState.empty(),
                         max_drawdown_pct=25.0, as_of=D1)
    _fill(led, "a2", "sell", 1000, 4.00, D2)
    result = await mark_to_market(led, fake_router({}), SleeveState.empty(),
                                  max_drawdown_pct=25.0, as_of=D2)
    (m,) = result.marks
    assert m.market_value == 0.0
    assert m.halted_now is False and m.point.drawdown_pct == 0.0


async def test_unpriced_holding_is_carried_at_cost_not_zero(led, fake_router, tmp_path,
                                                            monkeypatch):
    """A data outage must not manufacture a drawdown and halt the sleeve."""
    monkeypatch.chdir(tmp_path)
    _fill(led, "a1", "buy", 1000, 2.80, D1)
    await mark_to_market(led, fake_router({"ACME": 2.80}), SleeveState.empty(),
                         max_drawdown_pct=25.0, as_of=D1)
    result = await mark_to_market(led, fake_router({}), SleeveState.empty(),
                                  max_drawdown_pct=25.0, as_of=D2)   # no quote at all
    (m,) = result.marks
    assert m.unpriced == ("ACME",)
    assert m.market_value == pytest.approx(2800.0)
    assert m.halted_now is False


async def test_already_halted_sleeve_is_not_rehalted(led, fake_router, tmp_path,
                                                     monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fill(led, "a1", "buy", 1000, 2.80, D1)
    await mark_to_market(led, fake_router({"ACME": 2.80}), SleeveState.empty(),
                         max_drawdown_pct=25.0, as_of=D1)
    from croupier.state import Halt
    prior = SleeveState.empty().with_halt(
        Halt(SLEEVE, "earlier halt", datetime(2026, 8, 26, tzinfo=UTC), 40.0))
    result = await mark_to_market(led, fake_router({"ACME": 1.00}), prior,
                                  max_drawdown_pct=25.0, as_of=D2)
    (m,) = result.marks
    assert m.halted_now is False                       # no duplicate halt event
    assert result.state.halts[SLEEVE].reason == "earlier halt"   # original reason kept
