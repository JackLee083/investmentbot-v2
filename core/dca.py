"""Pure DCA/pool/SGOV math -- no DB, no network, stdlib only.

Extracted from trading/broker_utils.py verbatim (same numbers, same branch
structure) so it can be unit tested in isolation. See PLAN_V3.md §4 for the
canonical formulas this module must match exactly.
"""

import math


# ---------------------------------------------------------------------------
# Allocation tables (by CNN Fear & Greed index)
# ---------------------------------------------------------------------------


def get_allocations(fng):
    """Return (qqq_pool_pct, stock_pool_pct, satellite_pool_pct) -- the
    fraction of BASE_AMOUNT contributed to each pool this DCA cycle, based
    on the CNN Fear & Greed reading `fng`."""
    if fng < 30:
        return 0.06, 0.06, 0.18
    elif 30 <= fng < 55:
        return 0.0566, 0.0566, 0.17
    elif 55 <= fng < 80:
        return 0.0533, 0.0533, 0.16
    else:  # fng >= 80
        return 0.05, 0.05, 0.15


def iau_budget(fng, base):
    """IAU (gold ETF) investment amount for this cycle, as a fraction of
    `base` (BASE_AMOUNT) scaled by the F&G reading. Rollover from unspent
    budget in prior months is added by the caller, not here."""
    if fng >= 80:
        return base * 0.10
    elif 55 <= fng < 80:
        return base * 0.083
    elif 30 <= fng < 55:
        return base * 0.067
    else:
        return base * 0.05


# ---------------------------------------------------------------------------
# Dip-add pool draw
# ---------------------------------------------------------------------------


def dip_add_draw(pool, contrib):
    """How much extra ("dynamic") capital to draw from `pool` for a
    dip-triggered QQQ/stock buy, given this cycle's fixed contribution
    `contrib` was already added to `pool` by the caller before calling this.

    Ported verbatim from broker_utils.py's QQQ/stock dip-add branches:

        old_pool   = max(pool - contrib, 0)     # pool level before this
                                                  # cycle's contribution
        pool_alloc = old_pool / 3                # take a third of the old pool
        if pool_alloc < 30:
            pool_alloc = min(30, old_pool)       # floor: use up to $30 even
                                                  # if a third is less than $30
        dynamic = min(pool_alloc + contrib, contrib * 4, pool)
                                                  # cap at 4x this cycle's
                                                  # contribution, and never
                                                  # more than the whole pool

    Caller is responsible for deducting the returned amount from `pool` and
    recording last_buy_price -- this function only computes the number.
    """
    old_pool = max(pool - contrib, 0)
    pool_alloc = old_pool / 3
    if pool_alloc < 30:
        pool_alloc = min(30, old_pool)
    dynamic = min(pool_alloc + contrib, contrib * 4, pool)
    return dynamic


# ---------------------------------------------------------------------------
# SGOV cash sweep
# ---------------------------------------------------------------------------


def sgov_orders(cash, target, base, price, inventory):
    """Decide the SGOV sweep order for this tick, given current available
    USD `cash`, the `target` cash level (SGOV_CASH_MULT * BASE_AMOUNT),
    `base` (BASE_AMOUNT, used as the buffer above target), the current
    SGOV `price`, and whole-share `inventory` currently held.

    Returns one of:
        ('SELL', qty)  -- cash is short of target by more than $100; sell
                          just enough whole shares to cover the gap, capped
                          at `inventory` (never oversell).
        ('BUY', qty)   -- cash exceeds target + base (BUFFER); buy whole
                          shares with the excess (minus a small $20 cushion
                          for fees/slippage).
        None           -- cash is within [target, target + base], or the
                          shortfall is <= $100 (not worth a trade), or the
                          computed share count would be 0.
    """
    if cash < target:
        diff = round(target - cash, 2)
        if diff <= 100:
            return None
        shares_to_sell = math.ceil(diff / price)
        if shares_to_sell > inventory:
            shares_to_sell = inventory
        if shares_to_sell > 0:
            return ("SELL", int(shares_to_sell))
        return None
    elif cash > (target + base):
        excess_cash = round(cash - target, 2)
        shares_to_buy = math.floor((excess_cash - 20) / price)
        if shares_to_buy > 0:
            return ("BUY", int(shares_to_buy))
        return None
    else:
        return None
