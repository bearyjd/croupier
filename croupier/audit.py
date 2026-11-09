"""Append-only JSONL audit log (PRP-001 invariant 1).

No log write, no approval: check_and_log() writes the record and fsyncs
BEFORE returning the verdict. Exportable as-is for compliance review.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path

from croupier.catalysts import CatalystCalendar
from croupier.data.base import DataHealth
from croupier.gates.pipeline import PolicyConfig, check
from croupier.models import AccountSnapshot, OrderIntent, Verdict, utcnow

_MAX_LOGGED_INT = 2**63


def _clean(value: object) -> object:
    """Make a record strict-JSON safe, recursively.

    Non-finite floats and absurdly large ints become text: a hostile value
    must be recorded, never raise and suppress the record.
    """
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= _MAX_LOGGED_INT:
        return str(value)
    return value


def valid_approval_id(value: object) -> str | None:
    """The one normalisation for approval ids: a non-blank string, else None.

    Anything else (null, a number, a list) can never name an approval, so it
    must never be matched, hashed or compared as one.
    """
    return value if isinstance(value, str) and value.strip() else None


def _safe_clean(record: dict) -> dict:
    """_clean, but a value nested past the recursion limit is summarised
    rather than raised: the record must still be written."""
    try:
        return _clean(record)
    except RecursionError:
        out = {}
        for key, value in record.items():
            try:
                out[key] = _clean(value)
            except RecursionError:
                out[key] = "<value too deeply nested to log>"
        return out


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def check_and_log(self, intent: OrderIntent, cfg: PolicyConfig,
                      snap: AccountSnapshot, auto_spent_today: float = 0.0,
                      data_health: DataHealth = DataHealth.FRESH,
                      calendar: CatalystCalendar | None = None,
                      today: date | None = None,
                      held_qty: float | None = None) -> Verdict:
        verdict = check(intent, cfg, snap, auto_spent_today, data_health,
                        calendar=calendar, today=today, held_qty=held_qty)
        record = {
            "ts": utcnow().isoformat(),
            "kind": "check",
            "intent": asdict(intent),
            "snapshot": {"total_value": snap.total_value, "cash": snap.cash},
            "data_health": str(data_health),
            "decisions": [asdict(d) for d in verdict.decisions],
            "approved": verdict.approved,
            "approval_id": verdict.approval_id,
            "requires_confirm": verdict.requires_confirm,
        }
        self._append(record)
        return verdict

    def log_fill(self, approval_id: str, ticker: str, side: str,
                 qty: float, price: float, sleeve: str | None = None,
                 orphan: bool = False, flags: tuple[str, ...] = ()) -> None:
        """Record a fill. An orphan fill (no matching approval) is still
        logged — an ungated trade is precisely what the audit trail is for.

        ``flags`` marks a fill that reached (or was refused by) the ledger but
        needs an operator: ``overfill``, ``fill_after_decline``, ``refused``,
        ``implausible``, ``bad_approval_id``. Each becomes a ``<flag>: true``
        field. Every fill record also gets a unique ``fill_id``: the handle
        that acknowledges it when its approval_id cannot (null, non-string).
        """
        self._append({"ts": utcnow().isoformat(), "kind": "fill",
                      "fill_id": uuid.uuid4().hex[:12],
                      "approval_id": approval_id, "sleeve": sleeve,
                      "ticker": ticker, "side": side, "qty": qty,
                      "price": price, "orphan": orphan,
                      **{flag: True for flag in flags}})

    def log_ack_fill(self, approval_id: str | None, reason: str,
                     handle: str | None = None) -> None:
        """Record the operator's acknowledgement of a flagged/orphan fill,
        by approval_id or, for a record with no usable id, by its handle."""
        self._append({"ts": utcnow().isoformat(), "kind": "ack_fill",
                      "approval_id": approval_id, "handle": handle,
                      "reason": reason})

    def log_decline(self, approval_id: str, reason: str,
                    known: bool = True) -> None:
        """Record that an approved CONFIRM-mode order will not be placed.

        An approval with no fill is a pending confirm forever — the log is
        append-only, so the only way to retire one is another record saying
        so. Declining is an operator act, not a gate decision: the check that
        produced the approval stays in the trail unchanged, and this record
        sits after it. ``known=False`` marks a decline of an approval_id the
        log has never seen; it is still written, for the same reason an
        orphan fill is.
        """
        self._append({"ts": utcnow().isoformat(), "kind": "decline",
                      "approval_id": approval_id, "reason": reason,
                      "known": known})

    def log_event(self, event: str, detail: dict) -> None:
        """Record a non-order policy event (halts, data-health transitions)."""
        self._append({"ts": utcnow().isoformat(), "kind": "event",
                      "event": event, **detail})

    def records(self) -> list[dict]:
        """Every well-formed record, oldest first. A corrupt line is skipped,
        never raised: the journal must still render after a bad write."""
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def find_approval(self, approval_id: str) -> dict | None:
        """Return the approved check record for ``approval_id``, if any.

        A fill must trace back to an approval: this is the join that keeps the
        ledger from holding a position no gate ever cleared.
        """
        if not self.path.exists():
            return None
        found = None
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("kind") == "check" and rec.get("approved")
                        and rec.get("approval_id") == approval_id):
                    found = rec
        return found

    def _append(self, record: dict) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(_safe_clean(record), default=str, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
