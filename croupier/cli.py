# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""`croupier check` / `fill` / `mark` / `auth-status` — the agent-facing surface.

The trading agent (connected to Robinhood MCP) pipes each proposed order
through `check` as JSON on stdin and may place ONLY orders that return
approved=true, quoting the approval_id. Fills are reported back via `fill`,
which joins them to their approval and persists them to the ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from croupier.audit import AuditLog, valid_approval_id
from croupier.data import observed_health
from croupier.data.base import DataHealth
from croupier.data.factory import build_router
from croupier.journal import JOURNAL_DIR
from croupier.journal import build as build_journal
from croupier.journal import write as write_journal
from croupier.journal_render import render as render_journal
from croupier.ledger import LEDGER_PATH, Ledger, LedgerConflict, new_fill
from croupier.marking import mark_to_market
from croupier.models import AccountSnapshot, OrderIntent, require_finite
from croupier.policy import load as load_full_policy

AUDIT_PATH = Path("data/audit.jsonl")

# A fill reporting more than this multiple of the approved quantity is not an
# overfill the operator can reconcile, it is corrupt data (a unit slip, a
# hostile or garbled report). Up to the factor, the real quantity is recorded
# and flagged; beyond it, or with a non-finite notional, the fill is refused
# and flagged, because a recorded 1e308 would poison cost basis and marking.
MAX_OVERFILL_FACTOR = 10


