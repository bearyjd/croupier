import pytest

from croupier.data.base import DataHealth
from croupier.gates.pipeline import PolicyConfig, SleeveConfig, check
from croupier.models import AccountSnapshot, DenyLevel, Mode, OrderIntent


def _cfg(**kw) -> PolicyConfig:
    base = dict(
        sleeves={"event_driven": SleeveConfig(
            "event_driven", budget_pct=10, position_max_pct=3)},
        denylist={}, sector_denylist={}, ticker_sector={},
    )
    base.update(kw)
    return PolicyConfig(**base)


def _snap(**kw) -> AccountSnapshot:
    base = dict(total_value=100_000.0, cash=50_000.0)
    base.update(kw)
    return AccountSnapshot(**base)


def _intent(**kw) -> OrderIntent:
    base = dict(sleeve="event_driven", ticker="ACME", side="buy",
                qty=1000, limit_price=2.80,
                signal_refs=("8-K 0001234-26-000042",), thesis="catalyst entry")
    base.update(kw)
    return OrderIntent(**base)


def test_no_provenance_cannot_construct():
    with pytest.raises(ValueError, match="signal_refs"):
        _intent(signal_refs=())


def test_clean_intent_approved_confirm_mode():
    v = check(_intent(), _cfg(), _snap())
    assert v.approved and v.requires_confirm and v.approval_id


def test_denylist_ticker_wins():
    v = check(_intent(), _cfg(denylist={"ACME": DenyLevel.NO_TRADE}), _snap())
    assert not v.approved and "denylist" in v.rejection_reasons[0]


def test_sector_etf_only_blocks_single_name_allows_etf():
    cfg = _cfg(sector_denylist={"defense": DenyLevel.ETF_ONLY},
               ticker_sector={"ACME": "defense", "ETFX": "defense"})
    assert not check(_intent(), cfg, _snap()).approved
    v = check(_intent(ticker="ETFX", is_etf=True, limit_price=95.0, qty=10), cfg, _snap())
    assert v.approved


def test_sleeve_budget_ceiling():
    snap = _snap(sleeve_cost_basis={"event_driven": 9_000.0})
    v = check(_intent(qty=1000, limit_price=2.80), _cfg(), snap)  # 2800 > 1000 headroom
    assert not v.approved and any("ceiling" in r for r in v.rejection_reasons)


def test_position_cap():
    snap = _snap(position_cost_basis={"ACME": 2_500.0})  # cap = 3000
    v = check(_intent(qty=500, limit_price=2.80), _cfg(), snap)  # +1400 > cap
    assert not v.approved


def test_sells_bypass_budget_but_not_denylist():
    snap = _snap(sleeve_cost_basis={"event_driven": 10_000.0})
    assert check(_intent(side="sell"), _cfg(), snap).approved
    assert not check(_intent(side="sell"),
                     _cfg(denylist={"ACME": DenyLevel.NO_TRADE}), snap).approved


def test_halted_sleeve_rejects():
    cfg = _cfg(sleeves={"event_driven": SleeveConfig(
        "event_driven", 10, 3, mode=Mode.HALTED)})
    assert not check(_intent(), cfg, _snap()).approved


def test_auto_mode_caps():
    cfg = _cfg(sleeves={"event_driven": SleeveConfig(
        "event_driven", 10, 3, mode=Mode.AUTO,
        auto_order_max_usd=1000, auto_daily_max_usd=1500)})
    assert not check(_intent(), cfg, _snap()).approved  # 2800 > 1000
    small = _intent(qty=300, limit_price=2.80)  # 840
    assert check(small, cfg, _snap()).approved
    assert not check(small, cfg, _snap(), auto_spent_today=800.0).approved  # daily cap


# --- PRP-002: venue + data health ---


def test_venue_gate_rejects_schwab_orders():
    v = check(_intent(venue="schwab"), _cfg(), _snap())
    assert not v.approved and any("execution venue" in r or "not an execution" in r
                                  for r in v.rejection_reasons)


def test_degraded_blocks_auto_entries_allows_exits():
    cfg = _cfg(sleeves={"event_driven": SleeveConfig(
        "event_driven", 10, 3, mode=Mode.AUTO,
        auto_order_max_usd=5000, auto_daily_max_usd=5000)})
    buy = check(_intent(), cfg, _snap(), data_health=DataHealth.DEGRADED)
    assert not buy.approved
    sell = check(_intent(side="sell"), cfg, _snap(), data_health=DataHealth.DEGRADED)
    assert sell.approved


def test_degraded_allows_confirm_buys_with_flag():
    v = check(_intent(), _cfg(), _snap(), data_health=DataHealth.DEGRADED)
    assert v.approved and v.requires_confirm
    assert any("DEGRADED" in d.reason for d in v.decisions)


def test_dead_data_rejects_everything():
    assert not check(_intent(side="sell"), _cfg(), _snap(),
                     data_health=DataHealth.DEAD).approved
