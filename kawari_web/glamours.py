"""Dashboard -> Bot glamours, and the glamour API for the Kawari Glamour Collector (the Chrome extension in the Kawari
repository's tools/glamour-collector).

The party bots wear outfits from glamours.txt in the server's data folder (the folder mounted at /kawari/data, beside
world.db; servers/world/src/server/glamour.rs), which the world server reads again whenever it changes, so a new list
needs no restart. The extension sends its list here instead of the file being copied over by hand, and gets the class
list for its Class dropdown from the game install (kawari-questdump classes -> class-index.json), so no class names
are written into it.

The API takes the same per-account token as the Companion sync (Settings -> Character sync), and only an
administrator's: the list changes what everyone's bots wear.

    GET  /api/glamours/ping     check the URL and token; whether the class list is there
    GET  /api/glamours/classes  the combat classes and jobs, and the role groups
    GET  /api/glamours          the list as it is (text)
    POST /api/glamours          replace it: {"text": "..."} or the text as the body; the old one is kept as a backup
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from flask import flash, jsonify, redirect, render_template, request, url_for

from kawari_core import questlab as Q

from . import auth, webdb
from .admin_base import dashboard, runner, settings
from .questlab import TOOL, _active_build, _run_step, tool_status
from .syncapi import _bearer, api, owner_of

GLAMOUR_FILE = "glamours.txt"
CLASS_INDEX = "class-index.json"
MAX_LIST_BYTES = 2 * 1024 * 1024
KEEP_BACKUPS = 20

# The role groups, by ClassJob's Role (1 tank, 2 melee, 3 ranged, 4 healer) and PrimaryStat (2, dexterity, for the
# physical ranged). These are the names Eorzea Collection's "Intended for" uses, and the server knows them too.
DEXTERITY = 2
ROLE_GROUPS = ["Tanks", "Healers", "Melee DPS", "Physical Ranged DPS", "Caster DPS"]


def _group_of(role: int, primary_stat: int) -> str | None:
    if role == 1:
        return "Tanks"
    if role == 4:
        return "Healers"
    if role == 2:
        return "Melee DPS"
    if role == 3:
        return "Physical Ranged DPS" if primary_stat == DEXTERITY else "Caster DPS"
    return None


# ════════════════════════════════════════════════════════════════════════════
#  The list and the class index
# ════════════════════════════════════════════════════════════════════════════
def list_path() -> Path:
    """glamours.txt in the server's data folder (what the container sees as /kawari/data)."""
    return Path(settings().db_dir) / GLAMOUR_FILE


def _backup_dir() -> Path:
    path = Path(settings().web_dir) / "glamour-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def summary(text: str) -> dict:
    """How many glamours a list holds, by who they're for, and how many are for some classes only."""
    counts = {"glamours": 0, "male": 0, "female": 0, "any": 0, "classes": 0}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("=="):
            counts["glamours"] += 1
            tag = re.search(r"\((male|female)\)\s*$", line, re.I)
            counts[tag.group(1).lower() if tag else "any"] += 1
        elif re.match(r"(class|classes|job|jobs)\s*:", line, re.I):
            counts["classes"] += 1
    return counts


def read_list() -> tuple[str, float | None]:
    path = list_path()
    try:
        return path.read_text(encoding="utf-8"), path.stat().st_mtime
    except OSError:
        return "", None


def write_list(text: str) -> dict:
    """Replace the list, keeping the old one in the web folder's glamour-backups/ (the newest KEEP_BACKUPS)."""
    path = list_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old, _ = read_list()
    if old:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (_backup_dir() / f"glamours-{stamp}.txt").write_text(old, encoding="utf-8")
        for stale in sorted(_backup_dir().glob("glamours-*.txt"))[:-KEEP_BACKUPS]:
            stale.unlink(missing_ok=True)
    # Written beside it and renamed over it, so the server never reads half a file.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".glamours-", suffix=".txt")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text if text.endswith("\n") else text + "\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    return summary(text)


def backups() -> list[dict]:
    return [{"name": p.name, "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
             **summary(p.read_text(encoding="utf-8"))}
            for p in sorted(_backup_dir().glob("glamours-*.txt"), reverse=True)]


def class_index() -> dict | None:
    """class-index.json from kawari-questdump: {"format": 1, "classes": [...]}, or None before it's written."""
    path = Q.dumps_root(settings().data_dir) / CLASS_INDEX
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("classes"), list) else None


