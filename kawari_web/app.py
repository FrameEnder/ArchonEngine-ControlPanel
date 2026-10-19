"""Flask application: factory, first-run setup, and the public pages."""

from __future__ import annotations

import secrets
from pathlib import Path

from flask import (
    Blueprint, Flask, abort, current_app, flash, g, redirect, render_template,
    request, send_from_directory, session, url_for,
)

from kawari_core import dbtools

from . import auth, stats, upstreamcfg, webdb
from .config import Settings
from .jobs import JobRunner
from .podman_api import Podman

main = Blueprint("main", __name__)

ALLOWED_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024


# ════════════════════════════════════════════════════════════════════════════
#  Factory
# ════════════════════════════════════════════════════════════════════════════
def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    webdb.init(settings.webapp_db)
    from kawari_core import shop as shop_core
    shop_core.init(settings.webapp_db)
    # Which Kawari repository this installation tracks. Process-wide state, so
    # it has to be set before anything reads upstream - including the build
    # adoption that BuildStore does on first use.
    upstreamcfg.apply(settings.webapp_db)

    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.config["PODMAN"] = Podman(settings.podman_socket)
    app.secret_key = webdb.secret_key(settings.webapp_db)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.config["JOBS"] = JobRunner(app)
    # Importing these registers their routes on the shared blueprint.
    from . import admin_base, dashboard as _core, admin_setup, admin_updates, admin_character, questlab, questtest, progress, diagnostics, marketboard  # noqa: F401
    from .social import social
    from .shop import shop
    from .syncapi import api
    app.register_blueprint(main)
    app.register_blueprint(social)
    app.register_blueprint(shop)
    app.register_blueprint(api)
    app.register_blueprint(admin_base.dashboard, url_prefix="/dashboard")

    @app.before_request
    def _before():
        auth.load_user()
        _watch_server_state(settings, app.config["PODMAN"])
        # CSRF: SameSite=Lax already blocks cross-site form posts, but a token
        # costs nothing and covers same-site injection too.
        if request.method == "POST" and not request.path.startswith("/api/"):
            # /api/ is authenticated by bearer token, not by the session cookie,
            # so CSRF does not apply there.
            token = session.get("csrf")
            if not token or request.form.get("csrf") != token:
                return ("<h1>400 - Session expired</h1><p>Reload the page and try "
                        "again.</p>"), 400
        # Until an administrator exists, everything redirects to first-run setup.
        if not webdb.has_any_profile(settings.webapp_db):
            allowed = {"main.setup", "main.static_upload", "static",
                       "api.sync", "api.ping", "api.diag_snapshot", "api.diag_ping", "api.diag_battle"}
            if request.endpoint not in allowed:
                return redirect(url_for("main.setup"))
        return None

    @app.context_processor
    def _context():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        from . import __version__
        user = getattr(g, "user", None)
        unread, recent = 0, []
        if user:
            unread = webdb.unread_count(settings.webapp_db, user["user_id"])
            recent = webdb.notifications_for(settings.webapp_db, user["user_id"], 6)
        brand = webdb.branding(settings.webapp_db)
        return {
            "csrf_token": session["csrf"],
            "app_version": __version__,
            "unread_count": unread,
            "recent_notifications": recent,
            "branding": brand,
            "on_accent": on_accent(brand.get("accent", "")),
            "user": getattr(g, "user", None),
            "settings": settings,
        }

    @app.errorhandler(404)
    def _404(_e):
        return render_template("error.html", code=404,
                               message="That page does not exist."), 404

    return app


