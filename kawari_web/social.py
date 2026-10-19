"""Social features and per-user settings.

Posts and comments live only in the web app's own database; Kawari's world.db and
login.db are never touched by them. The single exception is the password change
in Security, which updates the Kawari account itself - there is only one account,
so changing it here changes what you type into the launcher.
"""

from __future__ import annotations

import time

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request,
    url_for,
)

from kawari_core import auracite, dbtools

from . import auth, webdb

social = Blueprint("social", __name__)


def _settings():
    return current_app.config["SETTINGS"]


def _save_images(settings, files, limit: int = 4) -> list:
    from .app import _save_upload
    stored = []
    for storage in files[:limit]:
        name = _save_upload(settings, storage)
        if name:
            stored.append(name)
    return stored


def _display(profile: dict) -> str:
    return profile.get("display_name") or profile.get("username") or "someone"


# ════════════════════════════════════════════════════════════════════════════
#  Feed
# ════════════════════════════════════════════════════════════════════════════
FEED_FILTERS = [("all", "All"), ("images", "Screenshots")]


@social.route("/feed")
def feed():
    settings = _settings()
    show = request.args.get("show", "all")
    if show not in dict(FEED_FILTERS):
        show = "all"
    if show == "images":
        # Posts carry their images after loading, so filter a wider window.
        posts = [p for p in webdb.recent_posts(settings.webapp_db, 200) if p["images"]][:30]
    else:
        posts = webdb.recent_posts(settings.webapp_db, 30)
    return render_template("feed.html", posts=posts, show=show, filters=FEED_FILTERS,
                           online=_online_profiles(settings),
                           announcements=webdb.announcements(settings.webapp_db, 3),
                           post_max=webdb.POST_MAX)


def _online_profiles(settings) -> list | None:
    """Players with a character in game, for the feed's sidebar; None when the
    server isn't known to be running. is_online can be left set by a crash, so it
    is only believed while the container runs (the same rule as Home and the shop)."""
    from . import stats
    from .app import _users_in_game
    if not stats.server_up(settings, current_app.config["PODMAN"]):
        return None
    ids = _users_in_game(settings)
    return [p for p in webdb.all_profiles(settings.webapp_db) if p["user_id"] in ids]


@social.route("/post", methods=["POST"])
@auth.login_required
def create_post():
    settings = _settings()
    body = (request.form.get("body") or "").strip()[:webdb.POST_MAX]
    images = _save_images(settings, request.files.getlist("images"))
    if not body and not images:
        flash("Write something or attach an image.", "err")
        return redirect(request.form.get("back") or url_for("main.profile"))
    post_id = webdb.create_post(settings.webapp_db, g.user["user_id"], body, images)
    flash("Posted.", "ok")
    return redirect(url_for("social.post_detail", post_id=post_id))


@social.route("/post/<int:post_id>")
def post_detail(post_id: int):
    settings = _settings()
    post = webdb.get_post(settings.webapp_db, post_id)
    if not post:
        abort(404)
    return render_template("post.html", post=post,
                           comments=webdb.comments_for(settings.webapp_db, post_id),
                           comment_max=webdb.COMMENT_MAX)


@social.route("/post/<int:post_id>/edit", methods=["POST"])
@auth.login_required
def edit_post(post_id: int):
    settings = _settings()
    post = webdb.get_post(settings.webapp_db, post_id)
    if not post:
        abort(404)
    if post["user_id"] != g.user["user_id"]:
        abort(403)
    webdb.update_post(settings.webapp_db, post_id,
                      (request.form.get("body") or "")[:webdb.POST_MAX])
    flash("Post updated.", "ok")
    return redirect(url_for("social.post_detail", post_id=post_id))


@social.route("/post/<int:post_id>/delete", methods=["POST"])
@auth.login_required
def delete_post(post_id: int):
    settings = _settings()
    post = webdb.get_post(settings.webapp_db, post_id)
    if not post:
        abort(404)
    # An administrator can remove anyone's post; everyone else only their own.
    if post["user_id"] != g.user["user_id"] and not g.user.get("is_admin"):
        abort(403)
    for filename in webdb.delete_post(settings.webapp_db, post_id):
        (settings.uploads / filename).unlink(missing_ok=True)
    flash("Post deleted.", "ok")
    return redirect(url_for("main.profile"))


