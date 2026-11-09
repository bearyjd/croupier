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

import math
from dataclasses import dataclass
from datetime import date

from croupier.audit import AuditLog
from croupier.data import observed_health
from croupier.data.base import DataHealth, Quote
from croupier.data.router import DataRouter
from croupier.drawdown import EquityPoint, advance
from croupier.ledger import Ledger, Position
from croupier.models import utcnow
from croupier.state import Halt, SleeveState

# Quoted by `mark` when the book is empty, so data health is observed rather
# than defaulted. One credit a day on the EOD floor; any liquid ticker works.
CANARY_TICKER = "SPY"


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
    canary: str | None = CANARY_TICKER,
) -> MarkResult:
    """Mark all sleeves, advance their equity curves, halt on breach.

    ``canary`` is quoted when the book is empty, so the floor is genuinely
    observed even before the first position exists. Without it an empty book
    never wrote ``observed_health`` and ``journal`` read DEAD forever — which
    made the *first* entry impossible without a human overriding data
    health, the opposite of what the observed-health rule is for. Pass
    ``None`` to disable (tests that assert the no-probe path).
    """
    day = as_of or utcnow().date()
    positions = ledger.positions()
    tickers = {p.ticker for p in positions}
    probe = tickers or ({canary} if canary else set())
    quotes = await _quote_all(router, probe)

    marks: list[SleeveMark] = []
    next_state = state
    for sleeve in sorted({p.sleeve for p in positions} | set(ledger.sleeves())):
        held = [p for p in positions if p.sleeve == sleeve]
        values = {p.ticker: _priced_value(p, quotes.get(p.ticker)) for p in held}
        market_value = sum(v for v in values.values() if v is not None)
        unpriced = tuple(sorted(t for t, v in values.items() if v is None))
        # An unpriced holding is carried at cost rather than dropped to zero,
        # which would fake a drawdown and halt the sleeve on a data outage.
        # A quote that is non-finite or overflows the value counts as unpriced.
        market_value += sum(p.cost_basis for p in held if p.ticker in unpriced)
        net_flow = ledger.net_flow_on(sleeve, day)

        pending_event: tuple[str, dict] | None = None
        halted_now = False
        if not (math.isfinite(market_value) and math.isfinite(net_flow)):
            # A book that cannot be valued (legacy inf/NaN row) is not
            # evidence of safety: halt the sleeve, and keep the poison out
            # of the equity curve.
            point = _prev_or_flat(ledger.last_equity_point_before(sleeve, day), sleeve, day)
            if not next_state.is_halted(sleeve):
                halt = Halt(
                    sleeve=sleeve, since=utcnow(), drawdown_pct=0.0,
                    reason=("non-finite market value or cash flow in the ledger; "
                            "fix the offending fill, then human review required"),
                )
                next_state = next_state.with_halt(halt)
                halted_now = True
                # Safety state first, logging second.
                next_state.save()
                pending_event = ("sleeve_halted", {
                    "sleeve": sleeve, "reason": halt.reason,
                    "market_value": market_value, "net_flow": net_flow})
            market_value = point.market_value
            net_flow = 0.0
        else:
            point = advance(
                # Strictly before today, so re-marking within a day recomputes
                # today's point instead of stacking on it — see
                # Ledger.last_equity_point_before.
                ledger.last_equity_point_before(sleeve, day),
                sleeve=sleeve,
                as_of=day,
                market_value=market_value,
                net_flow=net_flow,
            )
            ledger.record_equity_point(point)
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
                # Safety state first, logging second: a failing audit write
                # must never leave a breached sleeve un-halted.
                next_state.save()
                pending_event = ("sleeve_halted", {
                    "sleeve": sleeve, "reason": halt.reason,
                    "drawdown_pct": halt.drawdown_pct,
                    "twr_index": point.twr_index, "hwm_index": point.hwm_index,
                })
        if pending_event is not None and audit is not None:
            audit.log_event(*pending_event)

        marks.append(SleeveMark(
            sleeve=sleeve,
            market_value=market_value,
            cost_basis=sum(p.cost_basis for p in held),
            net_flow=point.net_flow,
            point=point,
            halted_now=halted_now,
            unpriced=unpriced,
        ))

    health = router.health()
    if probe:
        # Only persisted when this call genuinely queried something —
        # either the open positions or, on an empty book, the canary. With
        # nothing probed, router.health() is still the router's constructed
        # default, and persisting it would smuggle the exact "unobserved but
        # reported anyway" bug this file exists to fix back in.
        observed_health.save(health, observed_at=utcnow())

    return MarkResult(
        as_of=day, data_health=health,
        marks=tuple(marks), state=next_state,
    )


def _priced_value(position: Position, quote: Quote | None) -> float | None:
    """qty x quoted price, or None when there is no usable quote."""
    if not quote:
        return None
    try:
        value = position.qty * quote.price
    except (TypeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _prev_or_flat(prev: EquityPoint | None, sleeve: str, day: date) -> EquityPoint:
    """The last good point, or a flat one; never recorded."""
    return prev or EquityPoint(sleeve=sleeve, as_of=day, market_value=0.0,
                               net_flow=0.0, twr_index=1.0, hwm_index=1.0,
                               drawdown_pct=0.0)


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

