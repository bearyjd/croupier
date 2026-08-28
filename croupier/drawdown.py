# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Per-sleeve drawdown from a high-water mark (sleeve guardrails).

Sleeve equity cannot be compared against its own high-water mark directly:
buys and sells are external cash flows into the sleeve's book, so raw market
value peaks when capital is deployed and troughs when a winner is sold. A
time-weighted return (TWR) index removes flows, which is exactly what
"drawdown in this sleeve" means: performance, not deployment.

Daily return assumes flows land at end of period:

    r = (MV_end - net_flow) / MV_begin - 1

with ``net_flow`` = cash into positions that day (buys - sells). MV_begin of
zero means no capital was at risk, so the day earns no return (r = 0) rather
than dividing by zero. The index chains those returns from 1.0; the
high-water mark is its running max; drawdown is the shortfall below it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Below this the sleeve had no meaningful capital at risk; the day is flat.
_MIN_BASE_USD = 1e-9


@dataclass(frozen=True)
class EquityPoint:
    """One day of a sleeve's time-weighted equity curve."""

    sleeve: str
    as_of: date
    market_value: float
    net_flow: float
    twr_index: float
    hwm_index: float
    drawdown_pct: float

    def breaches(self, max_drawdown_pct: float) -> bool:
        return self.drawdown_pct > max_drawdown_pct


def advance(
    prev: EquityPoint | None,
    *,
    sleeve: str,
    as_of: date,
    market_value: float,
    net_flow: float,
) -> EquityPoint:
    """Return the next EquityPoint. Pure: never mutates ``prev``."""
    prev_mv = prev.market_value if prev is not None else 0.0
    prev_index = prev.twr_index if prev is not None else 1.0
    prev_hwm = prev.hwm_index if prev is not None else 1.0

    if prev_mv > _MIN_BASE_USD:
        ret = (market_value - net_flow) / prev_mv - 1.0
    else:
        ret = 0.0

    index = max(0.0, prev_index * (1.0 + ret))
    hwm = max(prev_hwm, index)
    drawdown_pct = 0.0 if hwm <= 0 else max(0.0, (1.0 - index / hwm) * 100.0)

    return EquityPoint(
        sleeve=sleeve,
        as_of=as_of,
        market_value=market_value,
        net_flow=net_flow,
        twr_index=index,
        hwm_index=hwm,
        drawdown_pct=drawdown_pct,
    )