def on_accent(colour: str) -> str:
    """Text colour for things painted in the accent colour: dark on light accents,
    white on dark ones, whichever gives more contrast (WCAG relative luminance)."""
    value = (colour or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        r, g_, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#0b1220"

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * lin(r) + 0.7152 * lin(g_) + 0.0722 * lin(b)
    dark = 0.0066                       # luminance of #0b1220
    on_dark = (lum + 0.05) / (dark + 0.05)
    on_white = 1.05 / (lum + 0.05)
    return "#0b1220" if on_dark >= on_white else "#ffffff"


def _watch_server_state(settings, podman) -> None:
    """Notice the game server starting or stopping and tell everyone.

    Checked on requests rather than by a background timer: there is no scheduler
    here, and a state change nobody is around to see does not need announcing.
    Rate-limited so it costs at most one podman call every 20 seconds.
    """
    import time
    now = time.time()
    last = current_app.config.get("_server_state_checked", 0)
    if now - last < 20 or podman.mode == "none":
        return
    current_app.config["_server_state_checked"] = now
    try:
        running = podman.inspect(settings.shell.container).get("running", False)
    except Exception:                                   # noqa: BLE001
        return
    previous = webdb.get_setting(settings.webapp_db, "server_running", "")
    state = "1" if running else "0"
    if previous == state:
        return
    webdb.set_setting(settings.webapp_db, "server_running", state)
    if previous == "":
        return                                          # first ever check: nothing to report
    if running:
        webdb.notify_all(settings.webapp_db, "server", "The server is online",
                         "You can log in now.", url_for("main.connect"))
    else:
        webdb.notify_all(settings.webapp_db, "server", "The server went offline",
                         "An administrator has stopped it, or it crashed.",
                         url_for("main.home"))


# ════════════════════════════════════════════════════════════════════════════
#  First run
# ════════════════════════════════════════════════════════════════════════════
@main.route("/setup", methods=["GET", "POST"])
def setup():
    """Runs only while no profile exists: create the first admin, or adopt the
    accounts already in an existing login.db and pick which one is admin."""
    settings = _settings()
    if webdb.has_any_profile(settings.webapp_db):
        return redirect(url_for("main.home"))

    existing, err = dbtools.users(settings.login_db)
    existing = [] if err else [dict(r) for r in existing]

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "adopt":
            try:
                user_id = int(request.form.get("user_id", "0"))
            except ValueError:
                user_id = 0
            match = next((u for u in existing if u["id"] == user_id), None)
            if not match:
                flash("Select which account should be the administrator.", "err")
                return redirect(url_for("main.setup"))
            webdb.ensure_profile(settings.webapp_db, match["id"], match["username"],
                                 is_admin=True)
            for other in existing:
                if other["id"] != match["id"]:
                    webdb.ensure_profile(settings.webapp_db, other["id"],
                                         other["username"], is_admin=False)
            flash(f"Imported {len(existing)} account(s). '{match['username']}' is the "
                  f"administrator - sign in with that account's game password.", "ok")
            return redirect(url_for("main.login"))

        if action == "create":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""
            if password != confirm:
                flash("The passwords do not match.", "err")
                return redirect(url_for("main.setup"))
            from kawari_core.core import inspect_game_path
            info = inspect_game_path(settings.shell.game_path)
            max_ex = info["max_ex"] if info["repos"] else 5
            try:
                user_id, _sa = dbtools.create_account(settings.login_db, username,
                                                      password, max_ex)
            except Exception as exc:                    # noqa: BLE001
                flash(f"Could not create the account: {exc}", "err")
                return redirect(url_for("main.setup"))
            webdb.ensure_profile(settings.webapp_db, user_id, username, is_admin=True)
            flash(f"Created '{username}' as the administrator. It works in the game "
                  f"client too.", "ok")
            return redirect(url_for("main.login"))

    return render_template("setup.html", existing=existing,
                           login_db=settings.login_db,
                           login_db_exists=settings.login_db.exists())


# ════════════════════════════════════════════════════════════════════════════
#  Session
# ════════════════════════════════════════════════════════════════════════════
@main.route("/login", methods=["GET", "POST"])
def login():
    settings = _settings()
    if request.method == "POST":
        user = auth.authenticate(settings.login_db,
                                 request.form.get("username", ""),
                                 request.form.get("password", ""))
        if not user:
            flash("Incorrect username or password.", "err")
            return redirect(url_for("main.login"))
        # First sign-in of an account that predates the web app: give it a
        # profile, never admin (admin is granted from the dashboard).
        webdb.ensure_profile(settings.webapp_db, user["id"], user["username"])
        session.clear()
        session["user_id"] = user["id"]
        session.permanent = True
        nxt = request.form.get("next") or request.args.get("next") or ""
        return redirect(nxt if nxt.startswith("/") else url_for("main.profile"))
    return render_template("login.html", next=request.args.get("next", ""))


@main.route("/register", methods=["GET", "POST"])
def register():
    """Self-registration, gated by Kawari's own login.enable_registration."""
    settings = _settings()
    cfg = settings.server_config()
    if not bool(cfg.value("login", "enable_registration", True)):
        flash("Registration is closed on this server. Ask an administrator for an "
              "account.", "info")
        return redirect(url_for("main.login"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if password != (request.form.get("confirm") or ""):
            flash("The passwords do not match.", "err")
            return redirect(url_for("main.register"))
        from kawari_core.core import inspect_game_path
        info = inspect_game_path(settings.shell.game_path)
        try:
            user_id, _sa = dbtools.create_account(
                settings.login_db, username, password,
                info["max_ex"] if info["repos"] else 5)
        except Exception as exc:                        # noqa: BLE001
            flash(str(exc), "err")
            return redirect(url_for("main.register"))
        webdb.ensure_profile(settings.webapp_db, user_id, username)
        session.clear()
        session["user_id"] = user_id
        flash(f"Welcome, {username}. This account works in the game client too.", "ok")
        return redirect(url_for("main.profile"))
    return render_template("register.html")


@main.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("main.home"))


# ════════════════════════════════════════════════════════════════════════════
#  Public pages
# ════════════════════════════════════════════════════════════════════════════
@main.route("/")
def home():
    settings = _settings()
    podman = _podman()
    return render_template(
        "home.html",
        stats=stats.server_stats(settings, podman),
        info=stats.connection_info(settings),
        profiles=webdb.all_profiles(settings.webapp_db)[:12],
    )


@main.route("/connect")
def connect():
    settings = _settings()
    return render_template("connect.html", info=stats.connection_info(settings),
                           stats=stats.server_stats(settings, _podman()))


@main.route("/profile")
@main.route("/profile/<username>")
def profile(username: str | None = None):
    settings = _settings()
    if username is None:
        if not getattr(g, "user", None):
            return redirect(url_for("main.login"))
        prof = g.user
    else:
        prof = webdb.profile_by_username(settings.webapp_db, username)
        if not prof:
            abort(404)

    viewer = getattr(g, "user", None)
    is_own = bool(viewer and viewer["user_id"] == prof["user_id"])
    characters = auth.characters_for(settings.world_db, settings.login_db,
                                     prof["user_id"])
    public = webdb.public_characters(settings.webapp_db, prof["user_id"])
    visible = []
    for ch in characters:
        ch["is_public"] = ch["content_id"] in public
        if is_own or ch["is_public"]:
            visible.append(ch)
    # Tabs are a query parameter rather than client-side state, so a link to
    # someone's characters stays a link to their characters.
    tab = request.args.get("tab", "posts")
    if tab not in ("posts", "characters"):
        tab = "posts"
    # Only pay for character stats when that tab is actually being shown.
    details = ({c["content_id"]: stats.character_detail(settings, c["content_id"])
                for c in visible} if tab == "characters" else {})
    return render_template("profile.html", profile=prof, is_own=is_own,
                           characters=visible, details=details,
                           hidden_count=len(characters) - len(visible), tab=tab,
                           posts=webdb.posts_by(settings.webapp_db, prof["user_id"]),
                           post_max=webdb.POST_MAX)


@main.route("/profile/edit", methods=["POST"])
@auth.login_required
def edit_profile():
    settings = _settings()
    fields = {
        "display_name": (request.form.get("display_name") or "").strip()[:60],
        "bio": (request.form.get("bio") or "").strip()[:2000],
    }
    for key in ("avatar", "banner"):
        stored = _save_upload(settings, request.files.get(key))
        if stored:
            fields[key] = stored
    webdb.update_profile(settings.webapp_db, g.user["user_id"], **fields)
    flash("Profile updated.", "ok")
    return redirect(url_for("main.profile"))


@main.route("/profile/character/<int:content_id>/visibility", methods=["POST"])
@auth.login_required
def character_visibility(content_id: int):
    settings = _settings()
    owned = {c["content_id"] for c in auth.characters_for(
        settings.world_db, settings.login_db, g.user["user_id"])}
    if content_id not in owned:
        abort(403)
    public = request.form.get("public") == "1"
    webdb.set_character_public(settings.webapp_db, content_id, g.user["user_id"], public)
    flash("Character is now visible to everyone." if public
          else "Character is hidden from other players.", "ok")
    return redirect(url_for("main.profile"))


@main.route("/uploads/<path:name>")
def static_upload(name: str):
    settings = _settings()
    return send_from_directory(settings.uploads, name)


@main.route("/favicon.ico")
def favicon():
    """Emoji favicon rendered as an SVG, so branding needs no file upload."""
    brand = webdb.branding(_settings().webapp_db)
    emoji = brand.get("favicon_emoji") or "\U0001F30D"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           f'<text y="52" font-size="52">{emoji}</text></svg>')
    return svg, 200, {"Content-Type": "image/svg+xml"}


# ── helpers ─────────────────────────────────────────────────────────────────
def _settings() -> Settings:
    from flask import current_app
    return current_app.config["SETTINGS"]


def _podman() -> Podman:
    from flask import current_app
    return current_app.config["PODMAN"]


def _save_upload(settings: Settings, file_storage) -> str:
    """Store an uploaded image under a generated name. Returns the stored name."""
    if not file_storage or not file_storage.filename:
        return ""
    suffix = Path(file_storage.filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE:
        flash(f"Unsupported image type '{suffix}'.", "err")
        return ""
    data = file_storage.read()
    if len(data) > MAX_IMAGE_BYTES:
        flash("Image is larger than 4 MB.", "err")
        return ""
    # Never trust the client filename: generate our own.
    name = f"{secrets.token_hex(8)}{suffix}"
    settings.uploads.mkdir(parents=True, exist_ok=True)
    (settings.uploads / name).write_bytes(data)
    return name
