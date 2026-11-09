# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""End-to-end CLI: check -> fill -> ledger, and the joins that must hold."""
import io
import json
import shutil
from pathlib import Path

import pytest

from croupier import cli
from croupier.data.base import DataHealth
from croupier.ledger import Ledger

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway CWD with a real policy.yaml, so data/ lands in tmp_path."""
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "policy.example.yaml",
                tmp_path / "config" / "policy.yaml")
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


def test_decline_retires_a_pending_confirm_without_touching_the_ledger(
        workspace, monkeypatch, capsys):
    """check -> decline: the approval stays in the trail, the journal stops
    listing it, and no position is ever created."""
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["decline"], {
        "approval_id": verdict["approval_id"], "reason": "operator said N"})
    assert code == 0 and out["logged"] and out["known"] is True
    assert out["ticker"] == "ACME" and out["sleeve"] == "event_driven"

    lines = [json.loads(line) for line in
             (workspace / "data" / "audit.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in lines] == ["check", "decline"]
    assert not (workspace / "data" / "ledger.db").exists()

    from croupier.audit import AuditLog
    from croupier.journal import pending_confirms
    assert pending_confirms(AuditLog(workspace / "data" / "audit.jsonl")) == ()


def test_decline_needs_a_reason(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["decline"], {
        "approval_id": verdict["approval_id"], "reason": "   "})
    assert code == 1 and out["logged"] is False
    audit = (workspace / "data" / "audit.jsonl").read_text()
    assert '"decline"' not in audit


def test_decline_of_an_unknown_id_is_recorded_flagged_and_refused(
        workspace, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["decline"], {
        "approval_id": "deadbeefdeadbeef", "reason": "fat finger"})
    assert code == 1 and out["logged"] is True and out["known"] is False
    audit = (workspace / "data" / "audit.jsonl").read_text()
    assert '"known": false' in audit


def test_fill_that_contradicts_its_approval_is_refused(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": verdict["approval_id"], "ticker": "BRVO",   # wrong ticker
        "side": "buy", "qty": 1000, "price": 2.79})
    assert code == 1 and out["ledger"] is False and "does not match approval" in out["error"]


def _fill_payload(verdict, qty, **kw):
    return {"approval_id": verdict["approval_id"], "ticker": "ACME",
            "side": "buy", "qty": qty, "price": 2.79, **kw}


def _ledger_qty(workspace):
    with Ledger(workspace / "data" / "ledger.db") as led:
        return [f.qty for f in led.fills()]


def test_overfill_is_recorded_but_flagged(workspace, monkeypatch, capsys):
    """The ledger must reflect the real fill: refusing it would understate
    holdings, so cost-basis caps, marking and the drawdown halt go blind."""
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1500))
    assert code == 1 and out["ledger"] is True
    assert any("overfill: reported 1500 > approved 1000" in w for w in out["warnings"])
    assert _ledger_qty(workspace) == [1500.0]
    last = _audit_lines(workspace)[-1]
    assert last["overfill"] is True and not last.get("orphan")


def test_fill_after_decline_is_recorded_but_flagged(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["decline"], {
        "approval_id": verdict["approval_id"], "reason": "operator said N"})
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    assert code == 1 and out["ledger"] is True
    assert any("fill after decline" in w for w in out["warnings"])
    assert _ledger_qty(workspace) == [1000.0]
    last = _audit_lines(workspace)[-1]
    assert last["fill_after_decline"] is True and not last.get("orphan")


@pytest.mark.parametrize("qty, flagged", [
    (1000, False), (1000 + 1e-10, False),      # within the 1e-9 tolerance
    (1000 + 1e-8, True), (1000.5, True), (1001, True),
])
def test_overfill_boundary(workspace, monkeypatch, capsys, qty, flagged):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, qty))
    assert out["ledger"] is True
    assert bool(out["warnings"]) is flagged and code == (1 if flagged else 0)
    assert bool(_audit_lines(workspace)[-1].get("overfill")) is flagged


def test_identical_replay_is_a_harmless_repeat(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    n = len(_audit_lines(workspace))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    assert code == 0 and out["ledger"] is True and out["new"] is False
    assert len(_audit_lines(workspace)) == n
    assert _ledger_qty(workspace) == [1000.0]


def test_replay_of_a_flagged_fill_stays_flagged_and_not_orphaned(
        workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1500))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1500))
    assert code == 1 and out["new"] is False and out["ledger"] is True
    assert any("overfill" in w for w in out["warnings"])
    assert not any(r.get("orphan") for r in _audit_lines(workspace))


def test_conflicting_second_fill_is_logged_as_refused(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 900))
    assert code == 1 and out["ledger"] is False
    assert _audit_lines(workspace)[-1]["refused"] is True


def test_late_decline_of_a_filled_approval_is_refused(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    code, out = _run(monkeypatch, capsys, ["decline"], {
        "approval_id": verdict["approval_id"], "reason": "changed my mind"})
    assert code == 1 and "already" in out["error"]
    assert "decline" not in [r["kind"] for r in _audit_lines(workspace)]


def test_ack_fill_needs_a_reason_and_a_flagged_fill(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    aid = verdict["approval_id"]
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1500))
    code, out = _run(monkeypatch, capsys, ["ack-fill", aid, "--reason", "  "])
    assert code == 1 and "reason" in out["error"]
    code, out = _run(monkeypatch, capsys, ["ack-fill", "nope", "--reason", "x"])
    assert code == 1 and "no unresolved" in out["error"]
    code, out = _run(monkeypatch, capsys, ["ack-fill", aid, "--reason", "reconciled"])
    assert code == 0 and out["logged"] is True
    last = _audit_lines(workspace)[-1]
    assert last["kind"] == "ack_fill" and last["approval_id"] == aid
    assert last["reason"] == "reconciled"


def test_journal_exits_nonzero_until_a_flagged_fill_is_acknowledged(
        workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    aid = verdict["approval_id"]
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1500))
    capsys.readouterr()
    assert cli.main(["journal"]) == 1
    text = capsys.readouterr().out
    assert "Needs action" in text and aid in text and "overfill" in text
    _run(monkeypatch, capsys, ["ack-fill", aid, "--reason", "reconciled"])
    capsys.readouterr()
    assert cli.main(["journal"]) == 0


def test_rejected_check_yields_no_approval_id(workspace, monkeypatch, capsys):
    code, verdict = _run(monkeypatch, capsys, ["check"], _order(venue="schwab"))
    assert code == 1 and not verdict["approved"] and verdict["approval_id"] is None


NONFINITE = ("NaN", "Infinity", "-Infinity")


def _run_raw(monkeypatch, capsys, argv, raw):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("literal", NONFINITE)
@pytest.mark.parametrize("field", ["qty", "limit_price", "cash", "total_value"])
def test_check_rejects_nonfinite_numbers_at_the_json_boundary(
        workspace, monkeypatch, capsys, literal, field):
    order = _order()
    if field in ("cash", "total_value"):
        order["account"][field] = "@@"
    else:
        order[field] = "@@"
    raw = json.dumps(order).replace('"@@"', literal)
    code, out = _run_raw(monkeypatch, capsys, ["check"], raw)
    assert code == 2 and "approved" not in out and literal in out["error"]
    assert not (workspace / "data" / "audit.jsonl").exists()


@pytest.mark.parametrize("literal", NONFINITE)
def test_fill_rejects_nonfinite_numbers(workspace, monkeypatch, capsys, literal):
    raw = ('{"approval_id": "x", "ticker": "ACME", "side": "buy", '
           f'"qty": {literal}, "price": 1.0}}')
    code, out = _run_raw(monkeypatch, capsys, ["fill"], raw)
    assert code == 2 and literal in out["error"]


def _approved_id(monkeypatch, capsys):
    code, verdict = _run(monkeypatch, capsys, ["check"], _order())
    assert code == 0
    return verdict["approval_id"]


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("field", ["qty", "price"])
@pytest.mark.parametrize("bad", ["1e400", "-1e400", '"inf"', '"nan"', "true"])
def test_fill_after_an_approved_check_rejects_non_finite_or_non_numeric(
        workspace, monkeypatch, capsys, side, field, bad):
    approval_id = _approved_id(monkeypatch, capsys)
    good = {"qty": "1000", "price": "2.79"}
    good[field] = bad
    raw = (f'{{"approval_id": "{approval_id}", "ticker": "ACME", "side": "{side}", '
           f'"qty": {good["qty"]}, "price": {good["price"]}}}')
    code, out = _run_raw(monkeypatch, capsys, ["fill"], raw)
    assert code == 2 and out["ledger"] is False
    assert not (workspace / "data" / "ledger.db").exists()
    text = (workspace / "data" / "audit.jsonl").read_text()
    for line in text.splitlines():           # strict JSON: no bare NaN/Infinity
        json.loads(line, parse_constant=lambda c: pytest.fail(f"bare {c} in audit"))
    last = json.loads(text.splitlines()[-1])
    assert last["kind"] == "fill" and last["orphan"] is True


@pytest.mark.parametrize("field", ["qty", "limit_price"])
@pytest.mark.parametrize("bad", ["1e400", '"abc"', "true"])
def test_check_rejects_overflow_string_and_bool_with_json_error(
        workspace, monkeypatch, capsys, field, bad):
    raw = json.dumps({**_order(), field: "@@"}).replace('"@@"', bad)
    code, out = _run_raw(monkeypatch, capsys, ["check"], raw)
    assert code == 2 and "error" in out and "approved" not in out


def test_malformed_json_is_a_json_error_not_a_traceback(workspace, monkeypatch, capsys):
    code, out = _run_raw(monkeypatch, capsys, ["check"], "{not json")
    assert code == 2 and "error" in out


def test_fill_with_null_approval_id_cannot_join_a_rejected_check(
        workspace, monkeypatch, capsys):
    """A rejected check logs approval_id null; a null id must not match it."""
    _run(monkeypatch, capsys, ["check"], _order(venue="schwab"))
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": None, "ticker": "ACME", "side": "buy",
        "qty": 1, "price": 1.0})
    assert code == 2 and out["ledger"] is False
    assert not (workspace / "data" / "ledger.db").exists() or not any(
        Ledger(workspace / "data" / "ledger.db").positions())


def _audit_lines(workspace):
    path = workspace / "data" / "audit.jsonl"
    assert path.exists() and path.read_text().strip(), "audit log must not be empty"

    def boom(c):
        raise AssertionError(f"bare {c} in audit log")
    return [json.loads(x, parse_constant=boom) for x in path.read_text().splitlines()]


@pytest.mark.parametrize("field", ["approval_id", "ticker", "side"])
def test_fill_with_a_non_finite_identifier_is_still_audited(
        workspace, monkeypatch, capsys, field):
    payload = {"approval_id": "x", "ticker": "ACME", "side": "buy", "qty": 1, "price": 1.0}
    raw = json.dumps({**payload, field: "@@"}).replace('"@@"', "1e400")
    code, out = _run_raw(monkeypatch, capsys, ["fill"], raw)
    assert code != 0 and out["ledger"] is False
    assert _audit_lines(workspace)[-1]["kind"] == "fill"


def test_decline_with_a_non_finite_id_is_still_audited(workspace, monkeypatch, capsys):
    code, _ = _run_raw(monkeypatch, capsys, ["decline"],
                       '{"approval_id": 1e400, "reason": "r"}')
    assert code != 0
    assert _audit_lines(workspace)[-1]["kind"] == "decline"


@pytest.mark.parametrize("field", ["qty", "price"])
@pytest.mark.parametrize("bad", ["0", "-0.0", "-3", "1" + "0" * 400])
def test_fill_with_zero_negative_or_huge_numbers_is_orphaned_not_a_crash(
        workspace, monkeypatch, capsys, field, bad):
    approval_id = _approved_id(monkeypatch, capsys)
    good = {"qty": "1000", "price": "2.79"}
    good[field] = bad
    raw = (f'{{"approval_id": "{approval_id}", "ticker": "ACME", "side": "buy", '
           f'"qty": {good["qty"]}, "price": {good["price"]}}}')
    code, out = _run_raw(monkeypatch, capsys, ["fill"], raw)
    assert code == 2 and out["ledger"] is False
    assert not (workspace / "data" / "ledger.db").exists()
    last = _audit_lines(workspace)[-1]
    assert last["kind"] == "fill" and last["orphan"] is True


@pytest.mark.parametrize("field", ["ticker", "side"])
def test_fill_with_a_non_text_ticker_or_side_after_a_real_approval_is_orphaned(
        workspace, monkeypatch, capsys, field):
    approval_id = _approved_id(monkeypatch, capsys)
    payload = {"approval_id": approval_id, "ticker": "ACME", "side": "buy",
               "qty": 1, "price": 1.0, field: 1.5}
    code, out = _run(monkeypatch, capsys, ["fill"], payload)
    assert code == 2 and out["ledger"] is False
    assert _audit_lines(workspace)[-1]["orphan"] is True


def test_a_ledger_integrity_error_is_a_json_error_not_a_traceback(
        workspace, monkeypatch, capsys):
    import sqlite3
    approval_id = _approved_id(monkeypatch, capsys)

    def boom(*_a, **_k):
        raise sqlite3.IntegrityError("CHECK constraint failed")
    monkeypatch.setattr(Ledger, "record_fill", boom)
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": approval_id, "ticker": "ACME", "side": "buy",
        "qty": 1000, "price": 2.79})
    assert code == 2 and "error" in out


def test_every_audit_line_is_strict_json_after_hostile_inputs(
        workspace, monkeypatch, capsys):
    approval_id = _approved_id(monkeypatch, capsys)
    for qty, price in (("1e400", "1"), ('"nan"', "1"), ("1", "-1e400"), ("true", "1")):
        _run_raw(monkeypatch, capsys, ["fill"],
                 f'{{"approval_id": "{approval_id}", "ticker": "ACME", '
                 f'"side": "buy", "qty": {qty}, "price": {price}}}')
    _run_raw(monkeypatch, capsys, ["decline"], '{"approval_id": 1e400}')
    _run_raw(monkeypatch, capsys, ["fill"],
             '{"approval_id": 1e400, "ticker": 1e400, "side": 1e400, "qty": 1, "price": 1}')
    assert len(_audit_lines(workspace)) >= 6


@pytest.mark.parametrize("cmd", ["check", "fill"])
def test_absurdly_nested_json_is_a_json_error_not_a_traceback(
        workspace, monkeypatch, capsys, cmd):
    deep = "[" * 1200 + "]" * 1200
    base = {"check": _order(), "fill": {"approval_id": "x", "ticker": "ACME",
                                        "side": "buy", "qty": 1, "price": 1}}[cmd]
    raw = json.dumps({**base, "qty": "@@"}).replace('"@@"', deep)
    code, out = _run_raw(monkeypatch, capsys, [cmd], raw)
    assert code == 2 and "error" in out


def test_recursion_error_while_parsing_or_handling_is_a_json_error(
        workspace, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")
    with monkeypatch.context() as m:
        m.setattr("croupier.cli.json.load", boom)
        code, out = _run_raw(m, capsys, ["check"], "{}")
    assert code == 2 and "error" in out
    monkeypatch.setattr("croupier.cli.cmd_check", boom)
    code, out = _run_raw(monkeypatch, capsys, ["check"], "{}")
    assert code == 2 and "error" in out


def test_decline_with_an_absurdly_nested_id_is_still_audited(
        workspace, monkeypatch, capsys):
    deep = "[" * 1200 + "]" * 1200
    raw = '{"approval_id": ' + deep + ', "reason": "r"}'
    code, _ = _run_raw(monkeypatch, capsys, ["decline"], raw)
    assert code != 0 and _audit_lines(workspace)[-1]["kind"] == "decline"


def test_check_requires_signal_refs(workspace, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["check"], _order(signal_refs=[]))
    assert code == 2 and "signal_refs" in out["error"]


def test_mark_halts_the_sleeve_and_the_next_check_is_rejected(
        workspace, monkeypatch, capsys, fake_router):
    """The full loop: halt written by mark, honoured by the policy loader."""
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], {
        "approval_id": verdict["approval_id"], "ticker": "ACME",
        "side": "buy", "qty": 1000, "price": 2.80})

    routers = iter([fake_router({"ACME": 2.80}), fake_router({"ACME": 1.50})])
    monkeypatch.setattr(cli, "build_router", lambda *_a, **_k: next(routers))

    # The clock has to move: `mark` is idempotent within a day by design, so
    # two marks on one date recompute the same point rather than producing a
    # baseline and then a drawdown. This test read "day 1 / day 2" in its
    # comments but ran both on today's date, and only passed because the bug
    # that made a same-day re-mark stack on itself was still there.
    from datetime import UTC, datetime

    import croupier.marking as marking
    clock = iter([datetime(2026, 8, 26, 12, tzinfo=UTC),
                  datetime(2026, 8, 27, 12, tzinfo=UTC)])
    day = [None]

    def _tick():
        nxt = next(clock, None)
        if nxt is not None:
            day[0] = nxt
        return day[0]

    monkeypatch.setattr(marking, "utcnow", _tick)

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
    shutil.copy(REPO / "config" / "catalysts.example.yaml",
                workspace / "config" / "catalysts.yaml")
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


def test_journal_reads_the_last_observed_health_not_an_unqueried_router(
        workspace, monkeypatch, capsys):
    """The bug only shows with a key configured: build_router() then
    constructs a real TwelveDataMarketData fallback, whose health() defaults
    to DEGRADED before any query -- optimistic by construction. cmd_journal
    used to call .health() on that fresh instance immediately; it must read
    what `mark` actually observed instead. With no key at all, build_router()
    returns DataRouter(None, None), which reports DEAD either way -- that
    case would pass against the old, buggy code too, so it proves nothing."""
    from croupier.data import observed_health

    monkeypatch.setenv("TWELVEDATA_API_KEY", "unused-in-this-test")
    observed_health.save(DataHealth.DEAD)
    cli.main(["journal"])
    text = capsys.readouterr().out
    assert "data feed: `dead`" in text
    assert "DEAD DATA" in text


def test_journal_with_no_prior_mark_reports_dead_not_a_comfortable_degraded(
        workspace, monkeypatch, capsys):
    """Never observed must not default to DEGRADED, which is itself a claim
    of having checked. With a key configured but no prior `mark`, the old
    code constructed a fresh adapter and reported DEGRADED without ever
    asking it anything."""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "unused-in-this-test")
    cli.main(["journal"])
    text = capsys.readouterr().out
    assert "data feed: `dead`" in text


def test_journal_exits_nonzero_for_an_unacknowledged_orphan_alone(
        workspace, monkeypatch, capsys):
    """No pending confirm is involved: the needs-action list alone must trip
    the exit code."""
    _run(monkeypatch, capsys, ["fill"], {
        "approval_id": "ghost", "ticker": "ACME", "side": "buy",
        "qty": 10, "price": 2.79})
    capsys.readouterr()
    assert cli.main(["journal"]) == 1
    assert "ghost" in capsys.readouterr().out
    _run(monkeypatch, capsys, ["ack-fill", "ghost", "--reason", "typo, nothing placed"])
    capsys.readouterr()
    assert cli.main(["journal"]) == 0


# --- round-2 review: ids, implausible fills, replay marker --------------------

BAD_IDS = [123, None, ["x"], {"k": "v"}, "", "  "]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_fill_with_a_non_string_or_empty_approval_id_is_rejected_up_front(
        workspace, monkeypatch, capsys, bad):
    code, out = _run(monkeypatch, capsys, ["fill"], {
        "approval_id": bad, "ticker": "ACME", "side": "buy", "qty": 1, "price": 1.0})
    assert code == 2 and out["ledger"] is False and "approval_id" in out["error"]
    last = _audit_lines(workspace)[-1]
    assert last["kind"] == "fill" and last["orphan"] is True
    assert isinstance(last["approval_id"], str) and last["bad_approval_id"] is True
    assert last["fill_id"]


def test_null_id_orphan_is_ackable_only_by_its_own_handle(
        workspace, monkeypatch, capsys):
    # one null-id orphan and one genuine string-id orphan whose id is "None"
    _run(monkeypatch, capsys, ["fill"], {
        "approval_id": None, "ticker": "ACME", "side": "buy", "qty": 1, "price": 1.0})
    handle = _audit_lines(workspace)[-1]["fill_id"]
    _run(monkeypatch, capsys, ["fill"], {
        "approval_id": "None", "ticker": "ACME", "side": "buy", "qty": 1, "price": 1.0})
    # `ack-fill None` clears only the string-id orphan, never the null-id one
    code, _ = _run(monkeypatch, capsys, ["ack-fill", "None", "--reason", "r"])
    assert code == 0
    capsys.readouterr()
    assert cli.main(["journal"]) == 1
    assert handle in capsys.readouterr().out
    # a handle acknowledges exactly that record
    code, out = _run(monkeypatch, capsys, ["ack-fill", "--handle", handle, "--reason", "r"])
    assert code == 0, out
    capsys.readouterr()
    assert cli.main(["journal"]) == 0


def test_ack_fill_needs_exactly_one_of_id_or_handle(workspace, monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["ack-fill", "--reason", "r"])
    assert code == 1 and "approval_id or --handle" in out["error"]
    code, out = _run(monkeypatch, capsys, ["ack-fill", "x", "--handle", "y", "--reason", "r"])
    assert code == 1 and "approval_id or --handle" in out["error"]


@pytest.mark.parametrize("qty, price", [(1e308, 2.79), (11000, 2.79), (1000, 1e308),
                                        (10001, 2.79)])
def test_implausible_fill_is_refused_not_recorded(
        workspace, monkeypatch, capsys, qty, price):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, qty, price=price)
                     if price == 2.79 else {**_fill_payload(verdict, qty), "price": price})
    assert code == 1 and out["ledger"] is False and "implausible" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()
    last = _audit_lines(workspace)[-1]
    assert last["refused"] is True and last["implausible"] is True


@pytest.mark.parametrize("qty", [10000, 1500])
def test_overfill_up_to_ten_times_is_recorded_and_flagged(
        workspace, monkeypatch, capsys, qty):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, qty))
    assert code == 1 and out["ledger"] is True and out["warnings"]
    assert _ledger_qty(workspace) == [float(qty)]


def test_identical_replay_says_so(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _, first = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    _, second = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    assert not first.get("replay") and second["replay"] is True


@pytest.mark.parametrize("bad", [123, ["x"], {"k": "v"}, None])
def test_decline_with_a_non_string_id_is_recorded_unknown_and_never_blinds_the_journal(
        workspace, monkeypatch, capsys, bad):
    _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["decline"], {"approval_id": bad, "reason": "r"})
    assert code == 1 and out["logged"] is True
    last = _audit_lines(workspace)[-1]
    assert last["kind"] == "decline" and last["known"] is False
    assert isinstance(last["approval_id"], str)
    capsys.readouterr()
    assert cli.main(["journal"]) == 1       # the real pending confirm still shows


# --- round-3 review: price sanity, cost-basis finiteness, replay of refusals ---

def _plant_approval(workspace, approval_id, qty, limit_price, side="buy",
                    approved=True, sleeve="event_driven"):
    from croupier.audit import AuditLog
    intent = {"sleeve": sleeve, "ticker": "ACME", "side": side, "qty": qty}
    if limit_price is not None:
        intent["limit_price"] = limit_price
    (workspace / "data").mkdir(exist_ok=True)
    AuditLog(workspace / "data" / "audit.jsonl")._append({
        "ts": "2026-08-28T10:00:00+00:00", "kind": "check", "intent": intent,
        "approved": approved, "approval_id": approval_id, "requires_confirm": True})


def _fill_at(approval_id, qty, price, side="buy"):
    return {"approval_id": approval_id, "ticker": "ACME", "side": side,
            "qty": qty, "price": price}


@pytest.mark.parametrize("price", [1e300, 31.0])     # buy 11x above its limit
def test_absurd_price_is_refused_not_recorded(workspace, monkeypatch, capsys, price):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000, price=price))
    assert code == 1 and out["ledger"] is False and "implausible" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()
    last = _audit_lines(workspace)[-1]
    assert last["refused"] is True and last["implausible"] is True


@pytest.mark.parametrize("price, flagged", [
    (2.80, False), (2.80 * 1.005, False), (2.80 * 1.01, False),   # within 1% slip
    (2.80 * 1.02, True), (4.2, True),                              # above the limit
    (28.0, True),                                                  # exactly 10x: recorded
])
def test_buy_price_above_limit_is_flagged_beyond_tolerance(
        workspace, monkeypatch, capsys, price, flagged):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000, price=price))
    assert out["ledger"] is True
    assert bool(out["warnings"]) is flagged and code == (1 if flagged else 0)
    assert bool(_audit_lines(workspace)[-1].get("price_above_limit")) is flagged
    assert len(_ledger_qty(workspace)) == 1


@pytest.mark.parametrize("price, flagged", [
    (2.5, False), (2.0, False), (2.0 * 0.995, False), (2.0 * 0.99, False),
    (2.0 * 0.98, True), (0.5, True),
])
def test_sell_price_below_limit_is_flagged_beyond_tolerance(
        workspace, monkeypatch, capsys, price, flagged):
    _plant_approval(workspace, "buy1", 1000, 2.80)
    _plant_approval(workspace, "sell1", 500, 2.0, side="sell")
    _run(monkeypatch, capsys, ["fill"], _fill_at("buy1", 1000, 2.80))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("sell1", 500, price, "sell"))
    assert out["ledger"] is True
    assert bool(out["warnings"]) is flagged and code == (1 if flagged else 0)
    assert bool(_audit_lines(workspace)[-1].get("price_below_limit")) is flagged


def test_missing_limit_flags_nothing_but_cost_basis_is_still_checked(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "n1", 1000, None)
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("n1", 1000, 1e300))
    assert code == 0 and out["ledger"] is True and out["warnings"] == []
    _plant_approval(workspace, "n2", 1000, None)
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("n2", 1000, 1e308))
    assert code == 1 and out["ledger"] is False and "cost basis" in out["error"]


def test_fill_that_would_make_cost_basis_non_finite_is_refused(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "c1", 1e154, 0.9e154)
    _plant_approval(workspace, "c2", 1e154, 0.9e154)
    code, _ = _run(monkeypatch, capsys, ["fill"], _fill_at("c1", 1e154, 0.9e154))
    assert code == 0
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("c2", 1e154, 0.9e154))
    assert code == 1 and out["ledger"] is False and "cost basis" in out["error"]
    last = _audit_lines(workspace)[-1]
    assert last["refused"] is True and last["implausible"] is True
    assert len(_ledger_qty(workspace)) == 1


def test_exactly_ten_times_survives_float_rounding(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order(qty=623.78))
    assert 10 * 623.78 < 6237.8       # the rounding the tolerance exists for
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 6237.8))
    assert out["ledger"] is True and any("overfill" in w for w in out["warnings"])


def test_replaying_a_refused_fill_does_not_log_a_second_record(
        workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 10001))
    n = len(_audit_lines(workspace))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 10001))
    assert code == 1 and out["replay"] is True and out["ledger"] is False
    assert len(_audit_lines(workspace)) == n
    # a *different* refused report is still a new record
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 10002))
    assert len(_audit_lines(workspace)) == n + 1


def test_replaying_a_conflicting_fill_does_not_log_a_second_record(
        workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000))
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 900))
    n = len(_audit_lines(workspace))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 900))
    assert code == 1 and out["replay"] is True
    assert len(_audit_lines(workspace)) == n


def test_string_id_fill_cannot_join_a_rejected_check(workspace, monkeypatch, capsys):
    """#19 property, independent of the null-id input check."""
    _plant_approval(workspace, "rej1", 1000, 2.80, approved=False)
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("rej1", 1000, 2.80))
    assert code == 1 and out["ledger"] is False
    assert _audit_lines(workspace)[-1]["orphan"] is True
    assert not (workspace / "data" / "ledger.db").exists()