def _emit(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _reject_nonfinite(literal: str) -> float:
    """json.load's parse_constant hook: bare NaN/Infinity/-Infinity are not JSON."""
    raise ValueError(f"non-finite number {literal} is not accepted")


def cmd_check(payload: dict) -> int:
    intent = OrderIntent(
        sleeve=payload["sleeve"], ticker=payload["ticker"], side=payload["side"],
        qty=payload["qty"], limit_price=payload["limit_price"],
        signal_refs=tuple(payload.get("signal_refs", [])),
        thesis=payload.get("thesis", ""), is_etf=payload.get("is_etf", False),
        venue=payload.get("venue", "robinhood"),
        adv_shares=payload.get("adv_shares"),
    )
    snap = AccountSnapshot(
        total_value=payload["account"]["total_value"],
        cash=payload["account"]["cash"],
        sleeve_cost_basis=payload["account"].get("sleeve_cost_basis", {}),
        position_cost_basis=payload["account"].get("position_cost_basis", {}),
    )
    policy = load_full_policy()
    # Held quantity comes from Croupier's own ledger, never from the agent's
    # snapshot: the whole point of the holdings gate is to catch an agent
    # that has miscounted its position, so its own count cannot be the
    # authority. Read for sells only — a buy does not need it, and opening
    # the ledger on every check would create data/ledger.db before the first
    # fill ever lands.
    held_qty = None
    if intent.side == "sell":
        with Ledger(LEDGER_PATH) as ledger:
            held_qty = next((p.qty for p in ledger.positions()
                             if p.sleeve == intent.sleeve
                             and p.ticker == intent.ticker.upper()), 0.0)
    verdict = AuditLog(AUDIT_PATH).check_and_log(
        intent, policy.config, snap,
        payload.get("auto_spent_today", 0.0),
        DataHealth(payload.get("data_health", "fresh")),
        calendar=policy.catalysts,
        held_qty=held_qty,
    )
    _emit({
        "approved": verdict.approved,
        "approval_id": verdict.approval_id,
        "requires_confirm": verdict.requires_confirm,
        "decisions": [{"gate": d.gate, "passed": d.passed, "reason": d.reason}
                      for d in verdict.decisions],
    })
    return 0 if verdict.approved else 1


def cmd_fill(payload: dict) -> int:
    """Join a fill to its approval, then persist it to the ledger.

    A fill whose approval_id is unknown is an ungated trade: it is written to
    the audit log (never dropped — that is what the trail is for) but refused
    entry to the ledger, and the command exits non-zero.
    """
    log = AuditLog(AUDIT_PATH)
    approval_id = payload["approval_id"]
    ticker, side = payload["ticker"], payload["side"]
    qty, price = payload["qty"], payload["price"]
    if valid_approval_id(approval_id) is None:
        # Nothing can join to this id, and it must not be matched or hashed as
        # one. It still goes on the record, as text, with a handle to ack it by.
        log.log_fill(repr(approval_id), ticker, side, qty, price, orphan=True,
                     flags=("bad_approval_id",))
        _emit({"logged": True, "ledger": False, "error": (
            "approval_id must be a non-blank string; recorded as ORPHAN")})
        return 2
    try:
        if not isinstance(ticker, str) or not isinstance(side, str):
            raise ValueError("ticker and side must be text")
        require_finite(qty=qty, price=price)
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
    except ValueError as exc:
        # Not a fill any approval could cover; never reaches the ledger, but
        # still goes on the record.
        log.log_fill(approval_id, ticker, side, qty, price, orphan=True)
        _emit({"logged": True, "ledger": False, "error": f"{exc}; recorded as ORPHAN"})
        return 2

    approval = log.find_approval(approval_id)
    if approval is None:
        log.log_fill(approval_id, ticker, side, qty, price, orphan=True)
        _emit({"logged": True, "ledger": False, "error": (
            f"no approved check found for approval_id {approval_id!r}: fill "
            "recorded in the audit log as an ORPHAN and refused by the ledger")})
        return 1

    intent = approval.get("intent", {})
    if intent.get("ticker", "").upper() != ticker.upper() or intent.get("side") != side:
        log.log_fill(approval_id, ticker, side, qty, price,
                     sleeve=intent.get("sleeve"), orphan=True)
        _emit({"logged": True, "ledger": False, "error": (
            f"fill {side} {ticker} does not match approval {approval_id} "
            f"({intent.get('side')} {intent.get('ticker')}); recorded as ORPHAN")})
        return 1

    sleeve = intent["sleeve"]
    approved_qty = intent.get("qty")
    if not math.isfinite(qty * price) or (
            isinstance(approved_qty, (int, float))
            and qty > MAX_OVERFILL_FACTOR * approved_qty):
        log.log_fill(approval_id, ticker, side, qty, price, sleeve=sleeve,
                     flags=("refused", "implausible"))
        _emit({"logged": True, "ledger": False, "error": (
            f"implausible fill: {qty:g} @ {price:g} against approved qty "
            f"{approved_qty}; more than {MAX_OVERFILL_FACTOR}x the approval or a "
            "non-finite notional is treated as corrupt data and NOT recorded in "
            "the ledger. Stop and report to the operator")})
        return 1
    fill = new_fill(approval_id, sleeve, ticker, side, qty, price)
    with Ledger(LEDGER_PATH) as ledger:
        try:
            inserted = ledger.record_fill(fill)
        except LedgerConflict as exc:
            log.log_fill(approval_id, ticker, side, qty, price, sleeve=sleeve,
                         flags=("refused",))
            _emit({"logged": True, "ledger": False, "error": str(exc)})
            return 1
    # The ledger must hold what really happened, so a discrepant fill is
    # recorded at its real quantity and flagged, never refused: refusing would
    # understate holdings (cost-basis caps, marking, the drawdown halt).
    flags, warnings = _fill_flags(log, approval_id, intent, qty)
    if inserted:   # an identical replay is a harmless repeat: no second record
        log.log_fill(approval_id, ticker, side, qty, price, sleeve=sleeve, flags=flags)
    _emit({"logged": True, "ledger": True, "sleeve": sleeve,
           "new": inserted, "replay": not inserted,
           "warnings": warnings})
    return 1 if warnings else 0


def _fill_flags(log: AuditLog, approval_id: str, intent: dict,
                qty: float) -> tuple[tuple[str, ...], list[str]]:
    flags, warnings = [], []
    if any(r.get("kind") == "decline" and r.get("approval_id") == approval_id
           for r in log.records()):
        flags.append("fill_after_decline")
        warnings.append(f"fill after decline: approval {approval_id} was declined "
                        "but the fill is real and is recorded")
    approved_qty = intent.get("qty", qty)
    if qty > approved_qty + 1e-9:
        flags.append("overfill")
        warnings.append(f"overfill: reported {qty:g} > approved {approved_qty:g}")
    return tuple(flags), warnings


def cmd_decline(payload: dict) -> int:
    """Retire an approved CONFIRM-mode order that will not be placed.

    The operator said N (or never said Y). The approval stays in the audit
    log exactly as written; this appends the operator's answer after it so
    the journal stops listing the order as pending. Declining an approval_id
    the log has never seen is recorded anyway, flagged, and exits non-zero —
    an unknown id is more likely a typo than a real order, and a typo should
    not silently look like a decision.
    """
    log = AuditLog(AUDIT_PATH)
    approval_id = payload["approval_id"]
    if not isinstance(approval_id, str):
        approval_id = repr(approval_id)   # recorded as an unknown id, never hashed raw
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        _emit({"logged": False, "error": "a decline needs a reason — it is part of the trail"})
        return 1

    approval = log.find_approval(approval_id)
    if approval is None or not approval.get("approved"):
        log.log_decline(approval_id, reason, known=False)
        _emit({"logged": True, "known": False, "error": (
            f"no approved check found for approval_id {approval_id!r}: "
            "decline recorded and flagged; nothing was pending under that id")})
        return 1

    if LEDGER_PATH.exists():
        with Ledger(LEDGER_PATH) as ledger:
            filled = any(f.approval_id == approval_id for f in ledger.fills())
        if filled:
            _emit({"logged": False, "error": (
                f"approval_id {approval_id!r} already has a fill in the ledger; "
                "a decline cannot undo it — reconcile with the operator")})
            return 1

    intent = approval.get("intent", {})
    log.log_decline(approval_id, reason)
    _emit({"logged": True, "known": True, "approval_id": approval_id,
           "sleeve": intent.get("sleeve"), "ticker": intent.get("ticker"),
           "side": intent.get("side"), "qty": intent.get("qty"),
           "reason": reason})
    return 0


def cmd_ack_fill(approval_id: str | None, handle: str | None, reason: str) -> int:
    """Acknowledge a flagged, refused or orphan fill so the journal stops
    listing it. This only silences the journal: it corrects nothing in the
    ledger, and the fill's own record stays in the trail unchanged."""
    from croupier.journal import needs_action
    reason = reason.strip()
    if not reason:
        _emit({"logged": False, "error": "an acknowledgement needs a reason"})
        return 1
    if (approval_id is None) == (handle is None):
        _emit({"logged": False, "error": "give exactly one of approval_id or --handle"})
        return 1
    log = AuditLog(AUDIT_PATH)
    issues = needs_action(log)
    if handle is not None:
        hit = [i for i in issues if i.handle == handle]
    else:
        approval_id = valid_approval_id(approval_id)
        hit = [i for i in issues if approval_id and i.match_id == approval_id]
    if not hit:
        _emit({"logged": False, "error": (
            f"no unresolved flagged fill for {handle or approval_id!r}")})
        return 1
    log.log_ack_fill(hit[0].match_id if handle is not None else approval_id,
                     reason, handle=handle)
    _emit({"logged": True, "reason": reason, "handle": handle,
           "approval_id": approval_id})
    return 0


def cmd_mark() -> int:
    """Mark every open position, advance equity curves, halt on drawdown."""
    policy = load_full_policy()
    with Ledger(LEDGER_PATH) as ledger:
        result = asyncio.run(mark_to_market(
            ledger, build_router(policy.schwab_token_path), policy.state,
            max_drawdown_pct=policy.max_sleeve_drawdown_pct,
            audit=AuditLog(AUDIT_PATH),
        ))
    new_halts = [m.sleeve for m in result.marks if m.halted_now]
    _emit({
        "as_of": result.as_of.isoformat(),
        "data_health": str(result.data_health),
        "max_sleeve_drawdown_pct": policy.max_sleeve_drawdown_pct,
        "sleeves": [{
            "sleeve": m.sleeve,
            "market_value": round(m.market_value, 2),
            "cost_basis": round(m.cost_basis, 2),
            "net_flow_today": round(m.net_flow, 2),
            "twr_index": round(m.point.twr_index, 6),
            "hwm_index": round(m.point.hwm_index, 6),
            "drawdown_pct": round(m.point.drawdown_pct, 2),
            "halted_now": m.halted_now,
            "unpriced_carried_at_cost": list(m.unpriced),
        } for m in result.marks],
        "new_halts": new_halts,
        "halted_sleeves": sorted(result.state.halts),
    })
    return 1 if new_halts else 0


def cmd_journal() -> int:
    """Render the day: auth, data health, sleeves vs ceilings, confirms, halts."""
    policy = load_full_policy()
    audit = AuditLog(AUDIT_PATH)
    with Ledger(LEDGER_PATH) as ledger:
        report = build_journal(
            policy, ledger, audit,
            # Not build_router(...).health() — a router built here and
            # never queried is optimistic by construction, so that call
            # always reported DEGRADED-or-better whenever a key was merely
            # configured. `mark` is the command that actually queries the
            # floor; this reads what it last saw.
            data_health=observed_health.load(),
        )
    text = render_journal(report)
    path = write_journal(report, text, JOURNAL_DIR)
    print(text)
    print(f"\n<!-- written to {path} -->")
    # Non-zero when the operator has something to act on, so a cron wrapper
    # can push on the exit code alone.
    return 1 if (report.pending or report.needs_action or report.halts
                      or report.auth.nag) else 0


def cmd_auth_status() -> int:
    from croupier.data.schwab import TokenState
    policy = load_full_policy()
    t = TokenState.load(policy.schwab_token_path)
    if t is None:
        _emit({"schwab": "no tokens", "action": "run initial browser auth"})
        return 1
    days = t.days_until_reauth(datetime.now(UTC))
    _emit({"schwab_reauth_in_days": round(days, 2),
           "expires_at": t.refresh_expires_at.isoformat(),
           "nag": days < 1.0})
    return 0 if days > 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="croupier", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="gate an order intent (JSON on stdin)")
    sub.add_parser("fill", help="report a fill (JSON on stdin)")
    sub.add_parser("decline",
                   help="retire an approved order that will not be placed (JSON on stdin)")
    ack = sub.add_parser("ack-fill",
                         help="acknowledge a flagged/orphan fill after reconciling it")
    ack.add_argument("approval_id", nargs="?", default=None)
    ack.add_argument("--handle", default=None,
                     help="the fill_id shown in the journal, for a record with no usable id")
    ack.add_argument("--reason", default="")
    sub.add_parser("mark", help="daily mark-to-market + drawdown halt")
    sub.add_parser("journal", help="render today's operator journal")
    sub.add_parser("auth-status", help="Schwab data-feed token health")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.cmd == "ack-fill":
        return cmd_ack_fill(args.approval_id, args.handle, args.reason)
    if args.cmd == "mark":
        return cmd_mark()
    if args.cmd == "journal":
        return cmd_journal()
    if args.cmd == "auth-status":
        return cmd_auth_status()

    try:
        payload = json.load(sys.stdin, parse_constant=_reject_nonfinite)
    except (ValueError, RecursionError) as exc:
        _emit({"error": f"invalid JSON on stdin: {exc}"})
        return 2
    handler = {"check": cmd_check, "decline": cmd_decline}.get(args.cmd, cmd_fill)
    try:
        return handler(payload)
    except (ValueError, TypeError, OverflowError, RecursionError,
            sqlite3.IntegrityError) as exc:
        _emit({"error": f"invalid input: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
