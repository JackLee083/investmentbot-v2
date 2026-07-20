import pandas_market_calendars as mcal
from datetime import datetime, timedelta
from config.config_loader import est
import pytz

def get_trading_days():
    try:
        nasdaq = mcal.get_calendar('NASDAQ')
        now = datetime.now(est)
        
        # Set the anchor date
        start_date = now.replace(day=1)
        end_date = (start_date + timedelta(days=32)).replace(day=1)
        
        schedule = nasdaq.schedule(start_date=start_date, end_date=end_date)
        return schedule.index.date
    except Exception as e:
        print(f"Calendar Error: {e}")
        return []

def is_dca_day():
    trading_days = get_trading_days()
    today = datetime.now(est).date()
    
    if len(trading_days) == 0:
        return None
    
    if today == trading_days[0]:
        return "First_Day"
    
    # Mid-month logic: find the trading day in the month closest to the 15th
    # (measure each trading day's distance from the 15th)
    closest_day_to_15 = min(trading_days, key=lambda d: abs(d.day - 15))
    
    if today == closest_day_to_15:
        if closest_day_to_15 != trading_days[0]:
            return "Mid_Month"
    return None

def is_nyse_dca_window():
    """
    Check whether it's currently within the DCA window, two hours after
    the US market opens ET (11:30 AM ET); DST is handled automatically
    since `est` is a pytz timezone.
    """
    # Target time window: 11:20-11:45 AM ET
    now = datetime.now(est)
    start_time = now.replace(hour=11, minute=20, second=0, microsecond=0)
    end_time = now.replace(hour=11, minute=45, second=0, microsecond=0)
    return start_time <= now <= end_time

def check_dca_schedule():
    """
    Check whether today is a US-stock DCA day.
    """

    dca_type = is_dca_day()
    if dca_type:
        print(f"Today is a DCA trading day: {dca_type}")
        # Add any other global reminder needs here in the future
    return
