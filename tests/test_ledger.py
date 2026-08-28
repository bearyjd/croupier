# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Position ledger: approval_id is the key, replays are safe, conflicts are loud."""
from datetime import UTC, datetime

import pytest

from croupier.ledger import Fill, Ledger, LedgerConflict, new_fill

SLEEVE = "event_driven"


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


def _fill(approval_id="a1", side="buy", qty=1000.0, price=2.80, ticker="ACME",
          when=datetime(2026, 8, 26, 14, 0, tzinfo=UTC)) -> Fill:
    return Fill(approval_id=approval_id, sleeve=SLEEVE, ticker=ticker, side=side,
                qty=qty, price=price, filled_at=when)


def test_fill_persists_and_derives_a_position(tmp_path):
    with _ledger(tmp_path) as led:
        assert led.record_fill(_fill()) is True
        (pos,) = led.positions()
        assert pos.sleeve == SLEEVE and pos.ticker == "ACME"
        assert pos.qty == 1000.0 and pos.cost_basis == 2800.0
        assert round(pos.avg_cost, 4) == 2.80


def test_identical_replay_is_idempotent(tmp_path):
    with _ledger(tmp_path) as led:
        assert led.record_fill(_fill()) is True
        assert led.record_fill(_fill()) is False       # agent retried
        assert len(led.fills()) == 1


def test_conflicting_replay_raises_rather_than_overwriting(tmp_path):
    with _ledger(tmp_path) as led:
        led.record_fill(_fill())
        with pytest.raises(LedgerConflict, match="refusing to overwrite"):
            led.record_fill(_fill(qty=500.0))


def test_sell_reduces_qty_and_basis_proportionally(tmp_path):
    with _ledger(tmp_path) as led:
        led.record_fill(_fill("a1", "buy", 1000.0, 2.80))
        led.record_fill(_fill("a2", "sell", 400.0, 5.00))
        (pos,) = led.positions()
        assert pos.qty == 600.0
        assert round(pos.cost_basis, 6) == 1680.0      # basis follows avg cost, not sale price


def test_fully_closed_position_disappears(tmp_path):
    with _ledger(tmp_path) as led:
        led.record_fill(_fill("a1", "buy", 1000.0, 2.80))
        led.record_fill(_fill("a2", "sell", 1000.0, 5.00))
        assert led.positions() == []
        assert led.sleeves() == [SLEEVE]                # the sleeve still has history


def test_net_flow_is_buys_minus_sells_for_that_day(tmp_path):
    d1 = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
    d2 = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)
    with _ledger(tmp_path) as led:
        led.record_fill(_fill("a1", "buy", 1000.0, 2.80, when=d1))
        led.record_fill(_fill("a2", "buy", 500.0, 3.00, when=d2))
        led.record_fill(_fill("a3", "sell", 200.0, 4.00, when=d2))
        assert led.net_flow_on(SLEEVE, d1.date()) == 2800.0
        assert led.net_flow_on(SLEEVE, d2.date()) == 1500.0 - 800.0


def test_ledger_survives_reopen(tmp_path):
    with _ledger(tmp_path) as led:
        led.record_fill(_fill())
    with _ledger(tmp_path) as led:
        assert len(led.fills()) == 1 and led.positions()[0].qty == 1000.0


def test_new_fill_normalises_ticker_case():
    assert new_fill("a1", SLEEVE, "acme", "buy", 10, 1.0).ticker == "ACME"