# ════════════════════════════════════════════════════════════════════════════
#  Comments
# ════════════════════════════════════════════════════════════════════════════
@social.route("/post/<int:post_id>/comment", methods=["POST"])
@auth.login_required
def add_comment(post_id: int):
    settings = _settings()
    post = webdb.get_post(settings.webapp_db, post_id)
    if not post:
        abort(404)
    body = (request.form.get("body") or "").strip()[:webdb.COMMENT_MAX]
    if not body:
        flash("Write something first.", "err")
        return redirect(url_for("social.post_detail", post_id=post_id))

    parent_id = request.form.get("parent_id")
    parent = None
    if parent_id:
        parent = webdb.get_comment(settings.webapp_db, int(parent_id))
        if not parent or parent["post_id"] != post_id:
            abort(404)

    webdb.add_comment(settings.webapp_db, post_id, g.user["user_id"], body,
                      parent["id"] if parent else None)
    link = url_for("social.post_detail", post_id=post_id)
    me = _display(g.user)

    if parent:
        # Replying: tell the comment's author, not the post's.
        if parent["user_id"] != g.user["user_id"]:
            webdb.notify(settings.webapp_db, parent["user_id"], "reply",
                         f"{me} replied to your comment", body[:140], link)
    elif post["user_id"] != g.user["user_id"]:
        webdb.notify(settings.webapp_db, post["user_id"], "comment",
                     f"{me} commented on your post", body[:140], link)
    flash("Comment added.", "ok")
    return redirect(link)


@social.route("/comment/<int:comment_id>/edit", methods=["POST"])
@auth.login_required
def edit_comment(comment_id: int):
    settings = _settings()
    comment = webdb.get_comment(settings.webapp_db, comment_id)
    if not comment:
        abort(404)
    if comment["user_id"] != g.user["user_id"]:
        abort(403)
    body = (request.form.get("body") or "").strip()[:webdb.COMMENT_MAX]
    if body:
        webdb.update_comment(settings.webapp_db, comment_id, body)
        flash("Comment updated.", "ok")
    return redirect(url_for("social.post_detail", post_id=comment["post_id"]))


@social.route("/comment/<int:comment_id>/delete", methods=["POST"])
@auth.login_required
def delete_comment(comment_id: int):
    settings = _settings()
    comment = webdb.get_comment(settings.webapp_db, comment_id)
    if not comment:
        abort(404)
    if comment["user_id"] != g.user["user_id"] and not g.user.get("is_admin"):
        abort(403)
    webdb.delete_comment(settings.webapp_db, comment_id)
    flash("Comment deleted, along with any replies to it.", "ok")
    return redirect(url_for("social.post_detail", post_id=comment["post_id"]))


# ════════════════════════════════════════════════════════════════════════════
#  Notifications
# ════════════════════════════════════════════════════════════════════════════
@social.route("/notifications")
@auth.login_required
def notifications():
    """The list, optionally narrowed by ?kind= to unread ones or one kind."""
    settings = _settings()
    every = webdb.notifications_for(settings.webapp_db, g.user["user_id"], 100)
    labels = dict(NOTIFICATION_KIND_LABELS)
    kind = request.args.get("kind", "")
    if kind != "unread" and kind not in labels:
        kind = ""
    counts = {"": len(every), "unread": sum(1 for n in every if not n["read_at"])}
    for n in every:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    # Chips: All and Unread always, then only the kinds this list actually has.
    chips = [("", "All"), ("unread", "Unread")] + [
        (key, label) for key, label in NOTIFICATION_KIND_LABELS if counts.get(key)]
    if kind == "unread":
        items = [n for n in every if not n["read_at"]]
    elif kind:
        items = [n for n in every if n["kind"] == kind]
    else:
        items = every
    today = time.strftime("%Y-%m-%d")
    groups = [("Today", [n for n in items if n["created_at"].startswith(today)]),
              ("Earlier", [n for n in items if not n["created_at"].startswith(today)])]
    return render_template(
        "notifications.html", items=items, kind=kind, chips=chips, counts=counts,
        labels=labels, groups=[(label, rows) for label, rows in groups if rows])


# Short names for each notification kind, for the list's chips and tags.
NOTIFICATION_KIND_LABELS = [
    ("announcement", "Announcements"), ("reply", "Replies"), ("comment", "Comments"),
    ("shop", "Shop"), ("server", "Server"),
]


