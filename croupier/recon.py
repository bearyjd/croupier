# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Snapshot-vs-ledger reconciliation.

Compares the cost basis the agent reported in its latest audit snapshot with
what the ledger holds, per ticker and per sleeve. A gap means a fill was
reported to the broker but never to Croupier (ledger short), or the reverse.

Not the operator `reconcile` command: that fixes one refused fill. This only
detects; it never writes.

The snapshot is a point in time. The ledger side is taken as of the snapshot's
timestamp, so a later fill is not a false discrepancy.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from croupier.audit import AuditLog
from croupier.audit_schema import Kind
from croupier.ledger import Ledger, Position
from croupier.models import parse_ts

TOLERANCE_USD = 1.0
TOLERANCE_PCT = 0.5


@dataclass(frozen=True)
class Discrepancy:
    scope: str                 # "ticker" or "sleeve"
    key: str
    ledger: float
    reported: float | None     # None: absent from the snapshot

    @property
    def diff(self) -> float:
        return (self.reported or 0.0) - self.ledger


@dataclass(frozen=True)
class ReconReport:
    checked: bool              # False: no snapshot, or one without basis fields
    snapshot_ts: str | None = None
    discrepancies: tuple[Discrepancy, ...] = ()
    ledger_only: tuple[str, ...] = ()      # tickers held but not in the snapshot
    reported_only: tuple[str, ...] = ()    # tickers in the snapshot, not held

    @property
    def ok(self) -> bool:
        return not (self.discrepancies or self.ledger_only or self.reported_only)


def _beyond(ledger: float, reported: float, usd: float, pct: float) -> bool:
    """Outside tolerance only when off by more than BOTH the USD and percent bands."""
    diff = abs(reported - ledger)
    return diff > usd and diff > max(abs(ledger), abs(reported)) * pct / 100.0


def _numeric(raw: object) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): float(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def reconcile_snapshot(record: Mapping | None, positions: Iterable[Position],
                       tolerance_usd: float = TOLERANCE_USD,
                       tolerance_pct: float = TOLERANCE_PCT) -> ReconReport:
    """``record`` is the latest ``check`` audit record, or None."""
    snap = (record or {}).get("snapshot")
    if (not isinstance(snap, Mapping) or "sleeve_cost_basis" not in snap
            or "position_cost_basis" not in snap):
        return ReconReport(checked=False)

    by_ticker: dict[str, float] = {}
    by_sleeve: dict[str, float] = {}
    for p in positions:
        t = p.ticker.upper()
        by_ticker[t] = by_ticker.get(t, 0.0) + p.cost_basis
        by_sleeve[p.sleeve] = by_sleeve.get(p.sleeve, 0.0) + p.cost_basis

    rep_ticker: dict[str, float] = {}
    for k, v in _numeric(snap["position_cost_basis"]).items():
        rep_ticker[k.upper()] = rep_ticker.get(k.upper(), 0.0) + v
    rep_sleeve = _numeric(snap["sleeve_cost_basis"])

    out: list[Discrepancy] = []
    for scope, ledger, reported in (("ticker", by_ticker, rep_ticker),
                                    ("sleeve", by_sleeve, rep_sleeve)):
        for key in sorted(ledger.keys() & reported.keys()):
            if _beyond(ledger[key], reported[key], tolerance_usd, tolerance_pct):
                out.append(Discrepancy(scope, key, ledger[key], reported[key]))
        if scope == "sleeve":
            # A sleeve held on one side only is a discrepancy against zero.
            for key in sorted(ledger.keys() ^ reported.keys()):
                lv, rv = ledger.get(key, 0.0), reported.get(key, 0.0)
                if _beyond(lv, rv, tolerance_usd, tolerance_pct):
                    out.append(Discrepancy(scope, key, lv,
                                           reported.get(key)))

    ledger_only = tuple(sorted(
        t for t in by_ticker.keys() - rep_ticker.keys()
        if _beyond(by_ticker[t], 0.0, tolerance_usd, tolerance_pct)))
    reported_only = tuple(sorted(
        t for t in rep_ticker.keys() - by_ticker.keys()
        if _beyond(0.0, rep_ticker[t], tolerance_usd, tolerance_pct)))
    return ReconReport(True, record.get("ts"), tuple(out), ledger_only, reported_only)


def latest_check_record(records: Iterable[Mapping]) -> Mapping | None:
    """The newest ``check`` record that carries a snapshot."""
    for rec in reversed(list(records)):
        if rec.get("kind") == Kind.CHECK and isinstance(rec.get("snapshot"), Mapping):
            return rec
    return None


def reconcile_ledger(audit: AuditLog, ledger: Ledger) -> ReconReport:
    """Latest snapshot against the ledger as it stood at the snapshot's time,
    so a fill recorded after the snapshot is not a false discrepancy."""
    record = latest_check_record(audit.records())
    as_of = parse_ts(record.get("ts")) if record else None
    return reconcile_snapshot(record, ledger.positions(as_of))
