# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""The loader is the only path to a gate config, and it always merges halts."""
import shutil
from pathlib import Path

import pytest

from croupier import policy as policy_mod
from croupier.gates.pipeline import check
from croupier.models import AccountSnapshot, Mode, OrderIntent

REPO = Path(__file__).resolve().parent.parent


def _workspace(tmp_path) -> Path:
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "policy.example.yaml", tmp_path / "config" / "policy.yaml")
    shutil.copy(REPO / "config" / "catalysts.example.yaml",
                tmp_path / "config" / "catalysts.yaml")
    return tmp_path


def _load(ws: Path):
    return policy_mod.load(ws / "config" / "policy.yaml",
                           ws / "data" / "sleeve_state.yaml",
                           ws / "config" / "catalysts.yaml")


def _intent() -> OrderIntent:
    return OrderIntent(sleeve="event_driven", ticker="ACME", side="buy",
                       qty=100, limit_price=2.80,
                       signal_refs=("8-K 0001234-26-000042",), thesis="t")


def test_shipped_policy_loads(tmp_path):
    p = _load(_workspace(tmp_path))
    assert p.config.sleeves["event_driven"].mode == Mode.CONFIRM
    assert p.config.sleeves["filature"].mode == Mode.HALTED
    assert p.max_sleeve_drawdown_pct == 25
    assert p.config.execution_venue == "robinhood"
    assert len(p.catalysts.events) >= 1


def test_a_halt_in_the_state_file_reaches_the_gate(tmp_path):
    """The loader is the join between the state file and the mode gate."""
    ws = _workspace(tmp_path)
    (ws / "data").mkdir()
    (ws / "data" / "sleeve_state.yaml").write_text(
        "sleeves:\n  event_driven:\n    mode: halted\n    reason: drawdown\n")
    cfg = _load(ws).config
    assert cfg.sleeves["event_driven"].mode == Mode.HALTED
    v = check(_intent(), cfg, AccountSnapshot(100_000.0, 50_000.0))
    assert not v.approved
    assert any(d.gate == "mode" and "HALTED" in d.reason for d in v.decisions)


def test_state_file_cannot_promote_a_halted_sleeve_through_the_loader(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "data").mkdir()
    (ws / "data" / "sleeve_state.yaml").write_text(
        "sleeves:\n  filature:\n    mode: auto\n")
    assert _load(ws).config.sleeves["filature"].mode == Mode.HALTED


def test_load_is_the_only_loader_exported(tmp_path):
    """A second loader returning an unmerged PolicyConfig is the hazard."""
    assert not hasattr(policy_mod, "load_policy")


def test_missing_policy_points_at_the_example(tmp_path):
    """policy.yaml is gitignored, so its absence is expected, not a crash."""
    with pytest.raises(policy_mod.PolicyNotConfigured) as exc:
        policy_mod.load(tmp_path / "config" / "policy.yaml")
    msg = str(exc.value)
    assert "policy.example.yaml" in msg and "cp " in msg


def test_shipped_example_is_a_working_starting_point(tmp_path):
    """cp the example to the real name and everything loads."""
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "policy.example.yaml", tmp_path / "config" / "policy.yaml")
    p = policy_mod.load(tmp_path / "config" / "policy.yaml",
                        tmp_path / "data" / "sleeve_state.yaml",
                        tmp_path / "config" / "catalysts.yaml")
    assert p.config.sleeves and p.catalysts.events == ()   # no calendar yet is fine