def test_replay_of_a_recorded_fill_is_not_rechecked_against_its_own_cost_basis(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "c1", 1e154, 0.9e154)
    _run(monkeypatch, capsys, ["fill"], _fill_at("c1", 1e154, 0.9e154))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("c1", 1e154, 0.9e154))
    assert code == 0 and out["replay"] is True and out["ledger"] is True


def test_replay_of_a_fill_recorded_before_the_ceilings_existed_is_harmless(
        workspace, monkeypatch, capsys):
    from croupier.ledger import new_fill
    _plant_approval(workspace, "old1", 1000, 2.80)
    with Ledger(workspace / "data" / "ledger.db") as led:
        led.record_fill(new_fill("old1", "event_driven", "ACME", "buy", 50000, 2.79))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("old1", 50000, 2.79))
    assert out["replay"] is True and out["ledger"] is True


# --- favourable-direction fills are recorded and flagged, not refused -----------

def test_buy_filled_far_below_limit_is_recorded_and_flagged(
        workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())     # limit 2.80
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000, price=0.25))
    assert code == 1 and out["ledger"] is True
    assert any("better than limit" in w for w in out["warnings"])
    last = _audit_lines(workspace)[-1]
    assert last["price_better_than_limit"] is True and not last.get("refused")
    with Ledger(workspace / "data" / "ledger.db") as led:
        assert led.fills()[0].price == 0.25


