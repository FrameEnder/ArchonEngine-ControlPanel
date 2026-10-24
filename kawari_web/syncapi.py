"""Sync API for the Kawari Companion plugin.

A token-authenticated endpoint that accepts an Auracite-format character payload
and applies it to the character of the same name on the token's own account.

The safety rules all live here rather than in the plugin, because the plugin runs
on someone else's machine and cannot be trusted to enforce anything:

* a token identifies ONE account, and only characters on that account can be
  written - a token cannot touch anyone else's character;
* a character that is logged in on a running server is never written, since the
  world server rewrites it on logout and would undo the sync;
* a snapshot is taken before every applied sync, so any sync is undoable;
* uploads are rate limited per account, and the payload is size capped;
* nothing is ever created - if no character matches the name, the sync is
  refused rather than inventing one.
"""

from __future__ import annotations

import io
import json
import secrets
import time
import zipfile

from flask import Blueprint, current_app, g, jsonify, redirect, request, url_for, flash

from kawari_core import auracite, chardata

from . import auth, retention, webdb

api = Blueprint("api", __name__)

MIN_SECONDS_BETWEEN_SYNCS = 30
MAX_PAYLOAD_BYTES = 6 * 1024 * 1024

# What a sync is allowed to touch. Deliberately excludes "name" (renaming from a
# background job would be alarming) and "appearance" is opt-in, since a running
# client writes it rarely and it is the one field a bad payload could mangle.
DEFAULT_SECTIONS = ["classes", "unlocks", "travel", "content", "quests", "profile",
                    "inventory", "position", "playtime"]
ALLOWED_SECTIONS = set(DEFAULT_SECTIONS) | {"appearance", "companion"}


def _settings():
    return current_app.config["SETTINGS"]


# ── tokens ──────────────────────────────────────────────────────────────────
def token_for(db, user_id: int) -> str:
    return webdb.get_setting(db, f"sync_token:{user_id}", "")


def issue_token(db, user_id: int) -> str:
    old = token_for(db, user_id)
    if old:
        webdb.set_setting(db, f"sync_owner:{old}", "")
    token = secrets.token_urlsafe(32)
    webdb.set_setting(db, f"sync_token:{user_id}", token)
    webdb.set_setting(db, f"sync_owner:{token}", str(user_id))
    return token


def revoke_token(db, user_id: int) -> None:
    old = token_for(db, user_id)
    if old:
        webdb.set_setting(db, f"sync_owner:{old}", "")
    webdb.set_setting(db, f"sync_token:{user_id}", "")


def owner_of(db, token: str) -> int | None:
    if not token:
        return None
    owner = webdb.get_setting(db, f"sync_owner:{token}", "")
    return int(owner) if owner.isdigit() else None


def sync_sections(db, user_id: int) -> set:
    raw = webdb.get_setting(db, f"sync_sections:{user_id}", "")
    try:
        chosen = set(json.loads(raw)) if raw else set(DEFAULT_SECTIONS)
    except ValueError:
        chosen = set(DEFAULT_SECTIONS)
    return chosen & ALLOWED_SECTIONS


def set_sync_sections(db, user_id: int, sections: set) -> None:
    webdb.set_setting(db, f"sync_sections:{user_id}",
                      json.dumps(sorted(sections & ALLOWED_SECTIONS)))


