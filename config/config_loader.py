import os
import pytz
from dotenv import load_dotenv
import json
from pathlib import Path

# Load the .env file first (VPS deployment)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

est = pytz.timezone('US/Eastern')
mel_tz = pytz.timezone('Australia/Melbourne')

def get_config(key, default=None):
    return os.environ.get(key, default)

def get_list_from_env(key):
    value = os.environ.get(key)
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        print(f"Warning: {key} is malformed, check .env uses double quotes (e.g. [\"U123\"])")
        return []

NOTION_TOKEN = get_config("NOTION_TOKEN")
ALPHA_VANTAGE_KEY = get_config("ALPHA_VANTAGE_KEY")
KRAKEN_KEY = get_config("KRAKEN_KEY")
KRAKEN_SECRET = get_config("KRAKEN_SECRET")
BASE_AMOUNT = int(get_config("BASE_AMOUNT"))
KRAKEN_BTC_AMOUNT = int(BASE_AMOUNT * 0.10)
DATABASE_ID = "2f4a70df5ebd805d8fe1f5b6a1a259e5"
TRANSACTION_LOG_DB_ID = "2f4a70df5ebd80848bcdd975537addf6"
PRICE_ALERT_ID = "2faa70df5ebd80c084d5c41a3941dee6"
ASSETSDASHBOARD_PAGE_ID = "2f4a70df5ebd801d8e20c5a279b233f7"
MARKET_PAGE_IDS = {
    "Fear & Greed Index": "2f4a70df5ebd803ca468c967bbc560c9",
    "Crypto Fear & Greed Index": "2f4a70df5ebd80d7aae2c34f6f5d7ace",
    "VIX": "2f4a70df5ebd80cda240eb5083fd01cb",
    "Alpha Vantage News Sentiment": "2f4a70df5ebd8007a13cef729c4de712"
}

# --- IBKR-specific configuration ---
# Real values come from .env; the fallback here matches docker-compose's
# paper default (gnzsnz ib-gateway container's socat port: paper=4004, live=4003).
IBKR_HOST = get_config("IBKR_HOST", "ib-gateway")
IBKR_PORT = int(get_config("IBKR_PORT", 4004))
DCA_STOCK_TICKER = get_config("DCA_STOCK_TICKER")
missing_keys = [k for k, v in {
    "NOTION_TOKEN": NOTION_TOKEN,
    "KRAKEN_KEY": KRAKEN_KEY,
    "IBKR_PORT": IBKR_PORT
}.items() if not v]

if missing_keys:
    print(f"Warning: required settings not found: {', '.join(missing_keys)}")
else:
    print(f"Config loaded successfully (IBKR Port: {IBKR_PORT})")

# --- Line Bot---
LINE_ACCESS_TOKEN = get_config("LINE_ACCESS_TOKEN")
ADMIN_IDS = get_list_from_env("ADMIN_IDS")
REPORT_VIEWER_IDS = get_list_from_env("REPORT_VIEWER_IDS")
SIGNAL_VIEWER_IDS = get_list_from_env("SIGNAL_VIEWER_IDS")
LINE_CHANNEL_SECRET = get_config("LINE_CHANNEL_SECRET")