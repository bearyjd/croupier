# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Code-set sleeve state, merged over config/policy.yaml.

The merge is deliberately **monotone in one direction**: state may HALT a
sleeve that policy.yaml leaves trading, and may never un-halt a sleeve that
policy.yaml halts, loosen a mode, or invent a sleeve policy.yaml does not
declare. Prose cannot loosen code (PRP-001) and neither can runtime state:
the only thing an automatic drawdown trip is allowed to do is stop trading.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import yaml

from croupier.gates.pipeline import PolicyConfig
from croupier.models import Mode, utcnow

STATE_PATH = Path("data/sleeve_state.yaml")


@dataclass(frozen=True)
class Halt:
    sleeve: str
    reason: str
    since: datetime
    drawdown_pct: float | None = None

    def as_dict(self) -> dict:
        return {
            "mode": str(Mode.HALTED),
            "reason": self.reason,
            "since": self.since.isoformat(),
            "drawdown_pct": self.drawdown_pct,
        }


@dataclass(frozen=True)
class SleeveState:
    """Halts only. Anything else in the file is ignored, by design."""

    halts: dict[str, Halt]

    def is_halted(self, sleeve: str) -> bool:
        return sleeve in self.halts

    @classmethod
    def empty(cls) -> SleeveState:
        return cls(halts={})

    @classmethod
    def load(cls, path: str | Path = STATE_PATH) -> SleeveState:
        p = Path(path)
        if not p.exists():
            return cls.empty()
        raw = yaml.safe_load(p.read_text()) or {}
        halts = {}
        for sleeve, entry in (raw.get("sleeves") or {}).items():
            # Only 'halted' is honoured. A state file claiming 'auto' or
            # 'confirm' cannot promote a sleeve — it is silently ignored.
            if (entry or {}).get("mode") != str(Mode.HALTED):
                continue
            halts[sleeve] = Halt(
                sleeve=sleeve,
                reason=entry.get("reason", "halted by code state"),
                since=_parse_ts(entry.get("since")),
                drawdown_pct=entry.get("drawdown_pct"),
            )
        return cls(halts=halts)

    def with_halt(self, halt: Halt) -> SleeveState:
        """Return a new state including ``halt``. Existing halts are kept."""
        if halt.sleeve in self.halts:
            return self
        return SleeveState(halts={**self.halts, halt.sleeve: halt})

    def save(self, path: str | Path = STATE_PATH) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "_comment": (
                "Code-set sleeve state. Merged OVER config/policy.yaml, halts only: "
                "this file can stop a sleeve trading, never start one. Clearing a "
                "halt is a deliberate human act - delete the entry after review."
            ),
            "sleeves": {s: h.as_dict() for s, h in sorted(self.halts.items())},
        }
        p.write_text(yaml.safe_dump(body, sort_keys=False))


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return utcnow()


def merge_halts(cfg: PolicyConfig, state: SleeveState) -> PolicyConfig:
    """Apply code-set halts over a YAML policy. Never loosens, never creates."""
    merged = {
        name: replace(sc, mode=Mode.HALTED) if state.is_halted(name) else sc
        for name, sc in cfg.sleeves.items()
    }
    return replace(cfg, sleeves=merged)
