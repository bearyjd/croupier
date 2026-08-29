# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Render a JournalReport.

One renderer, two destinations: the output is Markdown for
data/journal/YYYY-MM-DD.md and stays readable unrendered on stdout, so the
operator never reads a different report from the one on disk.
"""
from __future__ import annotations

from croupier.catalysts import CatalystEvent
from croupier.data.base import DataHealth
from croupier.journal import JournalReport, PendingConfirm, SleeveLine

_HEALTH_BANNER = {
    DataHealth.FRESH: None,
    DataHealth.DEGRADED: (
        "**DEGRADED DATA** — Schwab feed unavailable, EOD fallback in "
        "use. No new AUTO entries; exits still run and are flagged."),
    DataHealth.DEAD: (
        "**DEAD DATA** — no market data source available. Place nothing "
        "without explicit human instruction."),
}


def render(report: JournalReport) -> str:
    out: list[str] = [f"# Croupier journal — {report.as_of.isoformat()}", ""]

    banner = _HEALTH_BANNER[report.data_health]
    if banner:
        out += [f"> {banner}", ""]
    if report.auth.nag:
        out += [f"> **RE-AUTH NAG** — {report.auth.detail}", ""]

    out += ["## Status", ""]
    out += [f"- data feed: `{report.data_health}`"]
    out += [f"- schwab auth: {report.auth.detail}"]
    if report.account_value is None:
        out += ["- account value: _unknown_ — no check has supplied a snapshot yet; "
                "sleeve ceilings cannot be sized"]
    else:
        out += [f"- account value: {_usd(report.account_value)} "
                f"(agent snapshot at {report.account_snapshot_ts})"]
    out += [""]

    out += ["## Sleeves", ""]
    out += [_sleeve_block(s) for s in report.sleeves] if report.sleeves else ["_none configured_"]
    out += [""]

    out += [f"## Pending confirms ({len(report.pending)})", ""]
    if report.pending:
        out += ["Approved, awaiting an explicit Y. Nothing here has been placed.", ""]
        out += [_pending_block(p) for p in report.pending]
    else:
        out += ["_none_"]
    out += [""]

    out += [f"## Halts ({len(report.halts)})", ""]
    if report.halts:
        out += ["Clearing a halt is a human act: review, then delete the entry "
                "from `data/sleeve_state.yaml`.", ""]
        out += [f"- **{h.sleeve}** — {h.reason} (since {h.since})" for h in report.halts]
    else:
        out += ["_none_"]
    out += [""]

    out += [f"## Catalyst freezes ({len(report.freezes)})", ""]
    if report.freezes:
        out += [f"Adds in these tickers require a human `Y` regardless of sleeve "
                f"mode, from T-{report.freeze_trading_days} trading days through "
                f"the end of the window. Exits are unaffected.", ""]
        out += [_freeze_block(e, report.freeze_trading_days) for e in report.freezes]
    else:
        out += ["_none_"]
    out += [""]

    return "\n".join(out)


def _sleeve_block(s: SleeveLine) -> str:
    if s.ceiling is None:
        headline = f"- **{s.sleeve}** — `{s.mode}` — basis {_usd(s.cost_basis)} (ceiling unknown)"
    elif s.ceiling == 0:
        headline = (f"- **{s.sleeve}** — `{s.mode}` — budget 0, no capital allocated "
                    f"(basis {_usd(s.cost_basis)})")
    else:
        used = s.utilisation_pct
        headline = (f"- **{s.sleeve}** — `{s.mode}` — {_usd(s.cost_basis)} of "
                    f"{_usd(s.ceiling)} ceiling"
                    + (f" ({used:.1f}%)" if used is not None else ""))
    lines = [headline]
    if s.halt is not None:
        lines.append(f"    - HALTED: {s.halt.reason}")
    for p in s.positions:
        lines.append(f"    - {p.ticker}: {p.qty:,.0f} sh @ {_usd(p.avg_cost)} avg, "
                     f"basis {_usd(p.cost_basis)}")
    if not s.positions:
        lines.append("    - no open positions")
    return "\n".join(lines)


def _pending_block(p: PendingConfirm) -> str:
    return "\n".join([
        f"- `{p.approval_id}` **{p.sleeve}** {p.side.upper()} {p.qty:,.0f} "
        f"{p.ticker} @ {_usd(p.limit_price)}",
        f"    - {p.thesis}" if p.thesis else "    - _no thesis recorded_",
        f"    - approved {p.ts}",
    ])


def _freeze_block(e: CatalystEvent, trading_days: int) -> str:
    lines = [
        f"- **{e.ticker}** {e.event_type} — window "
        f"{e.window_start.isoformat()}..{e.window_end.isoformat()}, "
        f"frozen since {e.freeze_start(trading_days).isoformat()}",
    ]
    if not e.verified:
        lines.append("    - UNVERIFIED window — confirm against the public source "
                     "before relying on it")
    if e.note:
        lines.append(f"    - {e.note.strip()}")
    lines.append(f"    - source: {e.source_url}")
    return "\n".join(lines)


def _usd(value: float) -> str:
    return f"${value:,.2f}" if abs(value) < 100 else f"${value:,.0f}"
