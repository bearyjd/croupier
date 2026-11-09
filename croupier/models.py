"""Croupier core types.

OrderIntent.signal_refs is mandatory non-empty (PRP-001 invariant 2):
an order with no public-signal provenance cannot even be constructed.
"""
from __future__ import annotations

import enum
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime


class Mode(enum.StrEnum):
    CONFIRM = "confirm"   # human approves each order (default)
    AUTO = "auto"         # allowed only within auto caps
    HALTED = "halted"     # drawdown/manual halt: reject everything


class DenyLevel(enum.StrEnum):
    NO_TRADE = "no_trade"
    ETF_ONLY = "etf_only"


def require_finite(**values: float) -> None:
    """NaN and +/-inf make every ``x > cap`` gate comparison False (fail open).

    bool is an int subclass and strings reach here from JSON, so both are refused.
    """
    for name, value in values.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError(f"{name} must be a finite number, got {value!r}")


def require_non_negative(**values: float) -> None:
    """Finite and >= 0: a negative amount would cancel a cap it is added to."""
    require_finite(**values)
    for name, value in values.items():
        if value < 0:
            raise ValueError(f"{name} must be non-negative, got {value!r}")


@dataclass(frozen=True)
class OrderIntent:
    sleeve: str
    ticker: str
    side: str                      # buy | sell
    qty: float
    limit_price: float
    signal_refs: tuple[str, ...]   # filing IDs, PR URLs, 8-K accession nos.
    thesis: str
    is_etf: bool = False
    venue: str = "robinhood"
    adv_shares: float | None = None  # average daily volume, for order-type gate

    def __post_init__(self) -> None:
        if not self.signal_refs or not all(s.strip() for s in self.signal_refs):
            raise ValueError("OrderIntent requires non-empty public signal_refs (PRP-001 inv. 2)")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"bad side {self.side!r}")
        require_finite(qty=self.qty, limit_price=self.limit_price)
        if self.adv_shares is not None:
            require_finite(adv_shares=self.adv_shares)
        if self.qty <= 0 or self.limit_price <= 0:
            raise ValueError("qty and limit_price must be positive")

    @property
    def notional(self) -> float:
        return self.qty * self.limit_price


@dataclass(frozen=True)
class AccountSnapshot:
    """Supplied by the agent with every check, from Robinhood MCP account data."""
    total_value: float
    cash: float
    # sleeve -> current cost basis deployed
    sleeve_cost_basis: dict[str, float] = field(default_factory=dict)
    # ticker -> current position cost basis (across sleeves)
    position_cost_basis: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_finite(total_value=self.total_value, cash=self.cash)
        require_non_negative(
            **{f"sleeve_cost_basis[{k}]": v for k, v in self.sleeve_cost_basis.items()},
            **{f"position_cost_basis[{k}]": v for k, v in self.position_cost_basis.items()})


@dataclass(frozen=True)
class GateDecision:
    gate: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class Verdict:
    approved: bool
    approval_id: str | None
    requires_confirm: bool
    decisions: tuple[GateDecision, ...]

    @property
    def rejection_reasons(self) -> list[str]:
        return [d.reason for d in self.decisions if not d.passed]


def approval_id_for(intent: OrderIntent, ts: datetime) -> str:
    payload = json.dumps(
        {"sleeve": intent.sleeve, "ticker": intent.ticker, "side": intent.side,
         "qty": intent.qty, "limit": intent.limit_price, "ts": ts.isoformat()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def utcnow() -> datetime:
    return datetime.now(UTC)
