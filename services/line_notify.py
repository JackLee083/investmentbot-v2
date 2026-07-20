"""Push-only LINE notifier for Investment Bot v3 -- see PLAN_V3.md §1/§5.

Phase 4 deletes inbound LINE handling entirely (line_webhook.py, the
`設定 TICKER PRICE` / `STATUS` commands, the `/callback` route). This module
now only ever pushes outbound Messaging API messages, and every single send
attempt -- including a would-be send with an empty recipient list -- is
audited to db.repo.notification_repo.

That audit is a deliberate bug fix, not just logging hygiene: PLAN_V3.md §1
traces the "dip-buy alert has never fired" root cause partly to the OLD
`_broadcast` silently no-op'ing (`if not target_ids: return`) whenever
SIGNAL_VIEWER_IDS was empty or malformed -- no error, no log line, nothing
anywhere a human would ever see. `_broadcast` below makes that failure mode
LOUD: an empty recipient list is now a printed warning AND an ok=0
notification_log row, same as any other failed send.
"""

from datetime import datetime

from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage

from config.config_loader import ADMIN_IDS, LINE_ACCESS_TOKEN, REPORT_VIEWER_IDS, SIGNAL_VIEWER_IDS, est
from db.repo import notification_repo


def _log_notification(kind, message, ok):
    """notification_repo.add, but un-crashable: the audit trail must never
    take the notification path down with it. If the DB write itself fails
    (SQLite locked past busy_timeout, disk full), print it and move on --
    the push already happened (or already failed) and the caller's return
    value must reflect THAT outcome, not the logging hiccup. Alerts fire
    mid-tick; an exception escaping here would crash the tick, which is
    exactly what _broadcast's contract forbids."""
    try:
        notification_repo.add(kind, message, ok)
    except Exception as e:
        print(f"notification_log write failed (kind={kind}, ok={ok}): {e}")


