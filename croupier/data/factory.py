# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Build the PRP-002 data router from config + environment.

Schwab credentials come from the environment, never from a config file in
the repo. With no credentials the router runs Stooq-only, which reports
DEGRADED — the documented floor: the system can always mark positions daily
even with zero broker data connectivity (PRP-002 inv. 3).
"""
from __future__ import annotations

import os
from pathlib import Path

from croupier.data.router import DataRouter
from croupier.data.schwab import SchwabMarketData
from croupier.data.stooq import StooqMarketData

SCHWAB_KEY_ENV = "SCHWAB_APP_KEY"
SCHWAB_SECRET_ENV = "SCHWAB_APP_SECRET"
DEFAULT_TOKEN_PATH = Path("data/schwab_tokens.json")


def build_router(token_path: str | Path = DEFAULT_TOKEN_PATH) -> DataRouter:
    key = os.environ.get(SCHWAB_KEY_ENV)
    secret = os.environ.get(SCHWAB_SECRET_ENV)
    primary = (
        SchwabMarketData(key, secret, Path(token_path))
        if key and secret
        else None      # Market Data product only; absent creds => Stooq floor
    )
    return DataRouter(primary, StooqMarketData())
