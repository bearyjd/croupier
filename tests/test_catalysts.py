# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""Catalyst calendar + the T-5 freeze. A freeze escalates; it never loosens."""
from datetime import date
from pathlib import Path

import pytest

from croupier.catalysts import (
    CatalystCalendar,
    CatalystEvent,
    minus_trading_days,
)
from croupier.gates.pipeline import PolicyConfig, SleeveConfig, check
from croupier.models import AccountSnapshot, Mode, OrderIntent

REPO = Path(__file__).resolve().parent.parent
# 2026-09-22 is a Tuesday; five weekdays back is 2026-09-15.
EVENT_START, EVENT_END = date(2026, 9, 22), date(2026, 10, 30)
FREEZE_START = date(2026, 9, 15)


def _event(ticker="ACME", start=EVENT_START, end=EVENT_END) -> CatalystEvent:
    return CatalystEvent(ticker, "phase_data_readout", start, end,
                         "https://www.sec.gov/cgi-bin/browse-edgar?CIK=ACME")


def _cal(*events, days=5) -> CatalystCalendar:
    return CatalystCalendar(events=events or (_event(),), freeze_trading_days=days)


def _cfg(mode=Mode.CONFIRM) -> PolicyConfig:
    return PolicyConfig(
        sleeves={"event_driven": SleeveConfig(
            "event_driven", 10, 3, mode=mode,
            auto_order_max_usd=10_000, auto_daily_max_usd=10_000)},
        denylist={}, sector_denylist={}, ticker_sector={})


def _snap() -> AccountSnapshot:
    return AccountSnapshot(total_value=100_000.0, cash=50_000.0)


def _intent(**kw) -> OrderIntent:
    base = dict(sleeve="event_driven", ticker="ACME", side="buy", qty=1000,
                limit_price=2.80, signal_refs=("8-K 0001234-26-000042",),
                thesis="catalyst entry near financing floor")
    base.update(kw)
    return OrderIntent(**base)


# --- trading-day arithmetic ------------------------------------------------

def test_minus_trading_days_skips_weekends():
    # Tue 2026-09-22 back 5 weekdays -> Tue 2026-09-15
    assert minus_trading_days(date(2026, 9, 22), 5) == date(2026, 9, 15)
    # Mon 2026-09-21 back 1 weekday -> Fri 2026-09-18
    assert minus_trading_days(date(2026, 9, 21), 1) == date(2026, 9, 18)


def test_minus_zero_days_is_a_no_op():
    assert minus_trading_days(EVENT_START, 0) == EVENT_START


# --- window boundaries -----------------------------------------------------

@pytest.mark.parametrize("day,frozen", [
    (date(2026, 9, 14), False),   # T-6 trading days
    (FREEZE_START, True),         # T-5 exactly
    (EVENT_START, True),          # event window opens
    (EVENT_END, True),            # last day of the window
    (date(2026, 10, 31), False),  # window closed
])
def test_freeze_window_boundaries(day, frozen):
    assert bool(_cal().freeze_for("ACME", day)) is frozen


def test_freeze_is_per_ticker():
    assert _cal().freeze_for("BRVO", EVENT_START) is None


def test_ticker_lookup_is_case_insensitive():
    assert _cal().freeze_for("acme", EVENT_START) is not None


def test_freezes_on_lists_every_active_event():
    cal = _cal(_event("ACME"), _event("BRVO"), _event("CDLR", date(2027, 1, 4), date(2027, 2, 1)))
    assert [e.ticker for e in cal.freezes_on(EVENT_START)] == ["ACME", "BRVO"]


# --- effect on the gate pipeline ------------------------------------------

def test_freeze_escalates_an_auto_sleeve_to_confirm():
    v = check(_intent(), _cfg(Mode.AUTO), _snap(), calendar=_cal(), today=EVENT_START)
    assert v.approved is True            # a freeze is not a rejection
    assert v.requires_confirm is True    # ...it is an escalation
    assert any(d.gate == "catalyst_freeze" and "FREEZE" in d.reason for d in v.decisions)


def test_auto_sleeve_outside_the_window_stays_auto():
    v = check(_intent(), _cfg(Mode.AUTO), _snap(), calendar=_cal(), today=date(2026, 9, 14))
    assert v.approved is True and v.requires_confirm is False


def test_freeze_never_lowers_a_confirm_sleeve():
    inside = check(_intent(), _cfg(Mode.CONFIRM), _snap(), calendar=_cal(), today=EVENT_START)
    outside = check(_intent(), _cfg(Mode.CONFIRM), _snap(), calendar=_cal(),
                    today=date(2026, 9, 14))
    assert inside.requires_confirm is True and outside.requires_confirm is True


def test_freeze_does_not_gate_exits():
    """An exit into a catalyst must not need the calendar's permission."""
    v = check(_intent(side="sell"), _cfg(Mode.AUTO), _snap(),
              calendar=_cal(), today=EVENT_START)
    assert v.approved is True and v.requires_confirm is False
    assert any(d.gate == "catalyst_freeze" and "no binary event" in d.reason
               for d in v.decisions)


def test_freeze_applies_regardless_of_which_sleeve_buys():
    cfg = PolicyConfig(
        sleeves={"filature": SleeveConfig("filature", 10, 3, mode=Mode.AUTO,
                                          auto_order_max_usd=10_000,
                                          auto_daily_max_usd=10_000)},
        denylist={}, sector_denylist={}, ticker_sector={})
    v = check(_intent(sleeve="filature"), cfg, _snap(), calendar=_cal(), today=EVENT_START)
    assert v.requires_confirm is True


def test_freeze_does_not_rescue_a_rejected_order():
    """Escalation must not paper over a real gate failure."""
    v = check(_intent(venue="schwab"), _cfg(Mode.AUTO), _snap(),
              calendar=_cal(), today=EVENT_START)
    assert v.approved is False and v.approval_id is None


def test_no_calendar_means_no_freeze_and_no_crash():
    v = check(_intent(), _cfg(Mode.AUTO), _snap(), today=EVENT_START)
    assert v.approved is True and v.requires_confirm is False


# --- the seeded file -------------------------------------------------------

def test_shipped_calendar_template_is_wellformed():
    """The shipped file is a template; only its shape is guaranteed."""
    cal = CatalystCalendar.load(REPO / "config" / "catalysts.yaml")
    assert cal.freeze_trading_days == 5
    assert cal.events, "the template should carry one worked example"
    for e in cal.events:
        assert e.source_url.startswith("https://")
        assert e.window_start <= e.window_end
        assert e.verified is False


def test_missing_calendar_file_is_an_empty_calendar(tmp_path):
    assert CatalystCalendar.load(tmp_path / "nope.yaml").events == ()
