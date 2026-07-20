"""Tests for services/line_notify.py -- the Phase 4 push-only refactor.

Every test runs against a temp-file SQLite DB (same INVESTBOT_DB pattern as
tests/test_repo.py) and fakes the LINE SDK's ApiClient/MessagingApi at the
module level -- nothing here ever constructs a real client with a real
token or reaches the network. LineNotifier.__init__ itself only builds a
linebot.v3.messaging.Configuration object (no I/O), so it's safe to
instantiate directly in every test.
"""

import pytest


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("INVESTBOT_DB", str(path))
    from db.database import init_db

    init_db()
    return str(path)


class FakeMessagingApi:
    """Stands in for linebot.v3.messaging.MessagingApi. Records every
    push_message call; raises instead if constructed with should_raise.
    fail_first=True makes only the FIRST push_message call raise (for the
    partial-delivery test) -- subsequent calls succeed."""

    def __init__(self, calls, should_raise=False, fail_first=False):
        self._calls = calls
        self._should_raise = should_raise
        self._fail_first = fail_first
        self._call_count = 0

    def push_message(self, push_request):
        self._call_count += 1
        if self._should_raise or (self._fail_first and self._call_count == 1):
            raise RuntimeError("simulated LINE SDK failure")
        self._calls.append(push_request)


class FakeApiClient:
    """Stands in for linebot.v3.messaging.ApiClient -- a plain context
    manager, no network on construction or __enter__/__exit__."""

    def __init__(self, configuration):
        self.configuration = configuration

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture()
def notifier(db_path, monkeypatch):
    """A fresh LineNotifier with the SDK faked and recipient lists under the
    test's control. Returns (notifier, sdk_calls) -- sdk_calls records every
    PushMessageRequest that would have gone out."""
    import services.line_notify as line_notify

    sdk_calls = []
    monkeypatch.setattr(line_notify, "ApiClient", FakeApiClient)
    monkeypatch.setattr(line_notify, "MessagingApi", lambda client: FakeMessagingApi(sdk_calls))

    n = line_notify.LineNotifier()
    n.admins = ["U_admin"]
    n.report_group = ["U_report"]
    n.signal_group = ["U_signal"]
    return n, sdk_calls


def _last_log_row():
    from db.repo import notification_repo

    rows = notification_repo.list_recent(limit=1)
    assert rows, "expected a notification_log row to have been written"
    return rows[0]


# ---------------------------------------------------------------------------
# each public send method, populated recipients -> SDK called + ok=1 row
# ---------------------------------------------------------------------------


def test_send_error_report_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.send_error_report("boom", "Test Error") is True
    assert len(sdk_calls) == 1
    row = _last_log_row()
    assert row["kind"] == "error"
    assert row["ok"] == 1


def test_send_dip_alert_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.send_dip_alert("TSLA", 200.0, 180.0, 300.0, 1) is True
    assert len(sdk_calls) == 1
    # stale Notion reference must be gone; message points at the dashboard.
    sent_text = sdk_calls[0].messages[0].text
    assert "notion" not in sent_text.lower()
    assert "Dashboard" in sent_text
    row = _last_log_row()
    assert row["kind"] == "dip"
    assert row["ok"] == 1


def test_send_stop_loss_alert_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.send_stop_loss_alert("TSLA", 150.0, 160.0, "跌破 ATR 停損 (T1 - 1.5x)") is True
    assert len(sdk_calls) == 1
    row = _last_log_row()
    assert row["kind"] == "stop_loss"
    assert row["ok"] == 1


def test_send_price_alert_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.send_price_alert("AAPL", 200.0, "漲破") is True
    assert len(sdk_calls) == 1
    row = _last_log_row()
    assert row["kind"] == "price_alert"
    assert row["ok"] == 1


def test_send_trade_report_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.send_trade_report("QQQ", "Buy", 1.5, 500.0, 750.0) is True
    assert len(sdk_calls) == 1
    row = _last_log_row()
    assert row["kind"] == "fill"
    assert row["ok"] == 1


def test_notify_weekly_login_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.notify_weekly_login() is True
    assert len(sdk_calls) == 1
    row = _last_log_row()
    assert row["kind"] == "2fa_reminder"
    assert row["ok"] == 1


def test_send_manual_dca_instruction_pushes_and_logs(notifier):
    n, sdk_calls = notifier
    assert n.send_manual_dca_instruction(["QQQ: buy $100", "GOOG: buy $50"]) is True
    assert len(sdk_calls) == 1
    row = _last_log_row()
    assert row["kind"] == "manual_dca"
    assert row["ok"] == 1


# ---------------------------------------------------------------------------
# empty recipient list -> loud failure, not a silent no-op
# ---------------------------------------------------------------------------


def test_empty_signal_group_returns_false_logs_ok0_and_never_calls_sdk(notifier):
    n, sdk_calls = notifier
    n.signal_group = []

    result = n.send_dip_alert("TSLA", 200.0, 180.0, 300.0, 1)

    assert result is False
    assert sdk_calls == []  # no SDK call attempted at all
    row = _last_log_row()
    assert row["kind"] == "dip"
    assert row["ok"] == 0


def test_empty_admins_returns_false_and_logs_ok0(notifier):
    n, sdk_calls = notifier
    n.admins = []

    result = n.send_error_report("boom")

    assert result is False
    assert sdk_calls == []
    row = _last_log_row()
    assert row["kind"] == "error"
    assert row["ok"] == 0


# ---------------------------------------------------------------------------
# SDK raising -> ok=0 row, exception swallowed (never escapes)
# ---------------------------------------------------------------------------


