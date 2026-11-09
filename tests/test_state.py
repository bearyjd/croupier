# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Code state wins over YAML for halts — and ONLY for halts, ONLY one way."""
from datetime import UTC, datetime

from croupier.gates.pipeline import PolicyConfig, SleeveConfig
from croupier.models import Mode
from croupier.state import Halt, SleeveState, merge_halts

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _cfg(**modes) -> PolicyConfig:
    return PolicyConfig(
        sleeves={n: SleeveConfig(n, 10, 3, mode=m) for n, m in modes.items()},
        denylist={}, sector_denylist={}, ticker_sector={},
    )


def _halted(*sleeves) -> SleeveState:
    return SleeveState(halts={
        s: Halt(s, "drawdown breach", NOW, 31.4) for s in sleeves})


def test_state_halts_a_trading_sleeve():
    merged = merge_halts(_cfg(event_driven=Mode.CONFIRM), _halted("event_driven"))
    assert merged.sleeves["event_driven"].mode == Mode.HALTED


def test_state_halts_an_auto_sleeve():
    merged = merge_halts(_cfg(event_driven=Mode.AUTO), _halted("event_driven"))
    assert merged.sleeves["event_driven"].mode == Mode.HALTED


def test_empty_state_leaves_policy_untouched():
    cfg = _cfg(event_driven=Mode.CONFIRM, filature=Mode.HALTED)
    merged = merge_halts(cfg, SleeveState.empty())
    assert merged.sleeves["event_driven"].mode == Mode.CONFIRM
    assert merged.sleeves["filature"].mode == Mode.HALTED


def test_state_can_never_unhalt_a_yaml_halted_sleeve(tmp_path):
    """The direction that must never work: state promoting a halted sleeve."""
    p = tmp_path / "state.yaml"
    p.write_text("sleeves:\n  filature:\n    mode: confirm\n")
    state = SleeveState.load(p)
    assert state.halts == {}                     # non-halt entries are dropped on load
    merged = merge_halts(_cfg(filature=Mode.HALTED), state)
    assert merged.sleeves["filature"].mode == Mode.HALTED


def test_state_cannot_promote_to_auto(tmp_path):
    p = tmp_path / "state.yaml"
    p.write_text("sleeves:\n  event_driven:\n    mode: auto\n")
    merged = merge_halts(_cfg(event_driven=Mode.CONFIRM), SleeveState.load(p))
    assert merged.sleeves["event_driven"].mode == Mode.CONFIRM


def test_state_cannot_invent_a_sleeve():
    merged = merge_halts(_cfg(event_driven=Mode.CONFIRM), _halted("ghost_sleeve"))
    assert set(merged.sleeves) == {"event_driven"}


def test_merge_does_not_mutate_the_input_config():
    cfg = _cfg(event_driven=Mode.CONFIRM)
    merge_halts(cfg, _halted("event_driven"))
    assert cfg.sleeves["event_driven"].mode == Mode.CONFIRM


def test_halts_round_trip_through_the_state_file(tmp_path):
    p = tmp_path / "state.yaml"
    _halted("event_driven").save(p)
    reloaded = SleeveState.load(p)
    assert reloaded.is_halted("event_driven")
    assert reloaded.halts["event_driven"].drawdown_pct == 31.4


def test_with_halt_is_immutable_and_keeps_existing_halts():
    first = SleeveState.empty().with_halt(Halt("a", "r", NOW))
    second = first.with_halt(Halt("b", "r", NOW))
    assert set(first.halts) == {"a"}
    assert set(second.halts) == {"a", "b"}


def test_missing_state_file_is_an_empty_state(tmp_path):
    assert SleeveState.load(tmp_path / "nope.yaml").halts == {}


# --- naive / malformed timestamps in the state file ------------------------------

import pytest  # noqa: E402

from croupier.models import parse_ts  # noqa: E402


@pytest.mark.parametrize("since", [
    "2026-08-28T10:00:00",            # naive string
    "2026-08-28T10:00:00+02:00",      # aware string
    "garbage", 12345, None, "",       # malformed
])
def test_halt_with_any_since_still_halts_and_since_is_aware(tmp_path, since):
    import yaml
    p = tmp_path / "state.yaml"
    p.write_text(yaml.safe_dump({"sleeves": {"event_driven": {
        "mode": "halted", "reason": "dd", "since": since}}}))
    state = SleeveState.load(p)
    assert state.is_halted("event_driven")
    since_dt = state.halts["event_driven"].since
    assert since_dt.tzinfo is not None
    assert since_dt <= datetime.now(UTC)          # comparable with aware values


def test_yaml_native_naive_timestamp_still_halts(tmp_path):
    p = tmp_path / "state.yaml"
    p.write_text("sleeves:\n  event_driven:\n    mode: halted\n"
                 "    since: 2026-08-28T10:00:00\n")
    state = SleeveState.load(p)
    assert state.is_halted("event_driven")
    assert state.halts["event_driven"].since == datetime(2026, 8, 28, 10, tzinfo=UTC)


def test_parse_ts_naive_is_utc_and_malformed_is_none():
    assert parse_ts("2026-08-28T10:00:00") == datetime(2026, 8, 28, 10, tzinfo=UTC)
    assert parse_ts(datetime(2026, 8, 28, 10)) == datetime(2026, 8, 28, 10, tzinfo=UTC)
    assert parse_ts("2026-08-28T12:00:00+02:00") == datetime(2026, 8, 28, 10, tzinfo=UTC)
    for bad in ("garbage", None, 5, ""):
        assert parse_ts(bad) is None
