"""Snapshot-vs-ledger reconciliation (distinct from the operator `reconcile`
fill-fix): the agent-reported cost basis against what the ledger holds."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from croupier import journal
from croupier.audit import AuditLog
from croupier.catalysts import CatalystCalendar
from croupier.data.base import DataHealth
from croupier.gates.pipeline import PolicyConfig, SleeveConfig
from croupier.journal_render import render
from croupier.ledger import Ledger, Position
from croupier.models import AccountSnapshot, Mode, OrderIntent
from croupier.policy import Policy
from croupier.recon import reconcile_snapshot
from croupier.state import SleeveState


def _pos(sleeve, ticker, qty, basis):
    return Position(sleeve=sleeve, ticker=ticker, qty=qty, cost_basis=basis)


def _snap(sleeves=None, positions=None, ts="2026-08-28T10:00:00+00:00"):
    snap = {"total_value": 100_000.0, "cash": 1.0}
    if sleeves is not None:
        snap["sleeve_cost_basis"] = sleeves
    if positions is not None:
        snap["position_cost_basis"] = positions
    return {"ts": ts, "kind": "check", "snapshot": snap}


def test_match_has_no_discrepancies():
    rec = _snap({"a": 1000.0}, {"ACME": 1000.0})
    out = reconcile_snapshot(rec, [_pos("a", "ACME", 10, 1000.0)])
    assert out.checked and out.discrepancies == () and out.ok


def test_missing_fill_is_a_ledger_short_discrepancy():
    rec = _snap({"a": 2000.0}, {"ACME": 2000.0})
    out = reconcile_snapshot(rec, [_pos("a", "ACME", 10, 1000.0)])
    assert not out.ok
    kinds = {(d.scope, d.key): d for d in out.discrepancies}
    assert kinds[("ticker", "ACME")].ledger == 1000.0
    assert kinds[("ticker", "ACME")].reported == 2000.0
    assert ("sleeve", "a") in kinds


def test_phantom_fill_ticker_only_in_ledger():
    rec = _snap({"a": 0.0}, {})
    out = reconcile_snapshot(rec, [_pos("a", "ACME", 10, 1000.0)])
    assert out.ledger_only == ("ACME",) and not out.ok


def test_ticker_only_reported_is_listed():
    rec = _snap({}, {"ZZZ": 500.0})
    out = reconcile_snapshot(rec, [])
    assert out.reported_only == ("ZZZ",) and not out.ok


def test_within_tolerance_is_ignored_and_beyond_is_flagged():
    pos = [_pos("a", "ACME", 10, 100_000.0)]
    near = reconcile_snapshot(_snap({"a": 100_050.0}, {"ACME": 100_050.0}), pos)
    assert near.ok        # $50 off but 0.05%: inside the percent band
    far = reconcile_snapshot(_snap({"a": 110_000.0}, {"ACME": 110_000.0}), pos)
    assert not far.ok
    tiny = reconcile_snapshot(_snap({"a": 100_000.5}, {"ACME": 100_000.5}), pos)
    assert tiny.ok


def test_ticker_case_is_normalised():
    rec = _snap({"a": 1000.0}, {"acme": 1000.0})
    assert reconcile_snapshot(rec, [_pos("a", "ACME", 10, 1000.0)]).ok


def test_no_snapshot_yet_is_not_checked():
    out = reconcile_snapshot(None, [_pos("a", "ACME", 10, 1000.0)])
    assert not out.checked and out.ok


def test_old_record_without_basis_fields_is_not_checked():
    out = reconcile_snapshot(_snap(), [_pos("a", "ACME", 10, 1000.0)])
    assert not out.checked and out.ok


def test_check_and_log_persists_basis_fields(tmp_path):
    from croupier.gates.pipeline import PolicyConfig as PC
    log = AuditLog(tmp_path / "audit.jsonl")
    cfg = PC(sleeves={"event_driven": SleeveConfig("event_driven", 10, 3, mode=Mode.CONFIRM)},
             denylist={}, sector_denylist={}, ticker_sector={})
    intent = OrderIntent(sleeve="event_driven", ticker="ACME", side="buy", qty=10,
                         limit_price=2.8, signal_refs=("x",), thesis="t")
    snap = AccountSnapshot(total_value=100_000.0, cash=5.0,
                           sleeve_cost_basis={"event_driven": 10.0},
                           position_cost_basis={"ACME": 10.0})
    log.check_and_log(intent, cfg, snap)
    rec = log.records()[-1]["snapshot"]
    assert rec["sleeve_cost_basis"] == {"event_driven": 10.0}
    assert rec["position_cost_basis"] == {"ACME": 10.0}


def _policy(tmp_path):
    cfg = PolicyConfig(
        sleeves={"event_driven": SleeveConfig("event_driven", 10, 3, mode=Mode.CONFIRM)},
        denylist={}, sector_denylist={}, ticker_sector={})
    return Policy(config=cfg, state=SleeveState.empty(), max_sleeve_drawdown_pct=25.0,
                  schwab_token_path=tmp_path / "no.json",
                  catalysts=CatalystCalendar.empty())


def test_journal_section_and_attention_flag(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit._append({**_snap({"event_driven": 900.0}, {"ACME": 900.0})})
    with Ledger(tmp_path / "ledger.db") as led:
        report = journal.build(_policy(tmp_path), led, audit, DataHealth.FRESH,
                               as_of=date(2026, 8, 28),
                               now=datetime(2026, 8, 28, 12, tzinfo=UTC))
    assert report.recon is not None and not report.recon.ok
    text = render(report)
    assert "Snapshot vs ledger" in text and "ACME" in text


@pytest.mark.parametrize("has_snapshot", [False, True])
def test_journal_section_always_rendered(tmp_path, has_snapshot):
    audit = AuditLog(tmp_path / "audit.jsonl")
    if has_snapshot:
        audit._append(_snap({}, {}))
    with Ledger(tmp_path / "ledger.db") as led:
        report = journal.build(_policy(tmp_path), led, audit, DataHealth.FRESH,
                               as_of=date(2026, 8, 28),
                               now=datetime(2026, 8, 28, 12, tzinfo=UTC))
    assert "Snapshot vs ledger" in render(report) and report.recon.ok


def test_fill_after_the_snapshot_is_not_a_discrepancy(tmp_path):
    from croupier.ledger import Fill
    from croupier.recon import reconcile_ledger
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit._append(_snap({"event_driven": 0.0}, {}))
    with Ledger(tmp_path / "ledger.db") as led:
        led.record_fill(Fill("a1", "event_driven", "ACME", "buy", 10, 100.0,
                             datetime(2026, 8, 28, 11, tzinfo=UTC)))
        assert reconcile_ledger(audit, led).ok
        led.record_fill(Fill("a0", "event_driven", "ZZZ", "buy", 10, 100.0,
                             datetime(2026, 8, 28, 9, tzinfo=UTC)))
        assert reconcile_ledger(audit, led).ledger_only == ("ZZZ",)
