# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""audit.jsonl is strict JSON: hostile input is recorded as text, never dropped."""
import json

import pytest

from croupier import audit as audit_mod
from croupier.audit import AuditLog


def _strict(path):
    def boom(c):
        raise AssertionError(f"bare {c} in audit log")
    return [json.loads(line, parse_constant=boom)
            for line in path.read_text().splitlines()]


def test_non_finite_floats_are_cleaned_recursively(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.log_event("e", {"a": float("nan"), "b": [float("inf"), {"c": float("-inf")}],
                        "d": (1.0, float("nan")), "big": 10**400})
    (rec,) = _strict(tmp_path / "audit.jsonl")
    assert rec["a"] == "nan" and rec["b"] == ["inf", {"c": "-inf"}]
    assert rec["d"] == [1.0, "nan"] and isinstance(rec["big"], str)


def test_allow_nan_false_is_a_backstop_if_cleaning_is_bypassed(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "_clean", lambda v: v)
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError):
        log.log_event("e", {"a": float("nan")})


def test_a_record_too_deeply_nested_to_clean_is_still_logged(tmp_path):
    deep: object = 1
    for _ in range(5000):
        deep = [deep]
    log = AuditLog(tmp_path / "audit.jsonl")
    log.log_fill("a1", "ACME", "buy", deep, 1.0, orphan=True)
    (rec,) = _strict(tmp_path / "audit.jsonl")
    assert rec["kind"] == "fill" and rec["approval_id"] == "a1"
    assert "nested" in rec["qty"]
