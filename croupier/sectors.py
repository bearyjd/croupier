# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Bulk ticker -> sector mapping, loaded from a CSV.

The sector denylist is only as good as the map behind it: a sector marked
`no_trade` does nothing for a ticker the map has never heard of. Hand-listing
thousands of tickers in policy.yaml is not practical, so the bulk map lives in
a CSV that a sibling project generates and both consume — same file, same
sectors, one place to fix a wrong mapping.

Format: a header row, then `ticker,sector`. Anything else in the row is
ignored, so the file can carry extra columns without breaking this reader.

Failure is quiet and empty, never an exception: a missing or malformed seed
must not stop the gate pipeline from running. It degrades the *sector*
denylist to whatever policy.yaml lists inline — the per-ticker denylist and
every other gate are unaffected.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)

TICKER_COLUMN = "ticker"
SECTOR_COLUMN = "sector"


def load_ticker_sector_csv(path: str | Path) -> dict[str, str]:
    """Read `ticker,sector` pairs. Returns {} if the file is unusable."""
    p = Path(path)
    if not p.exists():
        log.warning("sector seed %s not found; sector denylist covers only "
                    "tickers listed inline in policy.yaml", p)
        return {}
    try:
        text = p.read_text()
    except OSError as exc:
        log.warning("sector seed %s unreadable: %s", p, exc)
        return {}

    out: dict[str, str] = {}
    reader = csv.DictReader(_data_lines(text))
    if reader.fieldnames is None or TICKER_COLUMN not in reader.fieldnames:
        log.warning("sector seed %s has no %r column; ignoring", p, TICKER_COLUMN)
        return {}
    for row in reader:
        ticker = (row.get(TICKER_COLUMN) or "").strip().upper()
        sector = (row.get(SECTOR_COLUMN) or "").strip().lower()
        if not ticker or not sector:
            continue
        out[ticker] = sector
    log.info("sector seed %s: %d tickers", p, len(out))
    return out


def _data_lines(text: str):
    """Drop blank lines and '#' comments so the seed can document itself."""
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            yield line
