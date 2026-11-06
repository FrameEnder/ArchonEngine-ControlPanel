"""The web app's own SQLite database.

Kept strictly separate from Kawari's world.db and login.db. The web app reads
those two and never adds columns to them, so upgrading Kawari (which regenerates
its Diesel migrations) can never collide with anything here.

Accounts are not duplicated: identity lives in Kawari's `user` table and a
profile row here just hangs extra web-only data off that user id.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT    NOT NULL,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    display_name TEXT    NOT NULL DEFAULT '',
    bio          TEXT    NOT NULL DEFAULT '',
    avatar       TEXT    NOT NULL DEFAULT '',
    banner       TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT ''
);

-- Characters are private by DEFAULT: a row exists only once the owner has
-- explicitly made one public, so absence means hidden.
CREATE TABLE IF NOT EXISTS character_public (
    content_id INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── social ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    edited_at  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS post_user ON post(user_id, id DESC);

CREATE TABLE IF NOT EXISTS post_image (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id  INTEGER NOT NULL,
    filename TEXT    NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS post_image_post ON post_image(post_id, position);

-- parent_id is null for a top-level comment, otherwise the comment replied to.
CREATE TABLE IF NOT EXISTS comment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    parent_id  INTEGER,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    edited_at  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS comment_post ON comment(post_id, id);

CREATE TABLE IF NOT EXISTS notification (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    link       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    read_at    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS notification_user ON notification(user_id, id DESC);

CREATE TABLE IF NOT EXISTS notification_pref (
    user_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, kind)
);

-- Every announcement an administrator sent (the notifications themselves are
-- per account and can be cleared, so this is the record of what went out).
CREATE TABLE IF NOT EXISTS announcement (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    sent_by    INTEGER NOT NULL DEFAULT 0,
    recipients INTEGER NOT NULL DEFAULT 0,
    accounts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
"""

# Notification kinds, with the label shown in settings.
NOTIFICATION_KINDS = [
    ("comment", "Comments on my posts"),
    ("reply", "Replies to my comments"),
    ("server", "Server starts and stops"),
    ("announcement", "Announcements from administrators"),
    ("shop", "Shop deliveries"),
]

POST_MAX = 2000
COMMENT_MAX = 1000

BRANDING_DEFAULTS = {
    "site_name": "ArchonEngine ControlPanel",
    "tagline": "A private Final Fantasy XIV server",
    # Empty: the favicon is the logo (the uploaded one, or ArchonEngine's mark).
    "favicon_emoji": "",
    # The ArchonEngine mark's hue (#361B3D), light enough for buttons on both themes.
    "accent": "#b170c2",
    "theme": "dark",
    "logo": "",
    "footer": "",
    "links_json": json.dumps([
        {"label": "Kawari upstream", "url": "https://github.com/redstrate/Kawari"},
        {"label": "Kawari documentation", "url": "https://docs.xiv.zone/kawari/"},
        {"label": "Astra launcher", "url": "https://xiv.zone/astra/"},
        {"label": "Auracite (character backups)", "url": "https://auracite.xiv.zone/"},
    ]),
}


# The defaults before 2.26.0, when the panel was Kawari Web. The Branding page saves every field, so a panel
# saved with them has them stored: init() changes them to today's defaults once (branding_defaults = 2), and
# after that they're Bob's to set again if he wants them.
OLD_BRANDING_DEFAULTS = {
    "site_name": "Kawari",
    "favicon_emoji": "\U0001F30D",
    "accent": "#5aa9f0",
}
BRANDING_DEFAULTS_VERSION = "2"