def test_sdk_raising_logs_ok0_and_does_not_raise(db_path, monkeypatch):
    import services.line_notify as line_notify

    sdk_calls = []
    monkeypatch.setattr(line_notify, "ApiClient", FakeApiClient)
    monkeypatch.setattr(
        line_notify, "MessagingApi", lambda client: FakeMessagingApi(sdk_calls, should_raise=True)
    )

    n = line_notify.LineNotifier()
    n.signal_group = ["U_signal"]

    result = n.send_price_alert("AAPL", 200.0, "漲破")  # must NOT raise

    assert result is False
    row = _last_log_row()
    assert row["kind"] == "price_alert"
    assert row["ok"] == 0


def test_api_client_construction_failure_logs_ok0_and_does_not_raise(db_path, monkeypatch):
    """The OUTER try/except in _broadcast: ApiClient itself blowing up
    (before any per-recipient push is attempted) must also end as an ok=0
    row with no exception escaping."""
    import services.line_notify as line_notify

    class ExplodingApiClient:
        def __init__(self, configuration):
            raise RuntimeError("simulated ApiClient construction failure")

    monkeypatch.setattr(line_notify, "ApiClient", ExplodingApiClient)

    n = line_notify.LineNotifier()
    n.signal_group = ["U_signal"]

    result = n.send_price_alert("AAPL", 200.0, "漲破")  # must NOT raise

    assert result is False
    row = _last_log_row()
    assert row["kind"] == "price_alert"
    assert row["ok"] == 0


def test_audit_write_failure_does_not_escape_or_change_outcome(notifier, monkeypatch):
    """Pins the Phase 4.1 MUST-FIX: notification_repo.add itself raising
    (SQLite locked, disk full) must be swallowed -- the send still returns
    its normal outcome and nothing escapes. Covers both the success path
    (push worked, only logging failed -> still True) and the empty-list
    path (still False)."""
    import services.line_notify as line_notify
    from db import repo

    def exploding_add(kind, message, ok):
        raise RuntimeError("simulated notification_log write failure")

    monkeypatch.setattr(repo.notification_repo, "add", staticmethod(exploding_add))

    n, sdk_calls = notifier

    # success path: push went out, only the audit write failed -> True.
    assert n.send_price_alert("AAPL", 200.0, "漲破") is True
    assert len(sdk_calls) == 1

    # empty-list path: still the loud-failure False, still no exception.
    n.signal_group = []
    assert n.send_price_alert("AAPL", 200.0, "漲破") is False
    assert len(sdk_calls) == 1  # no further SDK call

    # warn_if_recipients_empty must survive a failing audit write too.
    monkeypatch.setattr(line_notify, "ADMIN_IDS", [])
    monkeypatch.setattr(line_notify, "REPORT_VIEWER_IDS", ["U"])
    monkeypatch.setattr(line_notify, "SIGNAL_VIEWER_IDS", ["U"])
    line_notify.warn_if_recipients_empty()  # must NOT raise


def test_partial_delivery_counts_as_delivered(db_path, monkeypatch):
    """First recipient raises, second succeeds -> the alert reached
    someone: ok=1 row, returns True."""
    import services.line_notify as line_notify

    sdk_calls = []
    monkeypatch.setattr(line_notify, "ApiClient", FakeApiClient)
    monkeypatch.setattr(
        line_notify, "MessagingApi", lambda client: FakeMessagingApi(sdk_calls, fail_first=True)
    )

    n = line_notify.LineNotifier()
    n.signal_group = ["U_one", "U_two"]

    result = n.send_price_alert("AAPL", 200.0, "漲破")

    assert result is True
    assert len(sdk_calls) == 1  # exactly one of the two got through
    row = _last_log_row()
    assert row["kind"] == "price_alert"
    assert row["ok"] == 1


# ---------------------------------------------------------------------------
# warn_if_recipients_empty -- startup sanity check
# ---------------------------------------------------------------------------


def test_warn_if_recipients_empty_writes_a_row_per_empty_list(db_path, monkeypatch):
    import services.line_notify as line_notify

    monkeypatch.setattr(line_notify, "ADMIN_IDS", [])
    monkeypatch.setattr(line_notify, "REPORT_VIEWER_IDS", [])
    monkeypatch.setattr(line_notify, "SIGNAL_VIEWER_IDS", ["U_signal"])  # non-empty, no row expected

    line_notify.warn_if_recipients_empty()

    from db.repo import notification_repo

    rows = notification_repo.list_recent(limit=10)
    startup_rows = [r for r in rows if r["kind"] == "startup_check"]
    assert len(startup_rows) == 2
    assert all(r["ok"] == 0 for r in startup_rows)
    messages = " ".join(r["message"] for r in startup_rows)
    assert "ADMIN_IDS" in messages
    assert "REPORT_VIEWER_IDS" in messages
    assert "SIGNAL_VIEWER_IDS" not in messages


def test_warn_if_recipients_empty_writes_nothing_when_all_populated(db_path, monkeypatch):
    import services.line_notify as line_notify

    monkeypatch.setattr(line_notify, "ADMIN_IDS", ["U1"])
    monkeypatch.setattr(line_notify, "REPORT_VIEWER_IDS", ["U2"])
    monkeypatch.setattr(line_notify, "SIGNAL_VIEWER_IDS", ["U3"])

    line_notify.warn_if_recipients_empty()

    from db.repo import notification_repo

    rows = [r for r in notification_repo.list_recent(limit=10) if r["kind"] == "startup_check"]
    assert rows == []
