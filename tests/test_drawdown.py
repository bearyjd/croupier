# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Grepon Labs
"""TWR equity index: flows must not fake a drawdown (sleeve guardrails)."""
from datetime import date

from croupier.drawdown import advance

D1, D2, D3 = date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)


def _adv(prev, day, mv, flow):
    return advance(prev, sleeve="event_driven", as_of=day,
                   market_value=mv, net_flow=flow)


def test_first_deployment_earns_no_return():
    p = _adv(None, D1, mv=2800.0, flow=2800.0)
    assert p.twr_index == 1.0 and p.hwm_index == 1.0 and p.drawdown_pct == 0.0


def test_price_drop_is_a_drawdown():
    p1 = _adv(None, D1, mv=2800.0, flow=2800.0)
    p2 = _adv(p1, D2, mv=2100.0, flow=0.0)          # 2.80 -> 2.10
    assert round(p2.twr_index, 6) == 0.75
    assert round(p2.drawdown_pct, 6) == 25.0
    assert p2.breaches(25.0) is False                # strictly greater than
    assert _adv(p1, D2, mv=2000.0, flow=0.0).breaches(25.0) is True


def test_adding_capital_is_not_a_gain():
    p1 = _adv(None, D1, mv=2800.0, flow=2800.0)
    p2 = _adv(p1, D2, mv=5600.0, flow=2800.0)       # doubled size, flat price
    assert round(p2.twr_index, 6) == 1.0 and p2.drawdown_pct == 0.0


def test_selling_a_winner_is_not_a_drawdown():
    """The bug this formula exists to avoid: exiting at a profit must not halt."""
    p1 = _adv(None, D1, mv=2800.0, flow=2800.0)
    p2 = _adv(p1, D2, mv=4000.0, flow=0.0)          # mark up
    p3 = _adv(p2, D3, mv=0.0, flow=-4000.0)         # sell everything at the mark
    assert p3.market_value == 0.0
    assert round(p3.twr_index, 6) == round(p2.twr_index, 6)
    assert p3.drawdown_pct == 0.0


def test_partial_sell_above_cost_reports_the_real_return():
    p1 = _adv(None, D1, mv=4000.0, flow=4000.0)     # 1000sh @ 4.00
    p2 = _adv(p1, D2, mv=2500.0, flow=-2500.0)      # sell 500 @ 5.00, 500 left @ 5.00
    assert round(p2.twr_index, 6) == 1.25           # the stock rose 25%, not 66%


def test_flat_day_with_no_capital_at_risk():
    p1 = _adv(None, D1, mv=0.0, flow=0.0)
    assert p1.twr_index == 1.0 and p1.drawdown_pct == 0.0
