import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

from db.repo import assets_repo

def get_strategy_metrics(symbol, hv_period=180, atr_period=14):
    """
    Fetch a ticker's HV180 and ATR values.
    """
    # Fetch enough history (252 trading days is roughly one year)
    df = yf.Ticker(symbol).history(period="1y")

    if df.empty:
        return None

    # Compute HV 180
    df['log_return'] = np.log(df['Close'] / df['Close'].shift(1))
    daily_std = df['log_return'].tail(hv_period).std()
    hv180 = daily_std * np.sqrt(252) * 100

    return {
        "HV180": round(hv180, 2),
        "CurrentPrice": round(df['Close'].iloc[-1], 2)
    }

def determine_tier(hv):
    """
    Classify a tier based on the HV value.
    """
    if hv < 30:
        return {"Tier": "T1", "Drop": 0.10, "ATR_Mult": 1.5}
    elif 30 <= hv < 40:
        return {"Tier": "T2", "Drop": 0.15, "ATR_Mult": 2.0}
    else:
        return {"Tier": "T3", "Drop": 0.20, "ATR_Mult": 3.0}

def check_and_lock_entry_atr():
    """
    Backfill logic: find assets where entry_count >= 3, Entry ATR is not
    yet locked, and Last Buy Date is in the past (not today), then backfill
    the ATR / close price as of that date.

    Why the delayed lock: at the moment the third entry is bought, that
    day's candle hasn't closed yet, so ATR doesn't have a "final" value
    yet. So we don't lock it at buy time -- instead we wait until the next
    day (or any later tick) and then look back at last_buy_date's
    historical data to compute and write it.
    """
    print("\n--- Checking and backfilling Entry ATR (delayed lock) ---")

    today_str = datetime.now().strftime("%Y-%m-%d")

    for asset in assets_repo.list_active():
        symbol = asset["ticker"]

        count = asset.get("entry_count") or 0
        if count < 3:
            continue

        current_atr = asset.get("entry_atr") or 0.0
        if current_atr > 0:
            continue

        last_buy_date = asset.get("last_buy_date")
        if not last_buy_date:
            continue

        if last_buy_date == today_str:
            continue

        print(f"{symbol} backfilling data (entry date: {last_buy_date})...")
        metrics = get_historical_metrics_by_date(symbol, last_buy_date)

        if metrics:
            target_atr = metrics["ATR"]
            target_close = metrics["Close"]

            print(f"Success! {symbol} locked ATR: {target_atr}, base price: {target_close}")

            assets_repo.update_fields(
                symbol,
                entry_atr=target_atr,
                stop_loss_cal_price=target_close,
            )
        else:
            print(f"Could not compute data for {symbol} on {last_buy_date}.")

def get_historical_metrics_by_date(ticker, target_date_str, period=14):
    """
    Compute the ATR and close price as of a given date.
    """
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(period="6mo")

        if df.empty: return 0.0

        # Handle columns: flatten to a single level if MultiIndex
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Strip timezone info
        df.index = df.index.tz_localize(None)
        if isinstance(df.index, pd.MultiIndex):
             df.index = df.index.get_level_values(0)

        df = df.apply(pd.to_numeric, errors='coerce')

        prev_close = df['Close'].shift(1)
        high_low = df['High'] - df['Low']
        high_close = (df['High'] - prev_close).abs()
        low_close = (df['Low'] - prev_close).abs()
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['ATR'] = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

        # Look up the target date
        df.index = df.index.strftime("%Y-%m-%d")

        if target_date_str in df.index:
            data_row = df.loc[target_date_str]

            atr_val = data_row['ATR']
            close_val = data_row['Close']

            # Defensive: make sure it's a single float, not a Series
            if isinstance(atr_val, pd.Series): atr_val = atr_val.iloc[0]
            if isinstance(close_val, pd.Series): close_val = close_val.iloc[0]

            return {
                "ATR": round(float(atr_val), 2),
                "Close": round(float(close_val), 2)
            }
        else:
            print(f"  (No candle found for {ticker} on {target_date_str})")
            return None

    except Exception as e:
        print(f"  ATR backfill calculation failed ({ticker}): {e}")
        return 0.0