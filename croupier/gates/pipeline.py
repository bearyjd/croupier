"""Gate pipeline (PRP-001).

Order of gates is meaningful: denylist first (deny wins over everything),
mode last (so a CONFIRM verdict still carries all other gate results).
All gates run even after a failure so the audit log shows the full picture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from croupier.catalysts import CatalystCalendar, CatalystEvent
from croupier.data.base import DataHealth
from croupier.models import (
    AccountSnapshot,
    DenyLevel,
    GateDecision,
    Mode,
    OrderIntent,
    Verdict,
    approval_id_for,
    utcnow,
)


@dataclass(frozen=True)
class SleeveConfig:
    name: str
    budget_pct: float            # max % of total account value at cost
    position_max_pct: float      # max % per position at cost
    mode: Mode = Mode.CONFIRM
    auto_order_max_usd: float = 0.0
    auto_daily_max_usd: float = 0.0


@dataclass(frozen=True)
class PolicyConfig:
    sleeves: dict[str, SleeveConfig]
    denylist: dict[str, DenyLevel]          # ticker -> level
    sector_denylist: dict[str, DenyLevel]   # sector -> level
    ticker_sector: dict[str, str]           # ticker -> sector
    min_price_for_market_orders: float = 10.0
    min_adv_shares: float = 1_000_000
    execution_venue: str = "robinhood"


def _deny_gate(intent: OrderIntent, cfg: PolicyConfig) -> GateDecision:
    level = cfg.denylist.get(intent.ticker.upper())
    if level is None:
        sector = cfg.ticker_sector.get(intent.ticker.upper())
        level = cfg.sector_denylist.get(sector) if sector else None
    if level is None:
        return GateDecision("denylist", True, "not on restricted-list denylist")
    if level == DenyLevel.ETF_ONLY and intent.is_etf:
        return GateDecision("denylist", True, "sector is ETF_ONLY; instrument is an ETF")
    return GateDecision("denylist", False,
                        f"restricted-list denylist: {intent.ticker} is {level} (deny wins)")


def _budget_gate(intent: OrderIntent, cfg: PolicyConfig, snap: AccountSnapshot) -> GateDecision:
    sc = cfg.sleeves.get(intent.sleeve)
    if sc is None:
        return GateDecision("sleeve_budget", False, f"unknown sleeve {intent.sleeve!r}")
    if intent.side == "sell":
        return GateDecision("sleeve_budget", True, "sell reduces exposure")
    ceiling = snap.total_value * sc.budget_pct / 100
    deployed = snap.sleeve_cost_basis.get(intent.sleeve, 0.0)
    if deployed + intent.notional > ceiling:
        return GateDecision(
            "sleeve_budget", False,
            f"sleeve ceiling ${ceiling:,.0f}, deployed ${deployed:,.0f}, "
            f"order ${intent.notional:,.0f} exceeds it")
    return GateDecision("sleeve_budget", True,
                        f"${deployed + intent.notional:,.0f} of ${ceiling:,.0f} ceiling")


def _position_gate(intent: OrderIntent, cfg: PolicyConfig, snap: AccountSnapshot) -> GateDecision:
    sc = cfg.sleeves.get(intent.sleeve)
    if sc is None or intent.side == "sell":
        return GateDecision("position_cap", True, "n/a")
    cap = snap.total_value * sc.position_max_pct / 100
    held = snap.position_cost_basis.get(intent.ticker.upper(), 0.0)
    if held + intent.notional > cap:
        return GateDecision("position_cap", False,
                            f"position cap ${cap:,.0f}; held ${held:,.0f} + order exceeds it")
    return GateDecision("position_cap", True, f"within ${cap:,.0f} cap")


def _order_type_gate(intent: OrderIntent, cfg: PolicyConfig) -> GateDecision:
    # Croupier only ever approves limit orders; this gate flags thin liquidity.
    if intent.limit_price < cfg.min_price_for_market_orders and (
        intent.adv_shares is None or intent.adv_shares < cfg.min_adv_shares
    ):
        return GateDecision(
            "order_type", True,
            "LIMIT ONLY + thin-liquidity: agent must not convert to market order")
    return GateDecision("order_type", True, "limit order")


def _venue_gate(intent: OrderIntent, cfg: PolicyConfig) -> GateDecision:
    if intent.venue != cfg.execution_venue:
        return GateDecision(
            "venue", False,
            f"orders execute only on {cfg.execution_venue!r} (PRP-002); "
            f"{intent.venue!r} is not an execution venue")
    return GateDecision("venue", True, f"execution venue {cfg.execution_venue}")


def _data_health_gate(intent: OrderIntent, cfg: PolicyConfig,
                      data_health: DataHealth, mode: Mode | None) -> GateDecision:
    if data_health == DataHealth.DEAD:
        return GateDecision("data_health", False,
                            "no market data available: human-instructed orders only")
    if data_health == DataHealth.DEGRADED:
        if intent.side == "buy" and mode == Mode.AUTO:
            return GateDecision("data_health", False,
                                "DEGRADED data: no new AUTO entries (PRP-002 inv. 2)")
        return GateDecision("data_health", True,
                            "DEGRADED data: EOD fallback in use — flagged for review")
    return GateDecision("data_health", True, "fresh data")


def _catalyst_freeze_decision(freeze: CatalystEvent | None,
                              trading_days: int) -> GateDecision:
    """Never a rejection — a freeze escalates an order to human confirmation.

    Rejecting here would also block the sleeve's own exit rules from being
    re-entered later; the sleeve protocol asks for a confirm, not a wall.
    """
    if freeze is None:
        return GateDecision("catalyst_freeze", True, "no binary event window in range")
    return GateDecision(
        "catalyst_freeze", True,
        f"FREEZE: within T-{trading_days} trading days of {freeze.describe(trading_days)}"
        " — human confirmation required regardless of sleeve mode")


def _mode_gate(intent: OrderIntent, cfg: PolicyConfig, auto_spent_today: float) -> GateDecision:
    sc = cfg.sleeves.get(intent.sleeve)
    if sc is None:
        return GateDecision("mode", False, "unknown sleeve")
    if sc.mode == Mode.HALTED:
        return GateDecision("mode", False, f"sleeve {intent.sleeve} is HALTED")
    if sc.mode == Mode.CONFIRM:
        return GateDecision("mode", True, "CONFIRM: human approval required before placement")
    # AUTO
    if intent.notional > sc.auto_order_max_usd:
        return GateDecision(
            "mode", False,
            f"AUTO order ${intent.notional:,.0f} > cap ${sc.auto_order_max_usd:,.0f}")
    if auto_spent_today + intent.notional > sc.auto_daily_max_usd:
        return GateDecision("mode", False, "AUTO daily cap exceeded")
    return GateDecision("mode", True, "AUTO within caps")


def check(intent: OrderIntent, cfg: PolicyConfig, snap: AccountSnapshot,
          auto_spent_today: float = 0.0,
          data_health: DataHealth = DataHealth.FRESH,
          calendar: CatalystCalendar | None = None,
          today: date | None = None) -> Verdict:
    sc = cfg.sleeves.get(intent.sleeve)
    cal = calendar or CatalystCalendar.empty()
    # Freezes gate *adds* only: an exit into a catalyst never needs a
    # calendar's permission.
    freeze = (cal.freeze_for(intent.ticker, today or utcnow().date())
              if intent.side == "buy" else None)
    decisions = (
        _deny_gate(intent, cfg),
        _venue_gate(intent, cfg),
        _budget_gate(intent, cfg, snap),
        _position_gate(intent, cfg, snap),
        _order_type_gate(intent, cfg),
        _data_health_gate(intent, cfg, data_health, sc.mode if sc else None),
        _catalyst_freeze_decision(freeze, cal.freeze_trading_days),
        _mode_gate(intent, cfg, auto_spent_today),
    )
    approved = all(d.passed for d in decisions)
    # Monotone: a freeze can only ever raise this, never lower it.
    requires_confirm = bool(sc and sc.mode == Mode.CONFIRM) or freeze is not None
    return Verdict(
        approved=approved,
        approval_id=approval_id_for(intent, utcnow()) if approved else None,
        requires_confirm=requires_confirm,
        decisions=decisions,
    )
