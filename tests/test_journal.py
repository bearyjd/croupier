# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""The daily journal: what the operator reads before deciding anything."""
from datetime import UTC, date, datetime

import pytest

from croupier import journal
from croupier.audit import AuditLog
from croupier.catalysts import CatalystCalendar, CatalystEvent
from croupier.data.base import DataHealth
from croupier.gates.pipeline import PolicyConfig, SleeveConfig
from croupier.journal_render import render
from croupier.ledger import Fill, Ledger
from croupier.models import Mode
from croupier.policy import Policy
from croupier.state import Halt, SleeveState

DAY = date(2026, 8, 28)
NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def _policy(tmp_path, state=None, mode=Mode.CONFIRM, calendar=None) -> Policy:
    cfg = PolicyConfig(
        sleeves={"event_driven": SleeveConfig("event_driven", 10, 3, mode=mode),
                 "filature": SleeveConfig("filature", 0, 0, mode=Mode.HALTED)},
        denylist={}, sector_denylist={}, ticker_sector={},
    )
    return Policy(config=cfg, state=state or SleeveState.empty(),
                  max_sleeve_drawdown_pct=25.0,
                  schwab_token_path=tmp_path / "no_tokens.json",
                  catalysts=calendar or CatalystCalendar.empty())


def _check_record(approval_id, approved=True, requires_confirm=True, total_value=100_000.0):
    return {
        "ts": "2026-08-28T10:00:00+00:00", "kind": "check",
        "intent": {"sleeve": "event_driven", "ticker": "ACME", "side": "buy",
                   "qty": 1000, "limit_price": 2.80, "thesis": "catalyst entry"},
        "snapshot": {"total_value": total_value, "cash": 50_000.0},
        "approved": approved, "approval_id": approval_id,
        "requires_confirm": requires_confirm,
    }


def _build(tmp_path, audit, ledger, health=DataHealth.DEGRADED, state=None, calendar=None):
    return journal.build(_policy(tmp_path, state, calendar=calendar), ledger, audit,
                         data_health=health, as_of=DAY, now=NOW)


def test_sleeve_ceiling_uses_the_latest_agent_snapshot(tmp_path, audit, ledger):
    audit._append(_check_record("a1", total_value=100_000.0))
    audit._append(_check_record("a2", total_value=120_000.0))
    ledger.record_fill(Fill("a1", "event_driven", "ACME", "buy", 1000, 2.80,
                            datetime(2026, 8, 28, 14, tzinfo=UTC)))
    report = _build(tmp_path, audit, ledger)
    assert report.account_value == 120_000.0
    line = next(s for s in report.sleeves if s.sleeve == "event_driven")
    assert line.ceiling == 12_000.0
    assert line.cost_basis == 2800.0
    assert round(line.utilisation_pct, 2) == pytest.approx(23.33, abs=0.01)


def test_unknown_account_value_is_stated_not_guessed(tmp_path, audit, ledger):
    report = _build(tmp_path, audit, ledger)
    assert report.account_value is None
    assert all(s.ceiling is None for s in report.sleeves)
    assert "_unknown_" in render(report)


def test_pending_confirms_exclude_filled_orders(tmp_path, audit, ledger):
    audit._append(_check_record("a1"))
    audit._append(_check_record("a2"))
    audit.log_fill("a1", "ACME", "buy", 1000, 2.79, sleeve="event_driven")
    report = _build(tmp_path, audit, ledger)
    assert [p.approval_id for p in report.pending] == ["a2"]


def test_orphan_fill_does_not_clear_a_pending_confirm(tmp_path, audit, ledger):
    audit._append(_check_record("a1"))
    audit.log_fill("a1", "ACME", "buy", 1000, 2.79, orphan=True)
    report = _build(tmp_path, audit, ledger)
    assert [p.approval_id for p in report.pending] == ["a1"]


def test_rejected_and_auto_orders_are_not_pending_confirms(tmp_path, audit, ledger):
    audit._append(_check_record("a1", approved=False))
    audit._append(_check_record("a2", requires_confirm=False))
    assert _build(tmp_path, audit, ledger).pending == ()


