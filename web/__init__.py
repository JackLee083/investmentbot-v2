"""Dashboard blueprint for Investment Bot v3 -- see PLAN_V3.md §5.

Everything under this package reads SQLite only (db.repo), plus exactly one
deliberate exception: web/views.py's /system route opens a short-lived raw
TCP socket to probe the IB Gateway port (documented there). No module in
this package may import services.ibkr's connect_ib, ccxt, ib_insync, or the
LINE SDK -- that is the "dashboard never talks to IB/ccxt/yfinance/LINE"
hard rule from the plan.

`web_bp` owns its own '/static' URL (see bot_server.create_app(), which
builds the Flask app with static_folder=None so there is only ever one
'/static' route in the whole app -- avoiding an ambiguous duplicate route
between the app's default static handler and this blueprint's).
"""

from flask import Blueprint, redirect, request, session, url_for

web_bp = Blueprint(
    "web",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

# Endpoints reachable without a logged-in session. Both are load-bearing:
# 'web.login' obviously can't require login-to-see-the-login-page, and
# 'web.healthz' is Docker's healthcheck target -- it has no session and must
# never redirect (see PLAN_V3.md §5 healthz row: "no auth").
_PUBLIC_ENDPOINTS = {"web.login", "web.healthz"}


@web_bp.before_request
def _require_login():
    # Also let the blueprint's own vendored static assets (pico.min.css,
    # htmx.min.js) through unauthenticated -- the login page itself needs
    # them to render, before there's any session yet.
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint == "web.static":
        return None
    if not session.get("logged_in"):
        return redirect(url_for("web.login", next=request.path))
    return None


# Imported for side effect: registers all routes onto web_bp. Placed at the
# bottom (after web_bp exists) because web/auth.py and web/views.py both do
# `from web import web_bp` -- importing them any earlier would be circular.
from web import auth, views  # noqa: E402,F401
