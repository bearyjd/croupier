# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Daily mark-to-market and the automatic drawdown halt (PRP-001 P2).

Marks every open position through the DataRouter, advances each sleeve's
time-weighted equity curve, and halts any sleeve whose drawdown from its
high-water mark exceeds the configured ceiling (25%, from the sleeve's
guardrails).

A halt is written to the sleeve state file, which the policy loader merges
over policy.yaml. Clearing it is a human act: the code never un-halts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from croupier.audit import AuditLog
from croupier.data.base import DataHealth, Quote
from croupier.data.router import DataRouter
from croupier.drawdown import EquityPoint, advance
from croupier.ledger import Ledger, Position
from croupier.models import utcnow
from croupier.state import Halt, SleeveState


@dataclass(frozen=True)
class SleeveMark:
    sleeve: str
    market_value: float
    cost_basis: float
    net_flow: float
    point: EquityPoint
    halted_now: bool
    unpriced: tuple[str, ...]      # tickers the router could not quote


@dataclass(frozen=True)
class MarkResult:
    as_of: date
    data_health: DataHealth
    marks: tuple[SleeveMark, ...]
    state: SleeveState


async def mark_to_market(
    ledger: Ledger,
    router: DataRouter,
    state: SleeveState,
    *,
    max_drawdown_pct: float,
    as_of: date | None = None,
    audit: AuditLog | None = None,
) -> MarkResult:
    """Mark all sleeves, advance their equity curves, halt on breach."""
    day = as_of or utcnow().date()
    positions = ledger.positions()
    quotes = await _quote_all(router, {p.ticker for p in positions})

    marks: list[SleeveMark] = []
    next_state = state
    for sleeve in sorted({p.sleeve for p in positions} | set(ledger.sleeves())):
        held = [p for p in positions if p.sleeve == sleeve]
        market_value = sum(
            p.qty * quotes[p.ticker].price for p in held if quotes.get(p.ticker)
        )
        unpriced = tuple(sorted(p.ticker for p in held if not quotes.get(p.ticker)))
        # An unpriced holding is carried at cost rather than dropped to zero,
        # which would fake a drawdown and halt the sleeve on a data outage.
        market_value += sum(p.cost_basis for p in held if p.ticker in unpriced)

        point = advance(
            ledger.last_equity_point(sleeve),
            sleeve=sleeve,
            as_of=day,
            market_value=market_value,
            net_flow=ledger.net_flow_on(sleeve, day),
        )
        ledger.record_equity_point(point)

        halted_now = False
        if point.breaches(max_drawdown_pct) and not next_state.is_halted(sleeve):
            halt = Halt(
                sleeve=sleeve,
                reason=(
                    f"drawdown {point.drawdown_pct:.1f}% exceeds "
                    f"{max_drawdown_pct:.0f}% from high-water mark "
                    "(sleeve guardrails); human strategy review required"
                ),
                since=utcnow(),
                drawdown_pct=round(point.drawdown_pct, 2),
            )
            next_state = next_state.with_halt(halt)
            halted_now = True
            if audit is not None:
                audit.log_event("sleeve_halted", {
                    "sleeve": sleeve, "reason": halt.reason,
                    "drawdown_pct": halt.drawdown_pct,
                    "twr_index": point.twr_index, "hwm_index": point.hwm_index,
                })

        marks.append(SleeveMark(
            sleeve=sleeve,
            market_value=market_value,
            cost_basis=sum(p.cost_basis for p in held),
            net_flow=point.net_flow,
            point=point,
            halted_now=halted_now,
            unpriced=unpriced,
        ))

    if next_state is not state:
        next_state.save()

    return MarkResult(
        as_of=day, data_health=router.health(),
        marks=tuple(marks), state=next_state,
    )


async def _quote_all(router: DataRouter, tickers: set[str]) -> dict[str, Quote | None]:
    """Quote every ticker; a router failure yields None, never an exception."""
    out: dict[str, Quote | None] = {}
    for ticker in sorted(tickers):
        try:
            out[ticker] = await router.quote(ticker)
        except Exception:                                  # noqa: BLE001
            # Market data must never raise into a trading path (PRP-002).
            out[ticker] = None
    return out


def positions_by_sleeve(positions: list[Position]) -> dict[str, list[Position]]:
    grouped: dict[str, list[Position]] = {}
    for p in positions:
        grouped = {**grouped, p.sleeve: [*grouped.get(p.sleeve, []), p]}
    return grouped
