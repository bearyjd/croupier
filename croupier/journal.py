# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""The daily journal: what the operator reads before deciding anything.

Renders auth status, data health, open positions against their sleeve
ceilings, orders still waiting on a human confirm, and any active halts.

Ceilings are percentages of account value, and Croupier holds no broker
credentials — so account value comes from the most recent snapshot the agent
supplied with a check. The journal always states how old that snapshot is
rather than implying a live number.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from croupier.audit import AuditLog
from croupier.catalysts import CatalystEvent
from croupier.data.base import DataHealth
from croupier.gates.pipeline import SleeveConfig
from croupier.ledger import Ledger, Position
from croupier.policy import Policy
from croupier.state import Halt

JOURNAL_DIR = Path("data/journal")


@dataclass(frozen=True)
class SleeveLine:
    sleeve: str
    mode: str
    cost_basis: float
    ceiling: float | None
    positions: tuple[Position, ...]
    halt: Halt | None

    @property
    def utilisation_pct(self) -> float | None:
        if not self.ceiling:
            return None
        return self.cost_basis / self.ceiling * 100.0


@dataclass(frozen=True)
class PendingConfirm:
    approval_id: str
    ts: str
    sleeve: str
    ticker: str
    side: str
    qty: float
    limit_price: float
    thesis: str


@dataclass(frozen=True)
class AuthStatus:
    configured: bool
    days_until_reauth: float | None
    nag: bool
    detail: str


@dataclass(frozen=True)
class JournalReport:
    as_of: date
    data_health: DataHealth
    auth: AuthStatus
    account_value: float | None
    account_snapshot_ts: str | None
    sleeves: tuple[SleeveLine, ...]
    pending: tuple[PendingConfirm, ...]
    halts: tuple[Halt, ...]
    freezes: tuple[CatalystEvent, ...] = ()   # active catalyst freezes today
    freeze_trading_days: int = 0


def latest_account_value(audit: AuditLog) -> tuple[float | None, str | None]:
    for rec in reversed(audit.records()):
        snap = rec.get("snapshot") or {}
        if rec.get("kind") == "check" and snap.get("total_value") is not None:
            return float(snap["total_value"]), rec.get("ts")
    return None, None


def pending_confirms(audit: AuditLog) -> tuple[PendingConfirm, ...]:
    """Approved CONFIRM-mode orders with no fill reported against them."""
    approved: dict[str, dict] = {}
    filled: set[str] = set()
    for rec in audit.records():
        if rec.get("kind") == "check" and rec.get("approved") and rec.get("requires_confirm"):
            approved[rec["approval_id"]] = rec
        elif rec.get("kind") == "fill" and not rec.get("orphan"):
            filled.add(rec.get("approval_id"))
    out = []
    for approval_id, rec in approved.items():
        if approval_id in filled:
            continue
        intent = rec.get("intent", {})
        out.append(PendingConfirm(
            approval_id=approval_id, ts=rec.get("ts", ""),
            sleeve=intent.get("sleeve", "?"), ticker=intent.get("ticker", "?"),
            side=intent.get("side", "?"), qty=intent.get("qty", 0.0),
            limit_price=intent.get("limit_price", 0.0),
            thesis=intent.get("thesis", ""),
        ))
    return tuple(sorted(out, key=lambda p: p.ts))


def auth_status(policy: Policy, now: datetime | None = None) -> AuthStatus:
    from croupier.data.schwab import TokenState
    tokens = TokenState.load(policy.schwab_token_path)
    if tokens is None:
        return AuthStatus(False, None, False,
                          "no Schwab tokens — Stooq EOD floor in use (DEGRADED)")
    days = tokens.days_until_reauth(now or datetime.now(UTC))
    if days <= 0:
        return AuthStatus(True, days, True,
                          "Schwab refresh token EXPIRED — browser re-auth required")
    return AuthStatus(True, days, days < 1.0,
                      f"Schwab re-auth in {days:.2f} days")


def build(policy: Policy, ledger: Ledger, audit: AuditLog,
          data_health: DataHealth, as_of: date | None = None,
          now: datetime | None = None) -> JournalReport:
    day = as_of or datetime.now(UTC).date()
    account_value, snapshot_ts = latest_account_value(audit)
    positions = ledger.positions()

    lines = []
    for name, sc in sorted(policy.config.sleeves.items()):
        held = tuple(p for p in positions if p.sleeve == name)
        lines.append(SleeveLine(
            sleeve=name,
            mode=str(sc.mode),
            cost_basis=sum(p.cost_basis for p in held),
            ceiling=_ceiling(account_value, sc),
            positions=held,
            halt=policy.state.halts.get(name),
        ))

    return JournalReport(
        as_of=day,
        data_health=data_health,
        auth=auth_status(policy, now),
        account_value=account_value,
        account_snapshot_ts=snapshot_ts,
        sleeves=tuple(lines),
        pending=pending_confirms(audit),
        halts=tuple(sorted(policy.state.halts.values(), key=lambda h: h.sleeve)),
        freezes=policy.catalysts.freezes_on(day),
        freeze_trading_days=policy.catalysts.freeze_trading_days,
    )


def _ceiling(account_value: float | None, sc: SleeveConfig) -> float | None:
    if account_value is None:
        return None
    return account_value * sc.budget_pct / 100.0


def write(report: JournalReport, text: str, directory: Path = JOURNAL_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.as_of.isoformat()}.md"
    path.write_text(text)
    return path
