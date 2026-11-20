# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""The audit JSONL schema: every writer's output keys must match its TypedDict."""
from typing import get_type_hints

from croupier import audit_schema as s
from croupier.audit import AuditLog
from croupier.catalysts import CatalystCalendar
from croupier.journal import _PROBLEMS
from croupier.gates.pipeline import PolicyConfig, SleeveConfig
from croupier.models import AccountSnapshot, Mode, OrderIntent


def _keys(td) -> set[str]:
    return set(get_type_hints(td))


_FLAGS = tuple(k for k, _ in _PROBLEMS if k != "orphan") + ("bad_approval_id",)


def _write_all(log: AuditLog) -> None:
    cfg = PolicyConfig(
        sleeves={"event_driven": SleeveConfig("event_driven", 10, 3, mode=Mode.CONFIRM)},
        denylist={}, sector_denylist={}, ticker_sector={},
    )
    intent = OrderIntent(sleeve="event_driven", ticker="ACME", side="buy", qty=1000,
                         limit_price=2.80, signal_refs=("8-K:0001",), thesis="t")
    log.check_and_log(intent, cfg, AccountSnapshot(100_000.0, 50_000.0),
                      calendar=CatalystCalendar.empty())
    log.log_fill("a1", "ACME", "buy", 1.0, 2.0, sleeve="event_driven",
                 flags=_FLAGS)
    log.log_ack_fill("a1", "ok", handle="h")
    log.log_reconcile("f1", "a1", "event_driven", "ACME", "buy", 1.0, 2.0, "r")
    log.log_decline("a1", "no")
    log.log_event("halt", {"why": "x"})


def test_every_writer_output_matches_its_typeddict(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    _write_all(log)
    recs = {r["kind"]: r for r in log.records()}
    assert set(recs) == set(s.RECORD_TYPES)
    for kind, rec in recs.items():
        # An event's keys beyond the envelope are caller-supplied detail.
        extra = set(rec) - _keys(s.RECORD_TYPES[kind])
        assert extra == ({"why"} if kind == s.Kind.EVENT else set()), kind
    # Non-flag-driven kinds carry every declared key: a dropped field is drift too.
    for kind in (s.Kind.CHECK, s.Kind.ACK_FILL, s.Kind.RECONCILE, s.Kind.DECLINE):
        assert set(recs[kind]) == _keys(s.RECORD_TYPES[kind]), kind


def test_kind_constants_are_the_on_disk_strings():
    assert {s.Kind.CHECK, s.Kind.FILL, s.Kind.ACK_FILL, s.Kind.RECONCILE,
            s.Kind.DECLINE, s.Kind.EVENT} == set(s.RECORD_TYPES)
    assert s.Kind.ACK_FILL == "ack_fill"


def test_every_journal_problem_key_is_declared_on_fill_record():
    assert {k for k, _ in _PROBLEMS} <= _keys(s.FillRecord)