def test_sell_filled_far_above_limit_is_recorded_and_flagged(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "buy1", 1000, 2.80)
    _plant_approval(workspace, "sell1", 500, 2.0, side="sell")
    _run(monkeypatch, capsys, ["fill"], _fill_at("buy1", 1000, 2.80))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("sell1", 500, 25.0, "sell"))
    assert code == 1 and out["ledger"] is True
    assert _audit_lines(workspace)[-1]["price_better_than_limit"] is True
    assert _ledger_qty(workspace) == [1000, 500]


def test_sell_filled_far_below_limit_is_still_refused(workspace, monkeypatch, capsys):
    _plant_approval(workspace, "buy1", 1000, 2.80)
    _plant_approval(workspace, "sell1", 500, 2.0, side="sell")
    _run(monkeypatch, capsys, ["fill"], _fill_at("buy1", 1000, 2.80))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("sell1", 500, 0.1, "sell"))
    assert code == 1 and out["ledger"] is False and "implausible" in out["error"]
    assert _ledger_qty(workspace) == [1000]


def test_sell_with_non_finite_notional_is_still_refused(workspace, monkeypatch, capsys):
    _plant_approval(workspace, "buy1", 1000, 2.80)
    _plant_approval(workspace, "sell1", 500, 2.0, side="sell")
    _run(monkeypatch, capsys, ["fill"], _fill_at("buy1", 1000, 2.80))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("sell1", 500, 1e308, "sell"))
    assert code == 1 and out["ledger"] is False and "implausible" in out["error"]


