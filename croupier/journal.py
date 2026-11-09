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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from croupier.audit import AuditLog, valid_approval_id
from croupier.catalysts import CatalystEvent
from croupier.data.base import DataHealth
from croupier.gates.pipeline import SleeveConfig
from croupier.ledger import Ledger, Position
from croupier.models import parse_ts
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
class FillIssue:
    """A record the operator must reconcile: a flagged, refused or orphan
    fill, or a record whose approval_id is malformed.

    ``match_id`` is the valid string id ``ack-fill <id>`` matches, or None
    when the record has no usable id; ``handle`` is always usable with
    ``ack-fill --handle``.
    """
    approval_id: str
    match_id: str | None
    handle: str
    ts: str
    ticker: str
    side: str
    qty: float
    problems: tuple[str, ...]


_PROBLEMS = (("orphan", "ORPHAN (no matching approval; not in the ledger)"),
             ("bad_approval_id", "approval_id is not a non-blank string"),
             ("refused", "REFUSED: not recorded in the ledger"),
             ("implausible", "implausible quantity (over 10x approved, or notional "
                             "not finite): treated as corrupt data"),
             ("fill_after_decline", "fill after decline (recorded in the ledger)"),
             ("price_better_than_limit", "filled far better than its limit, over 10x "
                                         "(recorded at the reported price)"),
             ("price_above_limit", "buy filled above its limit price (recorded)"),
             ("price_below_limit", "sell filled below its limit price (recorded)"),
             ("overfill", "overfill (recorded in the ledger at the reported qty)"))
_MALFORMED = "malformed record: approval_id is not a non-blank string"


def needs_action(audit: AuditLog) -> tuple[FillIssue, ...]:
    """Flagged, refused, orphan and malformed records with no later ack.

    A ``reconcile`` record resolves its ``fill_id`` the same way.
    An ``ack_fill`` naming a ``handle`` resolves exactly that record; one
    naming only a valid ``approval_id`` resolves every open record carrying
    that id. A record without a usable id can only be resolved by handle.
    """
    open_issues: list[FillIssue] = []
    for idx, rec in enumerate(audit.records()):
        kind = rec.get("kind")
        match_id = valid_approval_id(rec.get("approval_id"))
        if kind in ("ack_fill", "reconcile"):
            handle = rec.get("handle") or rec.get("fill_id")
            if handle:
                open_issues = [i for i in open_issues if i.handle != handle]
            elif match_id:
                open_issues = [i for i in open_issues if i.match_id != match_id]
            continue
        problems = ()
        if kind == "fill":
            problems = tuple(text for key, text in _PROBLEMS if rec.get(key))
            if match_id is None and not rec.get("bad_approval_id"):
                problems += (_MALFORMED,)
        elif match_id is None and (
                kind == "decline"
                or (kind == "check" and rec.get("approved"))):
            problems = (_MALFORMED,)
        if problems:
            open_issues.append(FillIssue(
                approval_id=str(rec.get("approval_id")),
                match_id=None if rec.get("bad_approval_id") else match_id,
                handle=str(rec.get("fill_id") or f"rec{idx}-{rec.get('ts', '')}"),
                ts=rec.get("ts", ""), ticker=str(rec.get("ticker")),
                side=str(rec.get("side")), qty=rec.get("qty"), problems=problems))
    return tuple(open_issues)


@dataclass(frozen=True)
class ForcedReconcile:
    """A reconcile the operator forced past the plausibility bands."""
    fill_id: str
    ts: str
    ticker: str
    side: str
    qty: float
    price: float
    reported_qty: object
    reported_price: object
    reason: str


FORCED_RECONCILE_WINDOW = timedelta(hours=24)


def forced_reconciles(audit: AuditLog, now: datetime) -> tuple[ForcedReconcile, ...]:
    """Reconciles with force:true in the last 24h, so --force use is visible
    beyond the audit log."""
    out = []
    for rec in audit.records():
        if rec.get("kind") != "reconcile" or not rec.get("force"):
            continue
        ts = parse_ts(rec.get("ts"))   # naive -> UTC; malformed -> skipped
        if ts is None:
            continue
        if now - ts <= FORCED_RECONCILE_WINDOW:
            out.append(ForcedReconcile(
                str(rec.get("fill_id")), rec["ts"], str(rec.get("ticker")),
                str(rec.get("side")), rec.get("qty"), rec.get("price"),
                rec.get("reported_qty"), rec.get("reported_price"),
                str(rec.get("reason"))))
    return tuple(out)


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
    needs_action: tuple[FillIssue, ...] = ()
    forced: tuple[ForcedReconcile, ...] = ()
    freezes: tuple[CatalystEvent, ...] = ()   # active catalyst freezes today
    freeze_trading_days: int = 0


def latest_account_value(audit: AuditLog) -> tuple[float | None, str | None]:
    for rec in reversed(audit.records()):
        snap = rec.get("snapshot") or {}
        if rec.get("kind") == "check" and snap.get("total_value") is not None:
            return float(snap["total_value"]), rec.get("ts")
    return None, None


def _fill_matches(approval: dict | None, fill: dict) -> bool:
    if approval is None:
        return False
    intent = approval.get("intent", {})
    try:
        within = float(fill["qty"]) <= float(intent["qty"]) + 1e-9
    except (KeyError, TypeError, ValueError):
        return False
    return (within and str(fill.get("ticker", "")).upper() == str(intent.get("ticker", "")).upper()
            and fill.get("side") == intent.get("side"))


def pending_confirms(audit: AuditLog) -> tuple[PendingConfirm, ...]:
    """Approved CONFIRM-mode orders with neither a fill nor a decline
    reported against them.

    Without the decline path an approval the operator simply never acted on
    stayed here forever — the log is append-only — and the journal exited
    non-zero every day over an order nobody intended to place.
    """
    approved: dict[str, dict] = {}
    fills: list[dict] = []
    declined: set[str] = set()
    for rec in audit.records():
        aid = valid_approval_id(rec.get("approval_id"))
        if rec.get("kind") == "check" and rec.get("approved") and rec.get("requires_confirm"):
            if aid:
                approved[aid] = rec
        elif (rec.get("kind") == "fill"
              and not any(rec.get(k) for k, _ in _PROBLEMS)):
            fills.append(rec)
        elif rec.get("kind") in ("decline", "ack_fill", "reconcile"):
            # an acknowledgement is the operator saying they reconciled it
            if aid:
                declined.add(aid)
    # A fill clears a pending confirm only if it actually matches the approval:
    # same ticker and side, within the approved quantity.
    filled = {valid_approval_id(f.get("approval_id")) for f in fills
              if _fill_matches(approved.get(valid_approval_id(f.get("approval_id"))), f)}
    out = []
    for approval_id, rec in approved.items():
        if approval_id in filled or approval_id in declined:
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
                          "no Schwab tokens — EOD floor in use (DEGRADED)")
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
        needs_action=needs_action(audit),
        forced=forced_reconciles(audit, now or datetime.now(UTC)),
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