class LineNotifier:
    def __init__(self, access_token=LINE_ACCESS_TOKEN):
        self.configuration = Configuration(access_token=access_token)
        self.admins = ADMIN_IDS
        self.report_group = REPORT_VIEWER_IDS
        self.signal_group = SIGNAL_VIEWER_IDS

    def _broadcast(self, text, target_ids, kind):
        """Core send: push `text` to every id in `target_ids`, and ALWAYS
        write exactly one notification_log row describing this attempt.

        Outcomes (all audited, none ever raise out of this method):
          (a) target_ids empty -> ok=0 row, loud printed warning, return
              False. No API call is attempted at all -- this is the
              silent-drop bug fix described in the module docstring.
          (b) push_message succeeds for at least one recipient -> ok=1 row,
              return True. (A partial failure -- some recipients ok, others
              not -- still counts as delivered: the alert reached someone.)
          (c) every push_message call raises, or the API client itself
              fails to construct (e.g. LINE API outage) -> ok=0 row with
              the error text, printed, exception swallowed here so a LINE
              outage can never crash a tick or the dashboard.
        """
        if not target_ids:
            warning = f"[{kind}] Recipient list is empty, message not sent: {text[:120]}"
            print(f"LINE warning: {warning}")
            _log_notification(kind, warning, ok=False)
            return False

        unique_targets = list(set(target_ids))
        success_count = 0
        last_error = None

        try:
            with ApiClient(self.configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                for uid in unique_targets:
                    try:
                        line_bot_api.push_message(
                            PushMessageRequest(to=uid, messages=[TextMessage(text=text)])
                        )
                        success_count += 1
                    except Exception as inner_e:
                        last_error = inner_e
                        print(f"Send to {uid} failed: {inner_e}")
        except Exception as e:
            last_error = e
            print(f"LINE API connection failed: {e}")

        if success_count > 0:
            _log_notification(kind, text, ok=True)
            return True

        failure = f"[{kind}] Send failed, all {len(unique_targets)} recipient(s) failed: {last_error}"
        print(f"LINE warning: {failure}")
        _log_notification(kind, failure, ok=False)
        return False

    # Admin-only (error reports)

    def send_error_report(self, error_msg, error_type="系統錯誤"):
        msg = (
            f"🔥 {error_type} 回報\n"
            f"------------------\n"
            f"時間: {datetime.now(est).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"錯誤: {str(error_msg)[:200]}..."
        )
        return self._broadcast(msg, self.admins, "error")

    def send_trade_report(self, ticker, side, qty, price, total):
        msg = (
            f"💰 成交確認 ({side})\n"
            f"標的: {ticker}\n"
            f"價格: {price:.2f}\n"
            f"數量: {qty:.4f}\n"
            f"總額: ${total:,.0f}"
        )
        return self._broadcast(msg, self.report_group, "fill")

    def send_dip_alert(self, ticker, current_price, target_price, entry_amount, level):
        msg = (
            f"📉 抄底機會 ({ticker}) Level {level}\n"
            f"現價: {current_price:,.2f}\n"
            f"目標: {target_price:,.2f}\n"
            f"建議投入金額: ${entry_amount:,.0f}（詳情請看 Dashboard 衛星摘要）"
        )
        return self._broadcast(msg, self.signal_group, "dip")

    def send_stop_loss_alert(self, ticker, current_price, stop_price, reason, days_below=0):
        """Send a stop-loss/exit alert (notification only, no auto-trading)."""
        icon = "🛑" if "ATR" in reason else "⚠️"
        extra_info = f"連續 {days_below} 日收盤低於前高" if days_below > 0 else ""

        msg = (
            f"{icon} 手動出場警示 ({ticker})\n"
            f"------------------\n"
            f"{extra_info}\n"
            f"現價: {current_price:.2f}\n"
            f"停損價: {stop_price:.2f}\n"
        )
        return self._broadcast(msg, self.signal_group, "stop_loss")

    def send_price_alert(self, ticker, price, direction):
        msg = f"🔔 快訊提醒\n{ticker} 價格已{direction} {price:,.2f}\n"
        return self._broadcast(msg, self.signal_group, "price_alert")

    def send_manual_dca_instruction(self, instructions):
        """Send manual DCA order instructions.
        :param instructions: list of str (one line of message text per entry)
        """
        content = "\n".join(instructions)
        msg = (
            f"📝 DCA 手動交易指令\n"
            f"------------------\n"
            f"{content}\n"
            f"⚡ 請開啟 IBKR App 手動下單"
        )
        return self._broadcast(msg, self.signal_group, "manual_dca")

    def notify_weekly_login(self):
        msg = "🔑 IB Gateway 每週驗證提醒"
        return self._broadcast(msg, self.admins, "2fa_reminder")


line_bot = LineNotifier()


def warn_if_recipients_empty():
    """Startup sanity check -- call once from bot_server.create_app().

    If ADMIN_IDS / REPORT_VIEWER_IDS / SIGNAL_VIEWER_IDS parse to an empty
    list (unset env var, or malformed JSON -- config_loader.get_list_from_env
    already prints its own warning and returns [] for the latter), print a
    prominent warning AND write one notification_log ok=0 row per empty
    list, so a misconfigured recipient list is visible on the dashboard's
    notification log immediately at boot -- before the first alert that
    would otherwise have gone nowhere silently (see module docstring).

    Deliberately does NOT push anything over LINE -- this is a config check,
    not a notification.
    """
    lists = {
        "ADMIN_IDS": ADMIN_IDS,
        "REPORT_VIEWER_IDS": REPORT_VIEWER_IDS,
        "SIGNAL_VIEWER_IDS": SIGNAL_VIEWER_IDS,
    }
    for name, ids in lists.items():
        if not ids:
            warning = f"{name} is an empty list -- every LINE notification depending on it will not be sent"
            print(f"LINE config warning: {warning}")
            _log_notification("startup_check", warning, ok=False)