def test_moderate_price_improvement_is_not_flagged(workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000, price=0.30))
    assert code == 0 and out["warnings"] == []      # exactly ~9.3x below: ordinary
    assert not _audit_lines(workspace)[-1].get("price_better_than_limit")


# --- croupier reconcile ---------------------------------------------------------

def _refused_handle(workspace, monkeypatch, capsys, qty=11000, price=2.79):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    code, _ = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, qty, price=price))
    assert code == 1
    last = _audit_lines(workspace)[-1]
    assert last["refused"] is True
    return verdict["approval_id"], last["fill_id"]


def _reconcile(monkeypatch, capsys, handle, qty="1000", price="2.79",
               reason="broker confirms", force=False):
    return _run(monkeypatch, capsys, ["reconcile", handle, f"--qty={qty}",
                                      f"--price={price}", f"--reason={reason}"]
                + (["--force"] if force else []))


def test_reconcile_records_a_refused_fill_exactly_once(workspace, monkeypatch, capsys):
    aid, handle = _refused_handle(workspace, monkeypatch, capsys)
    assert not (workspace / "data" / "ledger.db").exists()
    code, out = _reconcile(monkeypatch, capsys, handle, reason="broker statement: 1000")
    assert code == 0 and out["logged"] is True and out["ledger"] is True, out
    assert _ledger_qty(workspace) == [1000]
    with Ledger(workspace / "data" / "ledger.db") as led:
        assert led.fills()[0].approval_id == aid
        assert led.positions()[0].cost_basis == pytest.approx(1000 * 2.79)
    rec = _audit_lines(workspace)[-1]
    assert rec["kind"] == "reconcile" and rec["fill_id"] == handle
    assert rec["qty"] == 1000 and rec["price"] == 2.79
    assert rec["reason"] == "broker statement: 1000" and rec["approval_id"] == aid
    capsys.readouterr()
    assert cli.main(["journal"]) == 0          # resolved: no longer Needs action
    capsys.readouterr()
    # a second reconcile is refused and adds nothing
    n = len(_audit_lines(workspace))
    code, out = _reconcile(monkeypatch, capsys, handle)
    assert code == 1 and out["logged"] is False
    assert _ledger_qty(workspace) == [1000] and len(_audit_lines(workspace)) == n


