"""Dashboard authentication -- see PLAN_V3.md §1 decisions table and §2.

Two-layer design (user's final decision, do not re-litigate):
  1. Cloudflare Access is the PRIMARY gate, turned on for the whole tunnel
     hostname in the CF dashboard (email OTP / Google login). Zero code
     here -- Phase 5 wiring, out of scope for this file.
  2. This module is only the app's minimal SECOND factor: one shared
     password. The hash lives in the DASHBOARD_PASSWORD_HASH env var
     (generate it with the CLI at the bottom of this file); a correct
     password sets a signed session cookie.

Deliberately NOT implemented here (see PLAN_V3.md §1/§2, user decision):
  - IP-based lockout. Behind cloudflared, EVERY request's remote address is
    the tunnel's own internal address, not the real visitor's -- an IP
    lockout would lock out the owner (everyone shares that one IP) while
    doing nothing to slow down an actual attacker. Cloudflare Access
    already provides identity-based, rate-limited protection in front of
    this whole app.
  - ProxyFix. ProxyFix exists to make Flask trust X-Forwarded-* headers so
    request.remote_addr reflects the real client behind a reverse proxy.
    Nothing in this codebase makes a security decision based on
    remote_addr (see the no-IP-lockout point above), so there is nothing
    for ProxyFix to fix; adding it would only be attack surface for no
    benefit.
"""

import os
import secrets
import sys
from datetime import timedelta

from flask import redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from web import web_bp

def _dev_mode():
    """The single "are we in a real deployment or local dev/tests?" signal
    for the two dev-friendly fallbacks below (ephemeral secret key,
    non-Secure session cookie) -- the same flag bot_server.py already uses
    to skip the scheduler/startup tick for local runs and pytest.

    Read at call time (not cached at import time): pytest sets this env var
    via tests/conftest.py before any test module import, but computing it
    once as a module-level constant would still be fragile the moment
    anything imports web.auth before that env var is set -- a plain
    function has no such ordering dependency."""
    return os.environ.get("DISABLE_SCHEDULER") == "1"


def configure_session(app):
    """Wire FLASK_SECRET_KEY + session cookie flags onto `app`. Called once
    from bot_server.create_app() -- kept here (not in bot_server.py) so all
    auth-related config lives in one file.
    """
    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        if _dev_mode():
            # Local dev / pytest: an ephemeral key is fine -- sessions just
            # won't survive a process restart, which nobody notices outside
            # a real deployment.
            secret = secrets.token_hex(32)
            print(
                "WARNING: FLASK_SECRET_KEY not set -- using an ephemeral "
                "dev secret (sessions reset on restart). Fine for local "
                "dev/tests only; a real deployment must set this in .env."
            )
        else:
            # Refuse to start rather than silently pick a secret that
            # differs across worker restarts (which would just look like
            # random logouts) or, worse, ship a hardcoded default that
            # every deployment of this code would share.
            raise RuntimeError(
                "FLASK_SECRET_KEY is not set. Refusing to start what looks "
                "like a production run (DISABLE_SCHEDULER is not '1'). "
                "Generate one with "
                "`python -c \"import secrets; print(secrets.token_hex(32))\"` "
                "and put it in .env."
            )

    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Secure requires HTTPS to even round-trip the cookie. Cloudflare
        # tunnel terminates TLS in front of this app, so in a real
        # deployment every request the app sees really did arrive over
        # HTTPS -- Secure is correct there. In dev mode we're almost always
        # plain http://localhost with no tunnel, where a Secure cookie
        # would just silently never come back and break login.
        SESSION_COOKIE_SECURE=not _dev_mode(),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        pw_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
        if pw_hash and check_password_hash(pw_hash, password):
            session.clear()
            session["logged_in"] = True
            session.permanent = True  # picks up PERMANENT_SESSION_LIFETIME (30 days)
            # Open-redirect guard: `next` comes from the query string, so a
            # phishing link like /login?next=https://evil.example would
            # bounce a successful login straight to an attacker's site.
            # Only follow same-site RELATIVE paths: must start with a
            # single "/" ("//evil.example" is scheme-relative -- browsers
            # treat it as https://evil.example, so it must fail this check
            # too). Anything else falls back to the index page.
            next_url = request.args.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("web.index")
            return redirect(next_url)
        error = "密碼錯誤"
    return render_template("login.html", error=error)


@web_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("web.login"))


if __name__ == "__main__":
    # `python -m web.auth <password>` -- prints a ready-to-paste .env line.
    # Never store the plain password anywhere. (Other imports may print
    # their own startup lines above; the DASHBOARD_PASSWORD_HASH= line is
    # the one to copy. The RuntimeWarning about web.auth in sys.modules is
    # a harmless side effect of running a module that its own package's
    # __init__ also imports.)
    if len(sys.argv) != 2:
        print("Usage: python -m web.auth <password>")
        sys.exit(1)
    print(f"DASHBOARD_PASSWORD_HASH={generate_password_hash(sys.argv[1])}")
