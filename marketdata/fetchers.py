"""Market data fetchers: raw prices + sentiment indicators + price-alert
checks. Ported from data/data_fetchers.py (Phase 0 renamed the package to
marketdata/); Phase 2 changes:

- process_asset_price() now routes by the asset's own `price_source` column
  ('yahoo'/'kraken'/'skip') instead of substring-matching the ticker name
  (a ticker containing "SOL"/"ADA"/"BNB" used to misroute to Kraken -- see
  PLAN_V3.md §1 bug list).
- Indicator fetchers (CNN F&G, Crypto F&G, VIX, news sentiment) now return
  (value, ok) tuples instead of silently returning a sentinel (101/50/0.0)
  on failure. The caller (jobs/tick.py) is responsible for storing this via
  indicators_repo.insert(name, value, ok) and falling back to last_good()
  for sizing when a fetch fails -- see §4 "F&G for sizing".
- fetch_asset_avg_cost() (used to read a "Average Cost per share" Notion
  formula column) is deleted; the SQLite equivalent is
  positions_repo.list_all() / a direct row lookup, used by
  trading/transaction_logger.py directly.
- check_price_alerts() no longer takes a pre-fetched {Ticker: Price} cache
  limited to the watchlist; it reads Active alerts from alerts_repo and
  fetches a quote for each ticker independently, so a price alert can be
  set on any ticker, not just ones already tracked in `assets`.
"""

import requests
import yfinance as yf

from config.config_loader import ALPHA_VANTAGE_KEY
from db.repo import alerts_repo

# Browser-like headers for the CNN F&G scrape endpoint -- without these it
# can reject the request as non-browser traffic. Kept local to this module
# now that services/notion_utils.py (which used to bundle this alongside
# unrelated Notion-auth headers) is gone.
SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Origin": "https://www.cnn.com",
    "Referer": "https://www.cnn.com/",
    "Accept": "application/json",
}

# --- Raw price fetchers ---


def get_kraken_price(ticker):
    """`ticker` here is a Kraken pair string, e.g. 'BTC/USD'."""
    url = f"https://api.kraken.com/0/public/Ticker?pair={ticker.replace('/', '')}"
    try:
        res = requests.get(url, timeout=10).json()
        if not res.get("error"):
            pair_key = list(res["result"].keys())[0]
            price = res["result"][pair_key]["c"][0]
            return float(price)
        print(f"Kraken error: {res['error']}")
        return None
    except Exception as e:
        print(f"Kraken price fetch failed ({ticker}): {e}")
        return None


def get_yahoo_price(ticker_symbol):
    """Latest available price via yfinance fast_info, falling back to the
    most recent 1-minute bar if fast_info is unavailable/NaN."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        price = ticker.fast_info.get("last_price")

        if price is None or price != price:  # NaN check
            df = ticker.history(period="1d", interval="1m")
            if not df.empty:
                price = df["Close"].iloc[-1]

        if price is None:
            print(f"Yahoo Finance returned None for {ticker_symbol}")
            return None

        return float(price)
    except Exception as e:
        print(f"Yahoo Finance fetch failed ({ticker_symbol}): {e}")
        return None


def get_4_week_high(ticker_symbol):
    """Highest daily high over the trailing ~28 calendar days."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        df = ticker.history(period="1mo")
        if df.empty:
            return None
        return float(df["High"].max())
    except Exception as e:
        print(f"Get 4-week high failed ({ticker_symbol}): {e}")
        return None


# --- Sentiment / market indicator fetchers ---
# Each returns (value, ok). ok=False means the fetch failed -- callers must
# NOT treat `value` as a real reading in that case (see indicator_snapshots
# schema comment: never store a failure sentinel as if it were real data).


def get_cnn_fng_index():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    try:
        res = requests.get(url, headers=SCRAPE_HEADERS, timeout=10)
        data = res.json()
        score = data.get("fear_and_greed", {}).get("score")
        if score is None:
            # JSON parsed but the score key is missing (CNN changed their
            # payload, or served an error page as JSON). This must be a
            # FAILURE, not a value: defaulting to 0 with ok=True would
            # store a fake "extreme fear" reading and drive the allocation
            # tables into their most aggressive bracket.
            print(f"CNN F&G response missing score key: {str(data)[:200]}")
            return None, False
        return int(score), True
    except Exception as e:
        print(f"CNN F&G fetch failed: {e}")
        return None, False


def get_crypto_fng_index():
    try:
        res = requests.get("https://api.alternative.me/fng/", timeout=10).json()
        return int(res["data"][0]["value"]), True
    except Exception as e:
        print(f"Crypto F&G fetch failed: {e}")
        return None, False


def get_vix_value():
    try:
        ticker = yf.Ticker("^VIX")
        data = ticker.history(period="1d")
        if data.empty:
            return None, False
        return round(float(data["Close"].iloc[-1]), 2), True
    except Exception as e:
        print(f"VIX fetch failed: {e}")
        return None, False


def get_news_sentiment_score():
    url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&apikey={ALPHA_VANTAGE_KEY}"
    try:
        res = requests.get(url, timeout=10).json()
        if "feed" not in res:
            print(f"Alpha Vantage error/limit: {res}")
            return None, False
        raw_score = float(res.get("feed", [])[0].get("overall_sentiment_score", 0))
        return round(raw_score, 2), True
    except Exception as e:
        print(f"News sentiment fetch failed: {e}")
        return None, False


# --- Business logic ---


def process_asset_price(symbol, price_source):
    """Fetch the current price for `symbol` per its `price_source`
    ('yahoo'/'kraken'/'skip' -- the assets.price_source column). Returns
    (price_or_None, source_label). Routing is by this explicit column now,
    not by substring-matching the ticker (the old logic would misroute any
    ticker containing "SOL"/"ADA"/"BNB" to Kraken)."""
    try:
        if price_source == "skip":
            return None, "Skip"
        if price_source == "kraken":
            return get_kraken_price(symbol), "Kraken"
        # default / 'yahoo'
        return get_yahoo_price(symbol), "Yahoo"
    except Exception as e:
        print(f"process_asset_price failed ({symbol}): {e}")
        return None, "Error"


def check_price_alerts():
    """Check every Active price_alerts row, fetching a fresh quote per
    ticker (independent of the watchlist -- an alert can target any
    ticker). Marks triggered alerts as 'Triggered' in the DB and returns
    the list of fired alerts (for the caller to push via LINE)."""
    fired = []
    active = alerts_repo.list(status="Active")
    if not active:
        return fired

    price_cache = {}
    for alert in active:
        ticker = alert["ticker"]
        if ticker not in price_cache:
            price_cache[ticker] = get_yahoo_price(ticker)
        current_price = price_cache[ticker]
        if current_price is None:
            continue

        target = alert["target_price"]
        direction = alert["direction"]
        triggered = (direction == "Above" and current_price >= target) or (
            direction == "Below" and current_price <= target
        )
        if triggered:
            print(f"Price alert triggered: {ticker} {direction} {target} (now {current_price})")
            alerts_repo.update_status(alert["id"], "Triggered")
            fired.append(
                {
                    "ticker": ticker,
                    "price": current_price,
                    "target": target,
                    "direction": direction,
                }
            )
    return fired
