# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""End-to-end CLI: check -> fill -> ledger, and the joins that must hold."""
import io
import json
import shutil
from pathlib import Path

import pytest

from croupier import cli
from croupier.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway CWD with a real policy.yaml, so data/ lands in tmp_path."""
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "policy.yaml", tmp_path / "config" / "policy.yaml")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(monkeypatch, capsys, argv, payload=None):
    if payload is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _order(**kw):
    base = json.loads((REPO / "examples" / "order.json").read_text())
    base.update(kw)
    return base


def test_check_then_fill_lands_in_the_ledger(workspace, monkeypatch, capsys):
    code, verdict = _run(monkeypatch, capsys, ["check"], _order())
    assert code == 0 and verdict["approved"] and verdict["requires_confirm"]

    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": verdict["approval_id"], "ticker": "ACME",
        "side": "buy", "qty": 1000, "price": 2.79})
    assert code == 0 and out["ledger"] is True and out["sleeve"] == "event_driven"

    with Ledger(workspace / "data" / "ledger.db") as led:
        (pos,) = led.positions()
        assert pos.ticker == "ACME" and pos.qty == 1000 and pos.cost_basis == 2790.0


def test_fill_without_an_approval_is_audited_but_refused(workspace, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": "deadbeefdeadbeef", "ticker": "ACME",
        "side": "buy", "qty": 1000, "price": 2.79})
    assert code == 1 and out["ledger"] is False and "ORPHAN" in out["error"]
    assert out["logged"] is True
    audit = (workspace / "data" / "audit.jsonl").read_text()
    assert '"orphan": true' in audit           # the ungated trade is on the record
    assert not (workspace / "data" / "ledger.db").exists()


def test_fill_that_contradicts_its_approval_is_refused(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": verdict["approval_id"], "ticker": "BRVO",   # wrong ticker
        "side": "buy", "qty": 1000, "price": 2.79})
    assert code == 1 and out["ledger"] is False and "does not match approval" in out["error"]


def test_overfill_is_recorded_but_flagged(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": verdict["approval_id"], "ticker": "ACME",
        "side": "buy", "qty": 1500, "price": 2.79})       # approved 1000
    assert code == 1 and out["ledger"] is True
    assert any("overfill" in w for w in out["warnings"])


def test_rejected_check_yields_no_approval_id(workspace, monkeypatch, capsys):
    code, verdict = _run(monkeypatch, capsys, ["check"], _order(venue="schwab"))
    assert code == 1 and not verdict["approved"] and verdict["approval_id"] is None


def test_check_requires_signal_refs(workspace, monkeypatch, capsys):
    with pytest.raises(ValueError, match="signal_refs"):
        _run(monkeypatch, capsys, ["check"], _order(signal_refs=[]))


def test_mark_halts_the_sleeve_and_the_next_check_is_rejected(
        workspace, monkeypatch, capsys, fake_router):
    """The full loop: halt written by mark, honoured by the policy loader."""
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], {
        "approval_id": verdict["approval_id"], "ticker": "ACME",
        "side": "buy", "qty": 1000, "price": 2.80})

    routers = iter([fake_router({"ACME": 2.80}), fake_router({"ACME": 1.50})])
    monkeypatch.setattr(cli, "build_router", lambda *_a, **_k: next(routers))

    assert cli.main(["mark"]) == 0                      # day 1: baseline
    capsys.readouterr()
    assert cli.main(["mark"]) == 1                      # day 2: -46%, new halt
    out = json.loads(capsys.readouterr().out)
    assert out["new_halts"] == ["event_driven"]

    state = (workspace / "data" / "sleeve_state.yaml").read_text()
    assert "mode: halted" in state and "guardrails" in state

    code, verdict = _run(monkeypatch, capsys, ["check"], _order())
    assert code == 1
    assert any(d["gate"] == "mode" and not d["passed"] and "HALTED" in d["reason"]
               for d in verdict["decisions"])


def test_check_carries_the_catalyst_freeze_gate(workspace, monkeypatch, capsys):
    """The shipped calendar is wired into `croupier check`, freeze or not."""
    shutil.copy(REPO / "config" / "catalysts.yaml", workspace / "config" / "catalysts.yaml")
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    assert any(d["gate"] == "catalyst_freeze" for d in verdict["decisions"])


def test_check_inside_a_freeze_window_escalates_to_confirm(workspace, monkeypatch, capsys):
    (workspace / "config" / "catalysts.yaml").write_text(
        "freeze_trading_days: 5\n"
        "events:\n"
        "  - ticker: ACME\n"
        "    event_type: phase_data_readout\n"
        "    window_start: 2020-01-01\n"
        "    window_end: 2099-12-31\n"
        "    source_url: https://www.sec.gov/cgi-bin/browse-edgar?CIK=ACME\n")
    # AUTO with headroom: without a freeze this order would not need a human.
    policy = (workspace / "config" / "policy.yaml")
    policy.write_text(policy.read_text().replace(
        "    mode: confirm", "    mode: auto\n    auto_order_max_usd: 10000\n"
                             "    auto_daily_max_usd: 10000", 1))
    code, verdict = _run(monkeypatch, capsys, ["check"], _order())
    assert code == 0 and verdict["approved"] is True
    assert verdict["requires_confirm"] is True
    assert any(d["gate"] == "catalyst_freeze" and "FREEZE" in d["reason"]
               for d in verdict["decisions"])


def test_journal_shows_an_active_freeze(workspace, monkeypatch, capsys):
    (workspace / "config" / "catalysts.yaml").write_text(
        "freeze_trading_days: 5\n"
        "events:\n"
        "  - ticker: ACME\n"
        "    event_type: phase_data_readout\n"
        "    window_start: 2020-01-01\n"
        "    window_end: 2099-12-31\n"
        "    source_url: https://www.sec.gov/cgi-bin/browse-edgar?CIK=ACME\n")
    cli.main(["journal"])
    text = capsys.readouterr().out
    assert "## Catalyst freezes (1)" in text and "**ACME**" in text
    written = (workspace / "data" / "journal").glob("*.md")
    assert "Catalyst freezes (1)" in next(written).read_text()
