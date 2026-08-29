# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""The bulk sector map behind the sector denylist.

A sector marked no_trade only bites on tickers the map knows, so this
loader's failure modes decide how much of the denylist actually works.
"""
import shutil
from pathlib import Path

from croupier.gates.pipeline import check
from croupier.models import AccountSnapshot, DenyLevel, OrderIntent
from croupier.policy import load
from croupier.sectors import load_ticker_sector_csv

REPO = Path(__file__).resolve().parent.parent
HEADER = "ticker,sector\n"


def _csv(tmp_path, body: str, name="seed.csv") -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_reads_ticker_sector_pairs(tmp_path):
    got = load_ticker_sector_csv(_csv(tmp_path, HEADER + "ACME,defense\nBRVO,tech\n"))
    assert got == {"ACME": "defense", "BRVO": "tech"}


def test_tickers_upper_sectors_lower(tmp_path):
    got = load_ticker_sector_csv(_csv(tmp_path, HEADER + "acme,DEFENSE\n"))
    assert got == {"ACME": "defense"}


def test_extra_columns_are_ignored(tmp_path):
    body = "ticker,sector,name\nACME,defense,Acme Corp\n"
    assert load_ticker_sector_csv(_csv(tmp_path, body)) == {"ACME": "defense"}


def test_comments_and_blank_lines_are_skipped(tmp_path):
    body = "# generated 2026-08-28\n\n" + HEADER + "ACME,defense\n\n# trailing\n"
    assert load_ticker_sector_csv(_csv(tmp_path, body)) == {"ACME": "defense"}


def test_rows_missing_a_field_are_dropped_not_guessed(tmp_path):
    body = HEADER + "ACME,\n,tech\nBRVO,energy\n"
    assert load_ticker_sector_csv(_csv(tmp_path, body)) == {"BRVO": "energy"}


def test_missing_file_is_empty_not_an_exception(tmp_path):
    """A bad seed must never stop the gate pipeline from running."""
    assert load_ticker_sector_csv(tmp_path / "nope.csv") == {}


def test_file_without_a_ticker_column_is_ignored(tmp_path):
    assert load_ticker_sector_csv(_csv(tmp_path, "symbol,sector\nACME,defense\n")) == {}


def test_empty_file_is_empty(tmp_path):
    assert load_ticker_sector_csv(_csv(tmp_path, "")) == {}


# --- integration with the policy loader and the deny gate ------------------

def _workspace(tmp_path, csv_body: str, extra: str) -> Path:
    (tmp_path / "config").mkdir()
    policy = (REPO / "config" / "policy.example.yaml").read_text() + extra
    (tmp_path / "config" / "policy.yaml").write_text(policy)
    shutil.copy(REPO / "config" / "catalysts.example.yaml",
                tmp_path / "config" / "catalysts.yaml")
    (tmp_path / "seed.csv").write_text(csv_body)
    return tmp_path


def _load(ws: Path):
    return load(ws / "config" / "policy.yaml", ws / "data" / "state.yaml",
                ws / "config" / "catalysts.yaml")


def test_csv_feeds_the_sector_denylist_end_to_end(tmp_path):
    ws = _workspace(tmp_path, HEADER + "ACME,defense\n",
                    f'\nticker_sector_csv: {tmp_path / "seed.csv"}\n'
                    '\nsector_denylist:\n  defense: no_trade\n')
    cfg = _load(ws).config
    assert cfg.ticker_sector["ACME"] == "defense"
    assert cfg.sector_denylist["defense"] == DenyLevel.NO_TRADE
    v = check(OrderIntent(sleeve="event_driven", ticker="ACME", side="buy", qty=10,
                          limit_price=5.0, signal_refs=("8-K 1",), thesis="t"),
              cfg, AccountSnapshot(100_000.0, 50_000.0))
    assert not v.approved
    assert any(d.gate == "denylist" and not d.passed for d in v.decisions)


def test_inline_entries_outrank_the_generated_csv(tmp_path):
    """A hand-curated mapping is deliberate; the seed is bulk-generated."""
    ws = _workspace(tmp_path, HEADER + "ACME,tech\n",
                    f'\nticker_sector_csv: {tmp_path / "seed.csv"}\n'
                    '\nticker_sector:\n  ACME: defense\n')
    assert _load(ws).config.ticker_sector["ACME"] == "defense"


def test_absent_csv_setting_keeps_the_shipped_behaviour(tmp_path):
    ws = _workspace(tmp_path, "", "")
    assert _load(ws).config.ticker_sector == {}