def test_reconcile_may_record_operator_values_not_the_reported_ones(
        workspace, monkeypatch, capsys):
    _, handle = _refused_handle(workspace, monkeypatch, capsys, qty=11000)
    code, _ = _reconcile(monkeypatch, capsys, handle, qty="1000", price="2.80")
    assert code == 0 and _ledger_qty(workspace) == [1000]


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_reconcile_needs_a_non_blank_reason(workspace, monkeypatch, capsys, reason):
    _, handle = _refused_handle(workspace, monkeypatch, capsys)
    code, out = _reconcile(monkeypatch, capsys, handle, reason=reason)
    assert code == 1 and out["logged"] is False and "reason" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()


@pytest.mark.parametrize("qty, price", [
    ("nan", "2.79"), ("inf", "2.79"), ("-inf", "2.79"), ("0", "2.79"), ("-5", "2.79"),
    ("100", "nan"), ("100", "inf"), ("100", "0"), ("100", "-2.79")])
def test_reconcile_needs_finite_positive_qty_and_price(
        workspace, monkeypatch, capsys, qty, price):
    _, handle = _refused_handle(workspace, monkeypatch, capsys)
    n = len(_audit_lines(workspace))
    code, out = _reconcile(monkeypatch, capsys, handle, qty=qty, price=price)
    assert code == 1 and out["logged"] is False and "finite" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()
    assert len(_audit_lines(workspace)) == n


