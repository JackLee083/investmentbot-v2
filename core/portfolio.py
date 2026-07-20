"""Pure satellite-strategy math -- no DB, no network, stdlib only.

Everything here is a plain function of its inputs so it can be unit tested
without touching SQLite, IBKR, or Kraken. `jobs/tick.py` and
`trading/broker_utils.py` are the only callers; they own reading state from
the DB and pass it in as plain args.

Ported from the v2 Notion formula columns (see PLAN_V3.md §4). The tier
lookup table itself (HV180 -> T1/T2/T3) still lives in
utils/hv_atr_calculator.determine_tier -- this module takes the resulting
tier string as an input rather than duplicating that decision.
"""

# ---------------------------------------------------------------------------
# Tier parameters
# ---------------------------------------------------------------------------

_TIER_PARAMS = {
    "T1": {"drop": 0.10, "atr_mult": 1.5},
    "T2": {"drop": 0.15, "atr_mult": 2.0},
    "T3": {"drop": 0.20, "atr_mult": 3.0},
}


def tier_params(tier):
    """Return {'drop': float, 'atr_mult': float} for a tier string
    ('T1'/'T2'/'T3'). Unknown tiers fall back to T1's parameters, mirroring
    the v2 default (`multipliers.get(tier, 1.5)` in broker_utils.py)."""
    return _TIER_PARAMS.get(tier, _TIER_PARAMS["T1"])


# ---------------------------------------------------------------------------
# Entry ladder
# ---------------------------------------------------------------------------


def entry_prices(base_price, tier):
    """Return [entry_price_1, entry_price_2, entry_price_3] for a satellite
    ticker, each a UNIFORM `drop` below the previous one:

        entry_price_1 = base_price    * (1 - drop)
        entry_price_2 = entry_price_1 * (1 - drop)
        entry_price_3 = entry_price_2 * (1 - drop)

    Per the user's final decision (PLAN_V3.md §1/§4): the old Notion formula
    used a 0.30 drop for T1's level 3 -- that was a typo, not a deliberate
    steeper level. v3 uses the same `drop` for all three levels.

    None-safe: a missing/zero base_price (e.g. a brand-new satellite that
    hasn't had its first price tick yet) returns None rather than raising.
    """
    if not base_price:
        return None
    drop = tier_params(tier)["drop"]
    ep1 = base_price * (1 - drop)
    ep2 = ep1 * (1 - drop)
    ep3 = ep2 * (1 - drop)
    return [ep1, ep2, ep3]


def next_entry_amount(entry_count, pool):
    """Dollar amount to deploy for the NEXT satellite entry, given the
    current `entry_count` (0-3) and the satellite capital pool `pool`.

        entry_count in (None, 0) -> pool * 0.30   (first entry)
        entry_count == 1         -> pool * 0.60   (second entry)
        entry_count >= 2         -> pool * 1.00   (third entry: all-in)
    """
    if not entry_count:  # None or 0
        return pool * 0.30
    if entry_count == 1:
        return pool * 0.60
    return pool * 1.00


def dip_trigger(current_price, target_price):
    """True when `current_price` has reached (or is within 3% above) the
    ladder's target entry price -- the v2 dip-buy alert condition.
    Boundary is inclusive: current_price == target_price * 1.03 fires."""
    return current_price <= target_price * 1.03


# ---------------------------------------------------------------------------
# Stop-loss levels
# ---------------------------------------------------------------------------


def stop_levels(base_price, entry_atr, tier):
    """Return {'display_stop': ..., 'alert_threshold': ...} for a satellite
    position, given its trailing-high `base_price`, the ATR value locked in
    at entry (`entry_atr`), and its `tier`.

        display_stop     = base_price - entry_atr * atr_mult
        alert_threshold   = base_price - entry_atr * atr_mult * 0.8

    The alert threshold is intentionally *tighter* (0.8x) than the displayed
    stop -- it's an early warning that fires before the "official" stop is
    actually hit, preserved unchanged from v2 (broker_utils.py:469-470).
    """
    mult = tier_params(tier)["atr_mult"]
    display_stop = base_price - entry_atr * mult
    alert_threshold = base_price - entry_atr * mult * 0.8
    return {"display_stop": display_stop, "alert_threshold": alert_threshold}
