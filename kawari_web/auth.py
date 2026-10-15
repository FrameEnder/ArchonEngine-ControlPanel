"""Authentication.

Identity comes from Kawari's own `user` table, so a player signs in here with the
exact credentials they use in the launcher - there is no second set of accounts
to keep in sync. Kawari stores and compares passwords in plain text
(`selected_user.password == for_password` in servers/login/src/database.rs), so
this does the same comparison; it cannot be stronger than the game server it
front-ends.

A profile row and the service accounts behind it are what the rest of the app
hangs off: characters belong to service accounts, service accounts belong to a
user, and the profile is that user's web-facing page.
"""

from __future__ import annotations

import hmac
from functools import wraps
from pathlib import Path

from flask import current_app, flash, g, redirect, request, session, url_for

from kawari_core import dbtools

from . import webdb


def authenticate(login_db: Path, username: str, password: str) -> dict | None:
    """Return the matching user row, or None."""
    username = (username or "").strip()
    if not username or not password:
        return None
    _cols, rows, err = dbtools.query(
        login_db, "SELECT id, username, password FROM user WHERE username = ?",
        (username,))
    if err or not rows:
        return None
    row = rows[0]
    # compare_digest to avoid leaking length/prefix through timing
    if not hmac.compare_digest(str(row["password"]), str(password)):
        return None
    return {"id": row["id"], "username": row["username"]}


def service_accounts_for(login_db: Path, user_id: int) -> list:
    _cols, rows, err = dbtools.query(
        login_db, "SELECT id, max_ex, legacy FROM service_account WHERE user_id = ?",
        (user_id,))
    return [] if err else [dict(r) for r in rows]


def characters_for(world_db: Path, login_db: Path, user_id: int) -> list:
    """Every character across all of this user's service accounts."""
    owned = {sa["id"] for sa in service_accounts_for(login_db, user_id)}
    if not owned:
        return []
    rows, err = dbtools.characters(world_db)
    if err:
        return []
    return [dict(r) for r in rows if r["service_account_id"] in owned]


def load_user() -> None:
    """Populate g.user for the request from the session cookie."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    settings = current_app.config["SETTINGS"]
    prof = webdb.profile(settings.webapp_db, int(user_id))
    if prof is None:
        session.clear()
        return
    g.user = prof


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            flash("Please sign in first.", "info")
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if not user:
            flash("Please sign in first.", "info")
            return redirect(url_for("main.login", next=request.path))
        if not user.get("is_admin"):
            # Deliberately a 403 rather than a redirect: the dashboard must not
            # be reachable by guessing the URL.
            return ("<h1>403 - Administrators only</h1>"
                    "<p>The dashboard is restricted to administrator accounts.</p>"), 403
        return view(*args, **kwargs)
    return wrapped