def test_reconcile_refuses_an_unknown_handle(workspace, monkeypatch, capsys):
    _refused_handle(workspace, monkeypatch, capsys)
    code, out = _reconcile(monkeypatch, capsys, "nosuchhandle")
    assert code == 1 and out["logged"] is False and "unknown" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()


def test_reconcile_refuses_a_fill_that_was_recorded_in_the_ledger(
        workspace, monkeypatch, capsys):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())
    _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1500))    # overfill: recorded
    handle = _audit_lines(workspace)[-1]["fill_id"]
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1500")
    assert code == 1 and "refused or orphan" in out["error"]
    assert _ledger_qty(workspace) == [1500]


def test_reconcile_refuses_an_already_acknowledged_fill(workspace, monkeypatch, capsys):
    aid, handle = _refused_handle(workspace, monkeypatch, capsys)
    _run(monkeypatch, capsys, ["ack-fill", "--handle", handle, "--reason", "r"])
    code, out = _reconcile(monkeypatch, capsys, handle)
    assert code == 1 and "already" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()


def test_reconcile_of_an_orphan_with_a_sleeve_records_it(workspace, monkeypatch, capsys):
    _plant_approval(workspace, "known1", 1000, 2.80)
    _run(monkeypatch, capsys, ["fill"], {**_fill_at("known1", 500, 2.8), "ticker": "OTHER"})
    last = _audit_lines(workspace)[-1]
    assert last["orphan"] is True and last["sleeve"]
    code, out = _reconcile(monkeypatch, capsys, last["fill_id"], qty="500", price="2.8")
    assert code == 0 and _ledger_qty(workspace) == [500]


