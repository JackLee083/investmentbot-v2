"""Tests for core/dca.py -- pure DCA/pool/SGOV math.

No DB, no network: these just call the functions with plain numbers per
PLAN_V3.md §4.
"""

import pytest

from core.dca import get_allocations, iau_budget, dip_add_draw, sgov_orders


# ---------------------------------------------------------------------------
# get_allocations
# ---------------------------------------------------------------------------


def test_get_allocations_fng_29_extreme_fear():
    assert get_allocations(29) == (0.06, 0.06, 0.18)


def test_get_allocations_fng_30_boundary():
    assert get_allocations(30) == (0.0566, 0.0566, 0.17)


def test_get_allocations_fng_54():
    assert get_allocations(54) == (0.0566, 0.0566, 0.17)


def test_get_allocations_fng_55_boundary():
    assert get_allocations(55) == (0.0533, 0.0533, 0.16)


def test_get_allocations_fng_79():
    assert get_allocations(79) == (0.0533, 0.0533, 0.16)


def test_get_allocations_fng_80_boundary_extreme_greed():
    assert get_allocations(80) == (0.05, 0.05, 0.15)


# ---------------------------------------------------------------------------
# iau_budget
# ---------------------------------------------------------------------------


def test_iau_budget_extreme_greed():
    assert iau_budget(80, 1000.0) == pytest.approx(100.0)


def test_iau_budget_greed():
    assert iau_budget(60, 1000.0) == pytest.approx(83.0)


def test_iau_budget_neutral():
    assert iau_budget(40, 1000.0) == pytest.approx(67.0)


def test_iau_budget_fear():
    assert iau_budget(20, 1000.0) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# dip_add_draw
# ---------------------------------------------------------------------------


def test_dip_add_draw_floor_case_old_pool_under_90():
    # old_pool = 100 - 50 = 50 -> pool_alloc = 50/3 = 16.67 < 30
    #   -> floored to min(30, 50) = 30
    # dynamic = min(30 + 50, 50*4, 100) = min(80, 200, 100) = 80
    assert dip_add_draw(pool=100.0, contrib=50.0) == pytest.approx(80.0)


def test_dip_add_draw_cap_at_contrib_times_4():
    # old_pool = 1000 - 10 = 990 -> pool_alloc = 330
    # dynamic = min(330 + 10, 10*4, 1000) = min(340, 40, 1000) = 40
    assert dip_add_draw(pool=1000.0, contrib=10.0) == pytest.approx(40.0)


def test_dip_add_draw_cap_at_pool_when_contrib_exceeds_pool():
    # contrib(100) > pool(50) -> old_pool = max(50-100, 0) = 0
    # pool_alloc = 0 -> dynamic = min(0+100, 400, 50) = 50 (capped at pool)
    assert dip_add_draw(pool=50.0, contrib=100.0) == pytest.approx(50.0)


def test_dip_add_draw_pool_zero():
    assert dip_add_draw(pool=0.0, contrib=50.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# sgov_orders
# ---------------------------------------------------------------------------


def test_sgov_orders_sell_branch():
    result = sgov_orders(cash=1000.0, target=1500.0, base=1000.0, price=100.0, inventory=10)
    # diff = 500 -> shares_to_sell = ceil(500/100) = 5, within inventory
    assert result == ("SELL", 5)


def test_sgov_orders_sell_diff_at_or_under_100_is_noop():
    # diff = 50 <= 100 -> no trade, not worth the friction
    assert sgov_orders(cash=1450.0, target=1500.0, base=1000.0, price=100.0, inventory=10) is None


def test_sgov_orders_sell_capped_at_inventory():
    # diff = 1400 -> shares_to_sell = ceil(1400/10) = 140, but only 3 held
    result = sgov_orders(cash=100.0, target=1500.0, base=1000.0, price=10.0, inventory=3)
    assert result == ("SELL", 3)


def test_sgov_orders_buy_branch():
    # cash(3000) > target+base(2500) -> excess = 1500
    # shares_to_buy = floor((1500-20)/100) = 14
    result = sgov_orders(cash=3000.0, target=1500.0, base=1000.0, price=100.0, inventory=0)
    assert result == ("BUY", 14)


def test_sgov_orders_buy_branch_zero_shares_is_none():
    # excess just barely over threshold but not enough for even 1 share
    result = sgov_orders(cash=2510.0, target=1500.0, base=1000.0, price=1000.0, inventory=0)
    assert result is None


def test_sgov_orders_within_target_range_is_none():
    # target <= cash <= target + base -> no rebalance needed
    result = sgov_orders(cash=2000.0, target=1500.0, base=1000.0, price=100.0, inventory=5)
    assert result is None