def connect(path: Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate_branding(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_branding(conn: sqlite3.Connection) -> None:
    """Stored branding still at Kawari Web's defaults becomes ArchonEngine ControlPanel's, once."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'branding_defaults'").fetchone()
    if row and row["value"] >= BRANDING_DEFAULTS_VERSION:
        return
    for key, old in OLD_BRANDING_DEFAULTS.items():
        conn.execute("UPDATE setting SET value = ? WHERE key = ? AND lower(value) = lower(?)",
                     (BRANDING_DEFAULTS[key], key, old))
    conn.execute("INSERT INTO setting (key, value) VALUES ('branding_defaults', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (BRANDING_DEFAULTS_VERSION,))


# ── settings / branding ─────────────────────────────────────────────────────
def get_setting(path: Path, key: str, default: str = "") -> str:
    conn = connect(path)
    try:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row["value"] if row else default


def set_setting(path: Path, key: str, value: str) -> None:
    conn = connect(path)
    try:
        conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def secret_key(path: Path) -> str:
    """Stable session signing key, generated once and persisted."""
    key = get_setting(path, "secret_key")
    if not key:
        key = secrets.token_hex(32)
        set_setting(path, "secret_key", key)
    return key


def branding(path: Path) -> dict:
    out = dict(BRANDING_DEFAULTS)
    conn = connect(path)
    try:
        for row in conn.execute("SELECT key, value FROM setting"):
            if row["key"] in BRANDING_DEFAULTS:
                out[row["key"]] = row["value"]
    finally:
        conn.close()
    try:
        out["links"] = json.loads(out.get("links_json") or "[]")
    except ValueError:
        out["links"] = []
    return out


def set_branding(path: Path, values: dict) -> None:
    conn = connect(path)
    try:
        for key, value in values.items():
            if key not in BRANDING_DEFAULTS:
                continue
            conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, str(value)))
        conn.commit()
    finally:
        conn.close()


# ── profiles ────────────────────────────────────────────────────────────────
def profile(path: Path, user_id: int) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def profile_by_username(path: Path, username: str) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM profile WHERE username = ?",
                           (username,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def all_profiles(path: Path) -> list:
    conn = connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM profile ORDER BY is_admin DESC, username")]
    finally:
        conn.close()


def ensure_profile(path: Path, user_id: int, username: str,
                   is_admin: bool = False) -> dict:
    """Create the profile row for a Kawari account the first time it signs in."""
    existing = profile(path, user_id)
    if existing:
        if existing["username"] != username:          # renamed in login.db
            conn = connect(path)
            try:
                conn.execute("UPDATE profile SET username = ? WHERE user_id = ?",
                             (username, user_id))
                conn.commit()
            finally:
                conn.close()
            existing["username"] = username
        return existing
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO profile (user_id, username, is_admin, display_name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, 1 if is_admin else 0, username,
             time.strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    finally:
        conn.close()
    return profile(path, user_id)


def update_profile(path: Path, user_id: int, **fields) -> None:
    allowed = {"display_name", "bio", "avatar", "banner", "is_admin"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    conn = connect(path)
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE profile SET {sets} WHERE user_id = ?",
                     (*fields.values(), user_id))
        conn.commit()
    finally:
        conn.close()


def admin_count(path: Path) -> int:
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM profile WHERE is_admin = 1").fetchone()[0]
    finally:
        conn.close()


def has_any_profile(path: Path) -> bool:
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] > 0
    finally:
        conn.close()


# ── character visibility ────────────────────────────────────────────────────
def public_characters(path: Path, user_id: int | None = None) -> set:
    conn = connect(path)
    try:
        if user_id is None:
            rows = conn.execute("SELECT content_id FROM character_public")
        else:
            rows = conn.execute(
                "SELECT content_id FROM character_public WHERE user_id = ?", (user_id,))
        return {r["content_id"] for r in rows}
    finally:
        conn.close()


def set_character_public(path: Path, content_id: int, user_id: int,
                         public: bool) -> None:
    conn = connect(path)
    try:
        if public:
            conn.execute("INSERT OR REPLACE INTO character_public (content_id, user_id) "
                         "VALUES (?, ?)", (content_id, user_id))
        else:
            conn.execute("DELETE FROM character_public WHERE content_id = ?", (content_id,))
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
#  Posts and comments
# ════════════════════════════════════════════════════════════════════════════
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def create_post(path: Path, user_id: int, body: str, images: list) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO post (user_id, body, created_at) VALUES (?, ?, ?)",
            (user_id, body.strip(), _now()))
        post_id = cur.lastrowid
        for position, filename in enumerate(images):
            conn.execute("INSERT INTO post_image (post_id, filename, position) "
                         "VALUES (?, ?, ?)", (post_id, filename, position))
        conn.commit()
        return post_id
    finally:
        conn.close()


def _attach(conn, rows: list) -> list:
    """Add images and comment counts to post rows."""
    out = []
    for row in rows:
        post = dict(row)
        post["images"] = [r["filename"] for r in conn.execute(
            "SELECT filename FROM post_image WHERE post_id = ? ORDER BY position",
            (post["id"],))]
        post["comment_count"] = conn.execute(
            "SELECT COUNT(*) FROM comment WHERE post_id = ?", (post["id"],)).fetchone()[0]
        out.append(post)
    return out


POST_SELECT = """
    SELECT p.*, pr.username, pr.display_name, pr.avatar
    FROM post p LEFT JOIN profile pr ON pr.user_id = p.user_id
"""


def posts_by(path: Path, user_id: int, limit: int = 50) -> list:
    conn = connect(path)
    try:
        rows = conn.execute(POST_SELECT + " WHERE p.user_id = ? ORDER BY p.id DESC LIMIT ?",
                            (user_id, limit)).fetchall()
        return _attach(conn, rows)
    finally:
        conn.close()


def recent_posts(path: Path, limit: int = 20) -> list:
    conn = connect(path)
    try:
        rows = conn.execute(POST_SELECT + " ORDER BY p.id DESC LIMIT ?", (limit,)).fetchall()
        return _attach(conn, rows)
    finally:
        conn.close()


def get_post(path: Path, post_id: int) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute(POST_SELECT + " WHERE p.id = ?", (post_id,)).fetchone()
        if not row:
            return None
        return _attach(conn, [row])[0]
    finally:
        conn.close()


def update_post(path: Path, post_id: int, body: str) -> None:
    conn = connect(path)
    try:
        conn.execute("UPDATE post SET body = ?, edited_at = ? WHERE id = ?",
                     (body.strip(), _now(), post_id))
        conn.commit()
    finally:
        conn.close()


def delete_post(path: Path, post_id: int) -> list:
    """Delete a post with its images and comments. Returns removed image files."""
    conn = connect(path)
    try:
        files = [r["filename"] for r in conn.execute(
            "SELECT filename FROM post_image WHERE post_id = ?", (post_id,))]
        conn.execute("DELETE FROM post_image WHERE post_id = ?", (post_id,))
        conn.execute("DELETE FROM comment WHERE post_id = ?", (post_id,))
        conn.execute("DELETE FROM post WHERE id = ?", (post_id,))
        conn.commit()
        return files
    finally:
        conn.close()


def add_comment(path: Path, post_id: int, user_id: int, body: str,
                parent_id: int | None = None) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO comment (post_id, user_id, parent_id, body, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (post_id, user_id, parent_id, body.strip(), _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def comments_for(path: Path, post_id: int) -> list:
    """Top-level comments, each with its replies nested one level."""
    conn = connect(path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.*, pr.username, pr.display_name, pr.avatar "
            "FROM comment c LEFT JOIN profile pr ON pr.user_id = c.user_id "
            "WHERE c.post_id = ? ORDER BY c.id", (post_id,))]
    finally:
        conn.close()
    by_id = {r["id"]: dict(r, replies=[]) for r in rows}
    roots = []
    for row in rows:
        node = by_id[row["id"]]
        parent = by_id.get(row["parent_id"]) if row["parent_id"] else None
        (parent["replies"] if parent else roots).append(node)
    return roots


def get_comment(path: Path, comment_id: int) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM comment WHERE id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_comment(path: Path, comment_id: int, body: str) -> None:
    conn = connect(path)
    try:
        conn.execute("UPDATE comment SET body = ?, edited_at = ? WHERE id = ?",
                     (body.strip(), _now(), comment_id))
        conn.commit()
    finally:
        conn.close()


def delete_comment(path: Path, comment_id: int) -> None:
    """Delete a comment and everything replying to it."""
    conn = connect(path)
    try:
        doomed, frontier = [comment_id], [comment_id]
        while frontier:
            children = [r["id"] for r in conn.execute(
                f"SELECT id FROM comment WHERE parent_id IN "
                f"({','.join('?' * len(frontier))})", frontier)]
            doomed.extend(children)
            frontier = children
        conn.execute(f"DELETE FROM comment WHERE id IN ({','.join('?' * len(doomed))})",
                     doomed)
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
#  Notifications
# ════════════════════════════════════════════════════════════════════════════
def notify(path: Path, user_id: int, kind: str, title: str, body: str = "",
           link: str = "") -> bool:
    """Deliver a notification unless the recipient has turned that kind off."""
    if not notification_enabled(path, user_id, kind):
        return False
    conn = connect(path)
    try:
        conn.execute("INSERT INTO notification (user_id, kind, title, body, link, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (user_id, kind, title, body, link, _now()))
        conn.commit()
        return True
    finally:
        conn.close()


def notify_all(path: Path, kind: str, title: str, body: str = "", link: str = "",
               exclude: int | None = None) -> int:
    sent = 0
    for profile_row in all_profiles(path):
        if exclude is not None and profile_row["user_id"] == exclude:
            continue
        if notify(path, profile_row["user_id"], kind, title, body, link):
            sent += 1
    return sent


def record_announcement(path: Path, title: str, body: str, sent_by: int,
                        recipients: int, accounts: int) -> int:
    conn = connect(path)
    try:
        cur = conn.execute("INSERT INTO announcement (title, body, sent_by, recipients, accounts, "
                           "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (title, body, sent_by, recipients, accounts, _now()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def announcements(path: Path, limit: int = 50) -> list:
    """Sent announcements, newest first, with the sender's names."""
    conn = connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, p.username AS sender, p.display_name AS sender_display "
            "FROM announcement a LEFT JOIN profile p ON p.user_id = a.sent_by "
            "ORDER BY a.id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def notifications_for(path: Path, user_id: int, limit: int = 50,
                      unread_only: bool = False) -> list:
    conn = connect(path)
    try:
        sql = "SELECT * FROM notification WHERE user_id = ?"
        if unread_only:
            sql += " AND read_at = ''"
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (user_id, limit))]
    finally:
        conn.close()


