# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Last-observed data health, persisted between CLI invocations (PRP-002).

`croupier mark` genuinely observes the floor — it queries every open
position before reading `router.health()`. `croupier journal` did not: it
built a fresh, disposable router and called `.health()` on it immediately,
which is optimistic by construction and had never been asked anything. That
gap meant `journal` reported DEGRADED whenever a key was merely configured,
not proven reachable — the exact failure PRP-002 invariant 3 exists to
close, one layer up in `cli.py`. This file is `mark`'s record of what it
actually saw, and `journal`'s substitute for building its own unobserved
router.

Same shape as `state.py`/`schwab_tokens.json`: a small JSON sidecar,
load-or-default, save-on-change. No new persistence mechanism — this repo
already keeps `data/sleeve_state.yaml`, `data/schwab_tokens.json` and
`data/ledger.db` this way.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from croupier.data.base import DataHealth
from croupier.models import utcnow

OBSERVED_HEALTH_PATH = Path("data/observed_health.json")


def load(path: str | Path = OBSERVED_HEALTH_PATH) -> DataHealth:
    """The last health `mark` actually observed.

    Absent — never marked, or a fresh deployment — means never observed,
    which cannot be DEGRADED: DEGRADED is itself a claim about having
    checked. DEAD is the only default that does not lie. Same rule for a
    file that fails to parse: a corrupt sidecar is not evidence of a healthy
    feed.
    """
    p = Path(path)
    if not p.exists():
        return DataHealth.DEAD
    try:
        return DataHealth(json.loads(p.read_text())["health"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return DataHealth.DEAD


def save(health: DataHealth, path: str | Path = OBSERVED_HEALTH_PATH,
         observed_at: datetime | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "health": str(health),
        "observed_at": (observed_at or utcnow()).isoformat(),
    }))
