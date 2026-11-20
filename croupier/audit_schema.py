# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""The audit.jsonl record schema: one TypedDict per ``kind``.

The on-disk format is unchanged; this module is the single place that names
it. Writers (audit.py) build records through these shapes and readers match
on ``Kind`` instead of bare strings. ``total=False`` marks fields that are
optional or flag-driven (a fill's ``<flag>: true`` keys, an event's detail).
"""
from __future__ import annotations

from typing import Final, TypedDict


class Kind:
    CHECK: Final = "check"
    FILL: Final = "fill"
    ACK_FILL: Final = "ack_fill"
    RECONCILE: Final = "reconcile"
    DECLINE: Final = "decline"
    EVENT: Final = "event"


class CheckRecord(TypedDict):
    ts: str
    kind: str
    intent: dict
    snapshot: dict
    data_health: str
    decisions: list[dict]
    approved: bool
    approval_id: str | None
    requires_confirm: bool


class FillRecord(TypedDict, total=False):
    ts: str
    kind: str
    fill_id: str
    approval_id: str | None
    sleeve: str | None
    ticker: str
    side: str
    qty: float
    price: float
    orphan: bool
    # Flag-driven (log_fill ``flags``): each present as ``<flag>: True``.
    overfill: bool
    fill_after_decline: bool
    refused: bool
    implausible: bool
    bad_approval_id: bool
    price_above_limit: bool
    price_below_limit: bool
    price_better_than_limit: bool


class AckFillRecord(TypedDict):
    ts: str
    kind: str
    approval_id: str | None
    handle: str | None
    reason: str


class ReconcileRecord(TypedDict):
    ts: str
    kind: str
    fill_id: str
    approval_id: str
    sleeve: str
    ticker: str
    side: str
    qty: float
    price: float
    reason: str
    reported_qty: object
    reported_price: object
    force: bool
    inserted: bool


class DeclineRecord(TypedDict):
    ts: str
    kind: str
    approval_id: str
    reason: str
    known: bool


class EventRecord(TypedDict, total=False):
    ts: str
    kind: str
    event: str
    # plus free-form detail keys supplied by the caller


RECORD_TYPES: Final[dict[str, type]] = {
    Kind.CHECK: CheckRecord,
    Kind.FILL: FillRecord,
    Kind.ACK_FILL: AckFillRecord,
    Kind.RECONCILE: ReconcileRecord,
    Kind.DECLINE: DeclineRecord,
    Kind.EVENT: EventRecord,
}