def unread_count(path: Path, user_id: int) -> int:
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM notification WHERE user_id = ? "
                            "AND read_at = ''", (user_id,)).fetchone()[0]
    finally:
        conn.close()


def mark_read(path: Path, user_id: int, notification_id: int | None = None) -> None:
    conn = connect(path)
    try:
        if notification_id is None:
            conn.execute("UPDATE notification SET read_at = ? WHERE user_id = ? "
                         "AND read_at = ''", (_now(), user_id))
        else:
            conn.execute("UPDATE notification SET read_at = ? WHERE id = ? AND user_id = ?",
                         (_now(), notification_id, user_id))
        conn.commit()
    finally:
        conn.close()


def clear_notifications(path: Path, user_id: int) -> None:
    conn = connect(path)
    try:
        conn.execute("DELETE FROM notification WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def notification_enabled(path: Path, user_id: int, kind: str) -> bool:
    conn = connect(path)
    try:
        row = conn.execute("SELECT enabled FROM notification_pref WHERE user_id = ? "
                           "AND kind = ?", (user_id, kind)).fetchone()
    finally:
        conn.close()
    return True if row is None else bool(row["enabled"])      # opt-out, not opt-in


def notification_prefs(path: Path, user_id: int) -> dict:
    return {kind: notification_enabled(path, user_id, kind)
            for kind, _label in NOTIFICATION_KINDS}


def set_notification_prefs(path: Path, user_id: int, enabled_kinds: set) -> None:
    conn = connect(path)
    try:
        for kind, _label in NOTIFICATION_KINDS:
            conn.execute(
                "INSERT INTO notification_pref (user_id, kind, enabled) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, kind) DO UPDATE SET enabled = excluded.enabled",
                (user_id, kind, 1 if kind in enabled_kinds else 0))
        conn.commit()
    finally:
        conn.close()
