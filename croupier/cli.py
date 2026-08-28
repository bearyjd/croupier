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
import sys
from datetime import UTC, datetime
from pathlib import Path

from croupier.audit import AuditLog
from croupier.data.base import DataHealth
from croupier.data.factory import build_router
from croupier.journal import JOURNAL_DIR
from croupier.journal import build as build_journal
from croupier.journal import write as write_journal
from croupier.journal_render import render as render_journal
from croupier.ledger import LEDGER_PATH, Ledger, LedgerConflict, new_fill
from croupier.marking import mark_to_market
from croupier.models import AccountSnapshot, OrderIntent
from croupier.policy import load as load_full_policy

AUDIT_PATH = Path("data/audit.jsonl")


def _emit(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


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
    verdict = AuditLog(AUDIT_PATH).check_and_log(
        intent, policy.config, snap,
        payload.get("auto_spent_today", 0.0),
        DataHealth(payload.get("data_health", "fresh")),
        calendar=policy.catalysts,
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
    warnings = []
    if qty > intent.get("qty", qty) + 1e-9:
        warnings.append(
            f"overfill: {qty} filled against approved qty {intent.get('qty')}")

    fill = new_fill(approval_id, sleeve, ticker, side, qty, price)
    with Ledger(LEDGER_PATH) as ledger:
        try:
            inserted = ledger.record_fill(fill)
        except LedgerConflict as exc:
            log.log_fill(approval_id, ticker, side, qty, price, sleeve=sleeve)
            _emit({"logged": True, "ledger": False, "error": str(exc)})
            return 1
    log.log_fill(approval_id, ticker, side, qty, price, sleeve=sleeve)
    _emit({"logged": True, "ledger": True, "sleeve": sleeve,
           "new": inserted, "warnings": warnings})
    return 1 if warnings else 0


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
            data_health=build_router(policy.schwab_token_path).health(),
        )
    text = render_journal(report)
    path = write_journal(report, text, JOURNAL_DIR)
    print(text)
    print(f"\n<!-- written to {path} -->")
    # Non-zero when the operator has something to act on, so a cron wrapper
    # can push on the exit code alone.
    return 1 if (report.pending or report.halts or report.auth.nag) else 0


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
    sub.add_parser("mark", help="daily mark-to-market + drawdown halt")
    sub.add_parser("journal", help="render today's operator journal")
    sub.add_parser("auth-status", help="Schwab data-feed token health")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.cmd == "mark":
        return cmd_mark()
    if args.cmd == "journal":
        return cmd_journal()
    if args.cmd == "auth-status":
        return cmd_auth_status()

    payload = json.load(sys.stdin)
    if args.cmd == "check":
        return cmd_check(payload)
    return cmd_fill(payload)


if __name__ == "__main__":
    raise SystemExit(main())
