# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Build the PRP-002 data router from config + environment.

Every credential comes from the environment, never from a config file in the
repo. Two are read here and they fail differently:

  SCHWAB_APP_KEY / SCHWAB_APP_SECRET  absent -> no live feed, EOD floor only
  TWELVEDATA_API_KEY                  absent -> **no floor at all**, so DEAD

The second is new. Until 2026-08-29 the floor was Stooq, which needed no
credentials and so could be assumed present; PRP-002 invariant 3 said as much,
and it was wrong — Stooq began refusing plain HTTP clients and the assumption
became a lie the system kept telling itself. Its replacement is a free tier
with a key (PRP-004), which means an unconfigured deployment genuinely has no
price source. That is DEAD, and the router says so rather than reporting a
comfortable DEGRADED over nothing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from croupier.data.router import DataRouter
from croupier.data.schwab import SchwabMarketData
from croupier.data.twelvedata import TwelveDataMarketData

log = logging.getLogger(__name__)

SCHWAB_KEY_ENV = "SCHWAB_APP_KEY"
SCHWAB_SECRET_ENV = "SCHWAB_APP_SECRET"
TWELVEDATA_KEY_ENV = "TWELVEDATA_API_KEY"
DEFAULT_TOKEN_PATH = Path("data/schwab_tokens.json")


def build_router(token_path: str | Path = DEFAULT_TOKEN_PATH) -> DataRouter:
    key = os.environ.get(SCHWAB_KEY_ENV)
    secret = os.environ.get(SCHWAB_SECRET_ENV)
    primary = (
        SchwabMarketData(key, secret, Path(token_path))
        if key and secret
        else None      # Market Data product only; absent creds => EOD floor
    )

    eod_key = os.environ.get(TWELVEDATA_KEY_ENV)
    if eod_key:
        fallback = TwelveDataMarketData(eod_key)
    else:
        fallback = None
        log.warning(
            "%s is not set: there is no EOD price floor, so data health is "
            "DEAD and nothing trades without explicit human instruction. "
            "See docs/prp/PRP-002-broker-topology.md invariant 3.",
            TWELVEDATA_KEY_ENV)
    return DataRouter(primary, fallback)