def test_reconcile_of_an_orphan_without_a_sleeve_is_refused(workspace, monkeypatch, capsys):
    _run(monkeypatch, capsys, ["fill"], {
        "approval_id": None, "ticker": "ACME", "side": "buy", "qty": 1, "price": 1.0})
    handle = _audit_lines(workspace)[-1]["fill_id"]
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1", price="1")
    assert code == 1 and "sleeve" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()


def test_reconcile_that_would_oversell_is_refused_and_not_logged(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "sell1", 500, 2.0, side="sell")
    _run(monkeypatch, capsys, ["fill"], _fill_at("sell1", 500, 2.0, "sell"))
    handle = _audit_lines(workspace)[-1]["fill_id"]
    n = len(_audit_lines(workspace))
    code, out = _reconcile(monkeypatch, capsys, handle, qty="500", price="2.0")
    assert code == 1 and out["logged"] is False and "exceeds" in out["error"]
    assert len(_audit_lines(workspace)) == n


def test_reconcile_is_listed_as_operator_only_for_the_agent():
    text = (REPO / "croupier" / "executor" / "AGENT.md").read_text()
    hard = text[text.index("## Hard prohibitions"):]
    assert "croupier reconcile" in hard.split("- Never trade")[0]


# --- review round: reconcile bounds, idempotency, favourable floor ---------------

@pytest.mark.parametrize("force", [False, True])
def test_reconcile_never_records_a_non_finite_notional(workspace, monkeypatch, capsys, force):
    _, handle = _refused_handle(workspace, monkeypatch, capsys)
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1e200", price="1e200", force=force)
    assert code == 1 and out["logged"] is False and "finite" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()


def test_reconcile_never_makes_the_sleeve_cost_basis_non_finite_even_forced(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "c1", 1e154, 0.9e154)
    _plant_approval(workspace, "c2", 1e154, 0.9e154)
    _run(monkeypatch, capsys, ["fill"], _fill_at("c1", 1e154, 0.9e154))
    _run(monkeypatch, capsys, ["fill"], _fill_at("c2", 1e154, 0.9e154))
    handle = _audit_lines(workspace)[-1]["fill_id"]
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1e154", price="0.9e154", force=True)
    assert code == 1 and out["logged"] is False and "cost basis" in out["error"]
    assert len(_ledger_qty(workspace)) == 1


