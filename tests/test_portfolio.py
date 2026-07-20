"""Tests for core/portfolio.py -- pure satellite-strategy math.

No DB, no network: these just call the functions with plain numbers per
PLAN_V3.md §4.
"""

import pytest

from core.portfolio import (
    tier_params,
    entry_prices,
    next_entry_amount,
    dip_trigger,
    stop_levels,
)


# ---------------------------------------------------------------------------
# tier_params
# ---------------------------------------------------------------------------


def test_tier_params():
    assert tier_params("T1") == {"drop": 0.10, "atr_mult": 1.5}
    assert tier_params("T2") == {"drop": 0.15, "atr_mult": 2.0}
    assert tier_params("T3") == {"drop": 0.20, "atr_mult": 3.0}


def test_tier_params_unknown_falls_back_to_t1():
    assert tier_params("bogus") == tier_params("T1")


# ---------------------------------------------------------------------------
# entry_prices
# ---------------------------------------------------------------------------


def test_entry_prices_t1_uniform_drop():
    # base_price = 100, T1 drop = 0.10 for ALL THREE levels (the old Notion
    # 0.30-drop level-3 formula was a typo -- see PLAN_V3.md §1).
    prices = entry_prices(100.0, "T1")
    assert prices[0] == pytest.approx(90.0)
    assert prices[1] == pytest.approx(81.0)
    assert prices[2] == pytest.approx(72.9)


def test_entry_prices_t2():
    prices = entry_prices(100.0, "T2")
    assert prices[0] == pytest.approx(85.0)
    assert prices[1] == pytest.approx(72.25)
    assert prices[2] == pytest.approx(61.4125)


def test_entry_prices_t3():
    prices = entry_prices(100.0, "T3")
    assert prices[0] == pytest.approx(80.0)
    assert prices[1] == pytest.approx(64.0)
    assert prices[2] == pytest.approx(51.2)


def test_entry_prices_none_safe():
    assert entry_prices(None, "T1") is None
    assert entry_prices(0, "T1") is None
    assert entry_prices(0.0, "T2") is None


# ---------------------------------------------------------------------------
# next_entry_amount
# ---------------------------------------------------------------------------


def test_next_entry_amount_at_count_zero():
    assert next_entry_amount(0, 1000.0) == pytest.approx(300.0)


def test_next_entry_amount_at_count_none():
    assert next_entry_amount(None, 1000.0) == pytest.approx(300.0)


def test_next_entry_amount_at_count_one():
    assert next_entry_amount(1, 1000.0) == pytest.approx(600.0)


def test_next_entry_amount_at_count_two_and_above():
    assert next_entry_amount(2, 1000.0) == pytest.approx(1000.0)
    assert next_entry_amount(3, 1000.0) == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# dip_trigger
# ---------------------------------------------------------------------------


def test_dip_trigger_boundary_inclusive():
    target = 100.0
    # exactly at the 3% cushion -> fires (inclusive boundary)
    assert dip_trigger(103.0, target) is True


def test_dip_trigger_just_above_boundary_does_not_fire():
    target = 100.0
    assert dip_trigger(103.01, target) is False


def test_dip_trigger_below_target_fires():
    assert dip_trigger(90.0, 100.0) is True


# ---------------------------------------------------------------------------
# stop_levels
# ---------------------------------------------------------------------------


def test_stop_levels_t1():
    levels = stop_levels(base_price=100.0, entry_atr=10.0, tier="T1")
    # display_stop = 100 - 10*1.5 = 85
    assert levels["display_stop"] == pytest.approx(85.0)
    # alert_threshold = 100 - 10*1.5*0.8 = 88
    assert levels["alert_threshold"] == pytest.approx(88.0)


def test_stop_levels_alert_threshold_is_tighter_than_display_stop():
    levels = stop_levels(base_price=100.0, entry_atr=10.0, tier="T3")
    # alert threshold (0.8x the ATR distance) sits ABOVE the display stop --
    # it's meant to fire as an early warning before the real stop is hit.
    assert levels["alert_threshold"] > levels["display_stop"]
