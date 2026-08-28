"""Append-only JSONL audit log (PRP-001 invariant 1).

No log write, no approval: check_and_log() writes the record and fsyncs
BEFORE returning the verdict. Exportable as-is for compliance review.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path

from croupier.catalysts import CatalystCalendar
from croupier.data.base import DataHealth
from croupier.gates.pipeline import PolicyConfig, check
from croupier.models import AccountSnapshot, OrderIntent, Verdict, utcnow


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def check_and_log(self, intent: OrderIntent, cfg: PolicyConfig,
                      snap: AccountSnapshot, auto_spent_today: float = 0.0,
                      data_health: DataHealth = DataHealth.FRESH,
                      calendar: CatalystCalendar | None = None,
                      today: date | None = None) -> Verdict:
        verdict = check(intent, cfg, snap, auto_spent_today, data_health,
                        calendar=calendar, today=today)
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
                 orphan: bool = False) -> None:
        """Record a fill. An orphan fill (no matching approval) is still
        logged — an ungated trade is precisely what the audit trail is for."""
        self._append({"ts": utcnow().isoformat(), "kind": "fill",
                      "approval_id": approval_id, "sleeve": sleeve,
                      "ticker": ticker, "side": side, "qty": qty,
                      "price": price, "orphan": orphan})

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
                if rec.get("kind") == "check" and rec.get("approval_id") == approval_id:
                    found = rec
        return found

    def _append(self, record: dict) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