def test_reconcile_refuses_qty_far_over_approved_unless_forced(workspace, monkeypatch, capsys):
    _, handle = _refused_handle(workspace, monkeypatch, capsys)       # approved 1000
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1000000")
    assert code == 1 and out["logged"] is False and "--force" in out["error"]
    assert not (workspace / "data" / "ledger.db").exists()
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1000000", force=True)
    assert code == 0 and out["force"] is True
    assert out["reported_qty"] == 11000 and out["reported_price"] == 2.79
    rec = _audit_lines(workspace)[-1]
    assert rec["force"] is True and rec["reported_qty"] == 11000 and rec["qty"] == 1000000
    assert rec["reported_price"] == 2.79


def test_reconcile_refuses_qty_far_over_the_reported_qty_unless_forced(
        workspace, monkeypatch, capsys):
    _plant_approval(workspace, "big", 100000, 2.80)
    _run(monkeypatch, capsys, ["fill"], _fill_at("big", 1_000_001, 2.79))      # >10x: refused
    handle = _audit_lines(workspace)[-1]["fill_id"]
    _plant_approval(workspace, "ghost", 1, 2.80)
    _run(monkeypatch, capsys, ["fill"], {**_fill_at("ghost", 5, 2.8), "ticker": "OTHER"})
    h2 = _audit_lines(workspace)[-1]["fill_id"]                      # orphan, reported 5
    code, out = _reconcile(monkeypatch, capsys, h2, qty="500", price="2.8")
    assert code == 1 and "reported" in out["error"] and "--force" in out["error"]
    code, out = _reconcile(monkeypatch, capsys, h2, qty="500", price="2.8", force=True)
    assert code == 0 and handle


def test_reconcile_refuses_adverse_price_unless_forced(workspace, monkeypatch, capsys):
    _, handle = _refused_handle(workspace, monkeypatch, capsys, qty=1000, price=31.0)
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1000", price="31.0")
    assert code == 1 and "--force" in out["error"]
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1000", price="31.0", force=True)
    assert code == 0


def test_reconcile_accepts_a_favourable_extreme_but_not_below_the_price_floor(
        workspace, monkeypatch, capsys):
    _, handle = _refused_handle(workspace, monkeypatch, capsys, qty=1000, price=5e-324)
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1000", price="1e-7")
    assert code == 1 and "--force" in out["error"] and "floor" in out["error"]
    code, out = _reconcile(monkeypatch, capsys, handle, qty="1000", price="0.01")   # 280x gap
    assert code == 0 and out["force"] is False


def test_failed_audit_write_then_rerun_records_the_fill_once(workspace, monkeypatch, capsys):
    from croupier.audit import AuditLog
    _, handle = _refused_handle(workspace, monkeypatch, capsys)
    real = AuditLog.log_reconcile

    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(AuditLog, "log_reconcile", boom)
    with pytest.raises(OSError):
        _reconcile(monkeypatch, capsys, handle)
    assert _ledger_qty(workspace) == [1000]
    monkeypatch.setattr(AuditLog, "log_reconcile", real)
    code, out = _reconcile(monkeypatch, capsys, handle)
    assert code == 0 and _ledger_qty(workspace) == [1000]
    assert _audit_lines(workspace)[-1]["kind"] == "reconcile"


def test_failed_audit_write_with_taken_key_rerun_records_the_fill_once(
        workspace, monkeypatch, capsys):
    from croupier.audit import AuditLog
    aid, handle = _refused_handle(workspace, monkeypatch, capsys)
    _run(monkeypatch, capsys, ["fill"], _fill_payload({"approval_id": aid}, 1000, price=2.80))
    # the refused fill is still open; it reconciles under reconcile:<fid>
    real = AuditLog.log_reconcile
    monkeypatch.setattr(AuditLog, "log_reconcile",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        _reconcile(monkeypatch, capsys, handle)
    monkeypatch.setattr(AuditLog, "log_reconcile", real)
    code, _ = _reconcile(monkeypatch, capsys, handle)
    assert code == 0 and sorted(_ledger_qty(workspace)) == [1000, 1000]


@pytest.mark.parametrize("price, refused", [
    (0.028, False), (0.0279, True), (0.0277, True), (5e-324, True), (0.2, False)])
def test_buy_favourable_bound_is_limit_over_100(workspace, monkeypatch, capsys, price, refused):
    _, verdict = _run(monkeypatch, capsys, ["check"], _order())        # limit 2.80
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_payload(verdict, 1000, price=price))
    assert (out["ledger"] is False) is refused
    if refused:
        last = _audit_lines(workspace)[-1]
        assert last["refused"] is True and last["implausible"] is True


@pytest.mark.parametrize("price, refused", [(200.0, False), (200.5, True), (1e300, True)])
def test_sell_favourable_bound_is_limit_times_100(workspace, monkeypatch, capsys, price, refused):
    _plant_approval(workspace, "buy1", 1000, 2.80)
    _plant_approval(workspace, "sell1", 500, 2.0, side="sell")
    _run(monkeypatch, capsys, ["fill"], _fill_at("buy1", 1000, 2.80))
    code, out = _run(monkeypatch, capsys, ["fill"], _fill_at("sell1", 500, price, "sell"))
    assert (out["ledger"] is False) is refused