@social.route("/notifications/read", methods=["POST"])
@auth.login_required
def mark_notifications_read():
    settings = _settings()
    one = request.form.get("id")
    webdb.mark_read(settings.webapp_db, g.user["user_id"], int(one) if one else None)
    return redirect(request.form.get("back") or url_for("social.notifications"))


@social.route("/notifications/clear", methods=["POST"])
@auth.login_required
def clear_notifications():
    settings = _settings()
    webdb.clear_notifications(settings.webapp_db, g.user["user_id"])
    flash("Notifications cleared.", "ok")
    return redirect(url_for("social.notifications"))


# ════════════════════════════════════════════════════════════════════════════
#  Settings
# ════════════════════════════════════════════════════════════════════════════
SETTINGS_PAGES = [
    ("profile", "Profile", "Name, bio, picture and banner"),
    ("security", "Password & account", "Password for this site and the game"),
    ("notifications", "Notifications", "What you get told about"),
    ("sync", "Character sync", "Live updates from the companion plugin"),
    ("characters", "Character visibility", "Which characters others can see"),
]


@social.route("/settings")
@social.route("/settings/<page>")
@auth.login_required
def settings_page(page: str = "profile"):
    if page not in dict((key, label) for key, label, _d in SETTINGS_PAGES):
        abort(404)
    settings = _settings()
    context = {"pages": SETTINGS_PAGES, "page": page}
    if page == "notifications":
        context["prefs"] = webdb.notification_prefs(settings.webapp_db,
                                                    g.user["user_id"])
        context["kinds"] = webdb.NOTIFICATION_KINDS
    if page == "sync":
        from . import syncapi
        db = settings.webapp_db
        context["token"] = syncapi.token_for(db, g.user["user_id"])
        context["sections"] = syncapi.sync_sections(db, g.user["user_id"])
        context["all_sections"] = auracite.SECTIONS
        context["allowed"] = syncapi.ALLOWED_SECTIONS
        context["opt_in"] = syncapi.ALLOWED_SECTIONS - set(syncapi.DEFAULT_SECTIONS)
        context["last"] = syncapi.last_sync(db, g.user["user_id"])
        context["base_url"] = request.url_root.rstrip("/")
        context["min_interval"] = syncapi.MIN_SECONDS_BETWEEN_SYNCS
    if page == "characters":
        characters = auth.characters_for(settings.world_db, settings.login_db,
                                         g.user["user_id"])
        public = webdb.public_characters(settings.webapp_db, g.user["user_id"])
        from . import stats
        for character in characters:
            character["is_public"] = character["content_id"] in public
            character["summary"] = stats.character_detail(
                settings, character["content_id"]).get("summary", {})
        context["characters"] = characters
    return render_template("settings.html", **context)


@social.route("/settings/notifications", methods=["POST"])
@auth.login_required
def save_notification_prefs():
    settings = _settings()
    webdb.set_notification_prefs(settings.webapp_db, g.user["user_id"],
                                 set(request.form.getlist("kinds")))
    flash("Notification settings saved.", "ok")
    return redirect(url_for("social.settings_page", page="notifications"))


@social.route("/settings/security", methods=["POST"])
@auth.login_required
def change_password():
    """Change the Kawari account password - the same one the launcher uses."""
    settings = _settings()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    if not auth.authenticate(settings.login_db, g.user["username"], current):
        flash("Your current password is not correct.", "err")
        return redirect(url_for("social.settings_page", page="security"))
    if new != confirm:
        flash("The new passwords do not match.", "err")
        return redirect(url_for("social.settings_page", page="security"))
    if not new:
        flash("The new password cannot be empty.", "err")
        return redirect(url_for("social.settings_page", page="security"))

    ok, error, rows = dbtools.execute(
        settings.login_db, "UPDATE user SET password = ? WHERE id = ?",
        (new, g.user["user_id"]))
    if not ok or rows == 0:
        flash(f"Could not change the password: {error or 'account not found'}", "err")
        return redirect(url_for("social.settings_page", page="security"))
    flash("Password changed. Use the new one here and in the game launcher - they are "
          "the same account.", "ok")
    return redirect(url_for("social.settings_page", page="security"))