def test_halts_are_surfaced_with_their_reason(tmp_path, audit, ledger):
    state = SleeveState.empty().with_halt(
        Halt("event_driven", "drawdown 31.4% exceeds 25%", NOW, 31.4))
    report = _build(tmp_path, audit, ledger, state=state)
    text = render(report)
    assert "## Halts (1)" in text and "drawdown 31.4%" in text
    assert "HALTED: drawdown 31.4%" in text


def test_degraded_banner_is_shown(tmp_path, audit, ledger):
    assert "DEGRADED DATA" in render(_build(tmp_path, audit, ledger))


def test_dead_banner_is_shown(tmp_path, audit, ledger):
    text = render(_build(tmp_path, audit, ledger, health=DataHealth.DEAD))
    assert "DEAD DATA" in text


def test_fresh_data_shows_no_banner(tmp_path, audit, ledger):
    text = render(_build(tmp_path, audit, ledger, health=DataHealth.FRESH))
    assert "DEGRADED DATA" not in text and "DEAD DATA" not in text


def test_missing_schwab_tokens_reported_without_a_nag(tmp_path, audit, ledger):
    report = _build(tmp_path, audit, ledger)
    assert report.auth.configured is False and report.auth.nag is False
    assert "no Schwab tokens" in report.auth.detail


def test_reauth_nag_inside_24h(tmp_path, audit, ledger):
    """Refresh tokens hard-expire at 7 days; nag inside the last 24h."""
    from croupier.data.schwab import TokenState
    # issued 2026-08-21 18:00 -> expires 2026-08-28 18:00, i.e. 6h from NOW
    TokenState("a", "r", NOW, datetime(2026, 8, 21, 18, tzinfo=UTC)).save(
        tmp_path / "no_tokens.json")
    report = _build(tmp_path, audit, ledger)
    assert report.auth.nag is True
    assert round(report.auth.days_until_reauth, 2) == 0.25
    assert "RE-AUTH NAG" in render(report)


def test_expired_refresh_token_is_flagged(tmp_path, audit, ledger):
    from croupier.data.schwab import TokenState
    TokenState("a", "r", NOW, datetime(2026, 8, 1, tzinfo=UTC)).save(
        tmp_path / "no_tokens.json")
    report = _build(tmp_path, audit, ledger)
    assert report.auth.nag is True and "EXPIRED" in report.auth.detail


def test_active_freeze_renders_in_its_own_section(tmp_path, audit, ledger):
    cal = CatalystCalendar(events=(CatalystEvent(
        "ACME", "phase_data_readout", date(2026, 9, 1), date(2026, 12, 1),
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=ACME",
        verified=False, note="lead asset mono readout"), ))
    text = render(_build(tmp_path, audit, ledger, calendar=cal))   # DAY is inside
    assert "## Catalyst freezes (1)" in text
    assert "**ACME** phase_data_readout" in text
    assert "UNVERIFIED window" in text
    assert "sec.gov" in text


def test_freeze_outside_its_window_is_not_shown(tmp_path, audit, ledger):
    cal = CatalystCalendar(events=(CatalystEvent(
        "ACME", "phase_data_readout", date(2026, 11, 1), date(2026, 12, 1),
        "https://example.invalid"), ))
    report = _build(tmp_path, audit, ledger, calendar=cal)   # DAY is 2026-08-28
    assert report.freezes == ()
    assert "## Catalyst freezes (0)" in render(report)


def test_journal_file_is_named_for_the_day(tmp_path, audit, ledger):
    report = _build(tmp_path, audit, ledger)
    path = journal.write(report, render(report), tmp_path / "journal")
    assert path.name == "2026-08-28.md"
    assert path.read_text().startswith("# Croupier journal — 2026-08-28")


def test_corrupt_audit_line_does_not_break_the_journal(tmp_path, audit, ledger):
    audit._append(_check_record("a1"))
    with open(audit.path, "a") as f:
        f.write("{not json\n")
    report = _build(tmp_path, audit, ledger)
    assert len(report.pending) == 1
