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
    assert code == 1 and out["ledger"] is False
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
