# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""SQLite position ledger (PRP-001 P2).

Fills are keyed by ``approval_id``: the ledger cannot hold a position that
did not come from an approved intent, so the audit log and the ledger agree
by construction. Replaying an identical fill is a no-op (agents retry);
replaying a *different* fill under the same approval_id raises rather than
overwriting, because that means either a partial fill was re-reported with
new numbers or an approval_id was reused — both need a human.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from croupier.drawdown import EquityPoint
from croupier.models import utcnow

LEDGER_PATH = Path("data/ledger.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    approval_id TEXT PRIMARY KEY,
    sleeve      TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty         REAL NOT NULL CHECK (qty > 0),
    price       REAL NOT NULL CHECK (price > 0),
    filled_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_sleeve_idx ON fills (sleeve, filled_at);

CREATE TABLE IF NOT EXISTS equity_points (
    sleeve       TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    market_value REAL NOT NULL,
    net_flow     REAL NOT NULL,
    twr_index    REAL NOT NULL,
    hwm_index    REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    PRIMARY KEY (sleeve, as_of)
);
"""


class LedgerConflict(RuntimeError):
    """A different fill was already recorded under this approval_id."""


@dataclass(frozen=True)
class Fill:
    approval_id: str
    sleeve: str
    ticker: str
    side: str
    qty: float
    price: float
    filled_at: datetime

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def signed_flow(self) -> float:
        """Cash into positions: buys deploy capital, sells return it."""
        return self.notional if self.side == "buy" else -self.notional


@dataclass(frozen=True)
class Position:
    sleeve: str
    ticker: str
    qty: float
    cost_basis: float          # remaining basis of the open lot

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.qty if self.qty else 0.0


class Ledger:
    def __init__(self, path: str | Path = LEDGER_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- fills -------------------------------------------------------------

    def record_fill(self, fill: Fill) -> bool:
        """Insert a fill. Returns True if new, False if an identical replay."""
        existing = self._conn.execute(
            "SELECT sleeve, ticker, side, qty, price FROM fills WHERE approval_id = ?",
            (fill.approval_id,),
        ).fetchone()
        if existing is not None:
            same = (
                existing["sleeve"] == fill.sleeve
                and existing["ticker"] == fill.ticker
                and existing["side"] == fill.side
                and abs(existing["qty"] - fill.qty) < 1e-9
                and abs(existing["price"] - fill.price) < 1e-9
            )
            if same:
                return False
            raise LedgerConflict(
                f"approval_id {fill.approval_id} already recorded as "
                f"{existing['side']} {existing['qty']} {existing['ticker']} @ "
                f"{existing['price']}; refusing to overwrite with "
                f"{fill.side} {fill.qty} {fill.ticker} @ {fill.price}"
            )
        self._conn.execute(
            "INSERT INTO fills (approval_id, sleeve, ticker, side, qty, price, filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fill.approval_id, fill.sleeve, fill.ticker.upper(), fill.side,
             fill.qty, fill.price, fill.filled_at.isoformat()),
        )
        self._conn.commit()
        return True

    def fills(self, sleeve: str | None = None) -> list[Fill]:
        sql = "SELECT * FROM fills"
        args: tuple[object, ...] = ()
        if sleeve is not None:
            sql += " WHERE sleeve = ?"
            args = (sleeve,)
        sql += " ORDER BY filled_at, approval_id"
        return [
            Fill(
                approval_id=r["approval_id"], sleeve=r["sleeve"], ticker=r["ticker"],
                side=r["side"], qty=r["qty"], price=r["price"],
                filled_at=datetime.fromisoformat(r["filled_at"]),
            )
            for r in self._conn.execute(sql, args)
        ]

    # --- derived state -----------------------------------------------------

    def positions(self) -> list[Position]:
        """Open positions per (sleeve, ticker), weighted-average cost basis."""
        return _positions_from(self.fills())

    def net_flow_on(self, sleeve: str, day: date) -> float:
        """Cash deployed into this sleeve's positions on ``day`` (buys - sells)."""
        return sum(
            f.signed_flow
            for f in self.fills(sleeve)
            if f.filled_at.date() == day
        )

    def sleeves(self) -> list[str]:
        return [r["sleeve"] for r in
                self._conn.execute("SELECT DISTINCT sleeve FROM fills ORDER BY sleeve")]

    # --- equity curve ------------------------------------------------------

    def last_equity_point(self, sleeve: str) -> EquityPoint | None:
        row = self._conn.execute(
            "SELECT * FROM equity_points WHERE sleeve = ? ORDER BY as_of DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
        return _point_from_row(row) if row is not None else None

    def equity_points(self, sleeve: str) -> list[EquityPoint]:
        return [
            _point_from_row(r) for r in self._conn.execute(
                "SELECT * FROM equity_points WHERE sleeve = ? ORDER BY as_of", (sleeve,))
        ]

    def record_equity_point(self, point: EquityPoint) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO equity_points "
            "(sleeve, as_of, market_value, net_flow, twr_index, hwm_index, drawdown_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (point.sleeve, point.as_of.isoformat(), point.market_value, point.net_flow,
             point.twr_index, point.hwm_index, point.drawdown_pct),
        )
        self._conn.commit()


def _point_from_row(row: sqlite3.Row) -> EquityPoint:
    return EquityPoint(
        sleeve=row["sleeve"], as_of=date.fromisoformat(row["as_of"]),
        market_value=row["market_value"], net_flow=row["net_flow"],
        twr_index=row["twr_index"], hwm_index=row["hwm_index"],
        drawdown_pct=row["drawdown_pct"],
    )


def _positions_from(fills: Iterable[Fill]) -> list[Position]:
    """Fold fills into open positions.

    Pure to callers: ``book`` is local, so accumulating into it in place is
    invisible outside and avoids rebuilding the dict once per fill.
    """
    book: dict[tuple[str, str], tuple[float, float]] = {}   # (sleeve, ticker) -> (qty, basis)
    for f in fills:
        key = (f.sleeve, f.ticker.upper())
        qty, basis = book.get(key, (0.0, 0.0))
        if f.side == "buy":
            book[key] = (qty + f.qty, basis + f.notional)
        else:
            avg = basis / qty if qty else 0.0
            sold = min(f.qty, qty)
            book[key] = (qty - sold, max(0.0, basis - avg * sold))
    return [
        Position(sleeve=s, ticker=t, qty=q, cost_basis=b)
        for (s, t), (q, b) in sorted(book.items())
        if q > 1e-9
    ]


def new_fill(approval_id: str, sleeve: str, ticker: str, side: str,
             qty: float, price: float) -> Fill:
    return Fill(approval_id=approval_id, sleeve=sleeve, ticker=ticker.upper(),
                side=side, qty=qty, price=price, filled_at=utcnow())
