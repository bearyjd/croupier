# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Policy loading: config/policy.yaml + code-set halts.

``load()`` is the only entry point on purpose. Anything needing the gate
config takes ``load(...).config``, which has halts already merged — a
second loader that returned a raw PolicyConfig could let a halted sleeve
trade.

YAML wins over sleeve prose (PRP-001). Code state wins over YAML for halts
and only for halts — see croupier.state.merge_halts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from croupier.catalysts import DEFAULT_CATALYST_PATH, CatalystCalendar
from croupier.gates.pipeline import PolicyConfig, SleeveConfig
from croupier.models import DenyLevel, Mode
from croupier.sectors import load_ticker_sector_csv
from croupier.state import STATE_PATH, SleeveState, merge_halts

DEFAULT_POLICY_PATH = Path("config/policy.yaml")
DEFAULT_MAX_SLEEVE_DRAWDOWN_PCT = 25.0     # sleeve guardrails
DEFAULT_SCHWAB_TOKEN_PATH = Path("data/schwab_tokens.json")


@dataclass(frozen=True)
class Policy:
    """Everything the CLI needs, with halts already merged in."""

    config: PolicyConfig
    state: SleeveState
    max_sleeve_drawdown_pct: float
    schwab_token_path: Path
    catalysts: CatalystCalendar = field(default_factory=CatalystCalendar.empty)


class PolicyNotConfigured(FileNotFoundError):
    """config/policy.yaml is missing. Deliberate: it is gitignored."""


def load(policy_path: str | Path = DEFAULT_POLICY_PATH,
         state_path: str | Path = STATE_PATH,
         catalyst_path: str | Path = DEFAULT_CATALYST_PATH) -> Policy:
    p = Path(policy_path)
    if not p.exists():
        example = p.with_name(p.stem + ".example" + p.suffix)
        raise PolicyNotConfigured(
            f"{p} not found. It is gitignored on purpose — real budgets and "
            f"restricted lists should not be committed to a public repository. "
            f"Start from the worked example:\n\n    cp {example} {p}\n")
    raw = yaml.safe_load(p.read_text()) or {}
    sleeves = {
        name: SleeveConfig(
            name=name,
            budget_pct=s["budget_pct"],
            position_max_pct=s["position_max_pct"],
            mode=Mode(s.get("mode", "confirm")),
            auto_order_max_usd=s.get("auto_order_max_usd", 0.0),
            auto_daily_max_usd=s.get("auto_daily_max_usd", 0.0),
        )
        for name, s in (raw.get("sleeves") or {}).items()
    }
    config = PolicyConfig(
        sleeves=sleeves,
        denylist={k.upper(): DenyLevel(v) for k, v in (raw.get("denylist") or {}).items()},
        sector_denylist={k: DenyLevel(v) for k, v in (raw.get("sector_denylist") or {}).items()},
        # Bulk seed first, then inline entries — a hand-curated mapping in
        # policy.yaml is a deliberate act and outranks the generated file.
        ticker_sector={
            **(load_ticker_sector_csv(raw["ticker_sector_csv"])
               if raw.get("ticker_sector_csv") else {}),
            **{k.upper(): v for k, v in (raw.get("ticker_sector") or {}).items()},
        },
        min_price_for_market_orders=raw.get("min_price_for_market_orders", 10.0),
        min_adv_shares=raw.get("min_adv_shares", 1_000_000),
        execution_venue=raw.get("execution_venue", "robinhood"),
    )
    state = SleeveState.load(state_path)
    token_path = Path(
        ((raw.get("brokers") or {}).get("schwab") or {}).get(
            "token_path", DEFAULT_SCHWAB_TOKEN_PATH)
    )
    return Policy(
        config=merge_halts(config, state),
        state=state,
        catalysts=CatalystCalendar.load(catalyst_path),
        max_sleeve_drawdown_pct=raw.get(
            "max_sleeve_drawdown_pct", DEFAULT_MAX_SLEEVE_DRAWDOWN_PCT),
        schwab_token_path=token_path,
    )