def class_list(index: dict) -> dict:
    """What the extension's dropdown shows: the role groups, and each group's classes by name, in the game's order."""
    classes, groups = [], {name: [] for name in ROLE_GROUPS}
    for row in index["classes"]:
        name = (row.get("name_english") or row.get("name") or "").strip()
        if not name:
            continue
        group = _group_of(int(row.get("role") or 0), int(row.get("primary_stat") or 0))
        classes.append({"id": row.get("id"), "name": name, "abbreviation": row.get("abbreviation", ""),
                        "group": group, "parent": row.get("parent")})
        if group:
            groups[group].append(name)
    return {"roles": ROLE_GROUPS, "groups": [{"name": g, "classes": groups[g]} for g in ROLE_GROUPS],
            "classes": classes}


def _has_classes_cmd() -> bool:
    build = _active_build()
    try:
        return build is not None and b"kawari-questdump classes" in (build / TOOL).read_bytes()
    except OSError:
        return False


# ════════════════════════════════════════════════════════════════════════════
#  The API (for the extension)
# ════════════════════════════════════════════════════════════════════════════
def _admin_from_token():
    """The token's owner if they're an administrator, else an error response."""
    db = settings().webapp_db
    user_id = owner_of(db, _bearer())
    if user_id is None:
        return None, (jsonify({"ok": False, "error": "unknown or missing token"}), 401)
    profile = webdb.profile(db, user_id) or {}
    if not profile.get("is_admin"):
        return None, (jsonify({"ok": False, "error": "this token's account isn't an administrator"}), 403)
    return user_id, None


@api.route("/api/glamours/ping")
def glamours_ping():
    _user, error = _admin_from_token()
    if error:
        return error
    text, modified = read_list()
    return jsonify({"ok": True, "server": "kawari-web", "accepts": "glamours/1",
                    "classes": class_index() is not None, "list": summary(text),
                    "modified": modified})


@api.route("/api/glamours/classes")
def glamours_classes():
    _user, error = _admin_from_token()
    if error:
        return error
    index = class_index()
    if index is None:
        return jsonify({"ok": False, "error": "The class list hasn't been read from the game yet: "
                                              "Dashboard -> Bot glamours -> Read the class list from the game."}), 404
    return jsonify({"ok": True, **class_list(index)})


@api.route("/api/glamours", methods=["GET"])
def glamours_get():
    _user, error = _admin_from_token()
    if error:
        return error
    text, modified = read_list()
    return jsonify({"ok": True, "text": text, "modified": modified, "list": summary(text)})


@api.route("/api/glamours", methods=["POST"])
def glamours_post():
    _user, error = _admin_from_token()
    if error:
        return error
    raw = request.get_data(cache=False)
    if len(raw) > MAX_LIST_BYTES:
        return jsonify({"ok": False, "error": "the list is too large"}), 413
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "the list isn't UTF-8 text"}), 400
    if request.is_json or body.lstrip().startswith("{"):
        try:
            body = json.loads(body).get("text", "")
        except (ValueError, AttributeError):
            return jsonify({"ok": False, "error": 'expected {"text": "..."}'}), 400
    if not isinstance(body, str) or summary(body)["glamours"] == 0:
        return jsonify({"ok": False, "error": "no glamours in it (each starts with a line beginning ==)"}), 400
    counts = write_list(body)
    return jsonify({"ok": True, "list": counts, "path": str(list_path().name)})


# ════════════════════════════════════════════════════════════════════════════
#  Dashboard -> Bot glamours
# ════════════════════════════════════════════════════════════════════════════
@dashboard.route("/glamours")
@auth.admin_required
def glamours():
    text, modified = read_list()
    index = class_index()
    return render_template(
        "dashboard/glamours.html", section="glamours",
        text=text, counts=summary(text), path=list_path(),
        modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)) if modified else "",
        backups=backups(), classes=class_list(index) if index else None,
        tool=tool_status(), has_classes_cmd=_has_classes_cmd(), busy=runner().busy)


@dashboard.route("/glamours/classes", methods=["POST"])
@auth.admin_required
def glamours_read_classes():
    status = tool_status()
    if not status["ok"]:
        flash(status["reason"], "err")
        return redirect(url_for("dashboard.glamours"))
    if not _has_classes_cmd():
        flash(f"The active build's {TOOL} predates the classes command. Build the fork again.", "err")
        return redirect(url_for("dashboard.glamours"))
    try:
        job = runner().start("Read the class list",
                             [("Write class-index.json", _run_step(["classes"]))])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.glamours"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="glamours"))


@dashboard.route("/glamours/restore", methods=["POST"])
@auth.admin_required
def glamours_restore():
    name = request.form.get("name", "")
    source = _backup_dir() / Path(name).name
    if not re.fullmatch(r"glamours-\d{8}-\d{6}\.txt", source.name) or not source.is_file():
        flash("No such backup.", "err")
        return redirect(url_for("dashboard.glamours"))
    counts = write_list(source.read_text(encoding="utf-8"))
    flash(f"Restored {source.name}: {counts['glamours']} glamours. The server reads it at once.", "ok")
    return redirect(url_for("dashboard.glamours"))