def last_sync(db, user_id: int) -> dict:
    raw = webdb.get_setting(db, f"sync_last:{user_id}", "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _record(db, user_id: int, **fields) -> None:
    fields["at"] = time.strftime("%Y-%m-%d %H:%M")
    fields["epoch"] = time.time()
    webdb.set_setting(db, f"sync_last:{user_id}", json.dumps(fields))


# ── endpoints ───────────────────────────────────────────────────────────────
@api.route("/api/sync/ping")
def ping():
    """Lets the plugin verify its URL and token without sending any data."""
    settings = _settings()
    user_id = owner_of(settings.webapp_db, _bearer())
    if user_id is None:
        return jsonify({"ok": False, "error": "unknown or missing token"}), 401
    profile = webdb.profile(settings.webapp_db, user_id)
    characters = auth.characters_for(settings.world_db, settings.login_db, user_id)
    return jsonify({
        "ok": True,
        "account": profile["username"] if profile else str(user_id),
        "characters": [c["name"] for c in characters],
        "sections": sorted(sync_sections(settings.webapp_db, user_id)),
        "min_interval_seconds": MIN_SECONDS_BETWEEN_SYNCS,
        "server": "kawari-web",
    })


@api.route("/api/sync", methods=["POST"])
def sync():
    settings = _settings()
    db = settings.webapp_db
    user_id = owner_of(db, _bearer())
    if user_id is None:
        return jsonify({"ok": False, "error": "unknown or missing token"}), 401

    previous = last_sync(db, user_id)
    waited = time.time() - float(previous.get("epoch", 0) or 0)
    if waited < MIN_SECONDS_BETWEEN_SYNCS:
        return jsonify({"ok": False, "error": "too soon",
                        "retry_after": round(MIN_SECONDS_BETWEEN_SYNCS - waited)}), 429

    payload, error = _read_payload()
    if error:
        _record(db, user_id, ok=False, error=error)
        return jsonify({"ok": False, "error": error}), 400

    try:
        dump = auracite.Dump(path=settings.web_dir / "sync.json", data=payload)
        name = dump.name
    except Exception as exc:                            # noqa: BLE001
        _record(db, user_id, ok=False, error=str(exc))
        return jsonify({"ok": False, "error": f"unreadable payload: {exc}"}), 400

    owned = {c["name"]: c["content_id"] for c in
             auth.characters_for(settings.world_db, settings.login_db, user_id)}
    if name not in owned:
        message = (f"no character called {name!r} on this account "
                   f"(have: {', '.join(sorted(owned)) or 'none'})")
        _record(db, user_id, ok=False, error=message, character=name)
        return jsonify({"ok": False, "error": message}), 404
    content_id = owned[name]

    podman = current_app.config["PODMAN"]
    if chardata.is_online(settings.world_db, content_id):
        running = (podman.mode != "none"
                   and podman.inspect(settings.shell.container).get("running"))
        if running:
            message = ("character is logged in on a running server; the world "
                       "server would overwrite the sync on logout")
            _record(db, user_id, ok=False, error=message, character=name)
            return jsonify({"ok": False, "error": message, "retry_after": 300}), 409

    sections = sync_sections(db, user_id)
    try:
        chardata.snapshot(settings.data_dir, settings.world_db, content_id, "pre-sync")
        retention.tidy_snapshots(settings, content_id)
        character = chardata.load(settings.world_db, content_id)
        changes, notes = auracite.build_changes(character, dump, sections)
        auracite.apply_changes(character, changes)
        written = chardata.save(settings.world_db, character)
    except Exception as exc:                            # noqa: BLE001
        _record(db, user_id, ok=False, error=str(exc), character=name)
        return jsonify({"ok": False, "error": f"could not apply: {exc}"}), 500

    columns = sum(len(cols) for cols in written.values())
    _record(db, user_id, ok=True, character=name, columns=columns,
            tables=sorted(written), notes=notes[:4])
    return jsonify({"ok": True, "character": name, "columns": columns,
                    "tables": sorted(written), "notes": notes[:4],
                    "sections": sorted(sections)})


def _bearer() -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("X-Kawari-Token", "")
            or request.args.get("token", "")).strip()


def _read_payload():
    """Accept raw character.json or an Auracite zip, and nothing enormous."""
    raw = request.get_data(cache=False)
    if not raw:
        return None, "empty body"
    if len(raw) > MAX_PAYLOAD_BYTES:
        return None, f"payload larger than {MAX_PAYLOAD_BYTES // 1048576} MB"

    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                member = next((n for n in zf.namelist()
                               if n.endswith("character.json")), None)
                if member is None:
                    return None, "zip has no character.json"
                raw = zf.read(member)
        except zipfile.BadZipFile as exc:
            return None, f"unreadable zip: {exc}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(payload, dict) or "name" not in payload:
        return None, "payload is not an Auracite character document"
    return payload, ""


# ── settings page actions ───────────────────────────────────────────────────
@api.route("/settings/sync/token", methods=["POST"])
@auth.login_required
def new_token():
    issue_token(_settings().webapp_db, g.user["user_id"])
    flash("New sync token issued. The old one stopped working immediately.", "ok")
    return redirect(url_for("social.settings_page", page="sync"))


@api.route("/settings/sync/revoke", methods=["POST"])
@auth.login_required
def drop_token():
    revoke_token(_settings().webapp_db, g.user["user_id"])
    flash("Sync token revoked.", "ok")
    return redirect(url_for("social.settings_page", page="sync"))


@api.route("/settings/sync/sections", methods=["POST"])
@auth.login_required
def save_sections():
    set_sync_sections(_settings().webapp_db, g.user["user_id"],
                      set(request.form.getlist("sections")))
    flash("Sync settings saved.", "ok")
    return redirect(url_for("social.settings_page", page="sync"))
