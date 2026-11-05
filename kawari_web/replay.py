"""Dashboard -> Battle replay: a retail battle and a Kawari one side by side on a top-down map, on one clock.

The retail side is a battle run: one Kawari Recorder sent (Diagnostics' battles, <web>/diag/battles) or one uploaded
here (``questkit chronofoil --battle`` writes the same format from a Chronofoil capture; kept in
<web>/diag/replay-uploads). The Kawari side is a visit (one zone, from one ZoneInit to the next) of one of the world
server's packet captures (Diagnostics -> Packet capture). Either side can be either kind, so two retail runs or two
Kawari tries can be compared too. kawari_core/battle_replay.py reads them all into one shape.

The casts' areas are drawn from the game install's Action sheet, read once with ``kawari-questdump actions``
(action-index.json, beside the quest index), as the world server shapes them.

    GET  /dashboard/replay                      the page
    GET  /dashboard/replay/visits?file=NAME     a capture's zone visits (JSON)
    GET  /dashboard/replay/data?src=SPEC        a replay (JSON); SPEC is battle:FILE, upload:FILE or capture:FILE:VISIT
    POST /dashboard/replay/upload               keep a battle run (a .json.gz or .json form file)
    POST /dashboard/replay/delete               remove an uploaded run
    POST /dashboard/replay/actions              read the action list from the game
"""

from __future__ import annotations

import gzip
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import flash, jsonify, redirect, render_template, request, url_for

from kawari_core import battle_replay as R
from kawari_core import questlab as Q

from . import auth
from .admin_base import dashboard, runner, settings
from .diagnostics import _battle_dir, _battle_index, _capture_dir, _diag_dir
from .questlab import TOOL, _active_build, _run_step, tool_status

ACTION_INDEX = "action-index.json"
UPLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json(\.gz)?$")
MAX_UPLOAD_BYTES = 7 * 1024 * 1024      # under the app's 8 MB form limit
KEEP_UPLOADS = 100
CACHE_SIZE = 8

_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


def _upload_dir() -> Path:
    path = _diag_dir() / "replay-uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _action_index_path() -> Path:
    return Q.dumps_root(settings().data_dir) / ACTION_INDEX


def _has_actions_cmd() -> bool:
    build = _active_build()
    try:
        return build is not None and b"kawari-questdump actions" in (build / TOOL).read_bytes()
    except OSError:
        return False


def _safe(folder: Path, name: str) -> Path | None:
    """A file directly in `folder`, by its bare name, or None."""
    path = folder / Path(name).name
    return path if path.name == name and path.is_file() else None


# ════════════════════════════════════════════════════════════════════════════
#  The sources
# ════════════════════════════════════════════════════════════════════════════
def _battles() -> list[dict]:
    out = []
    for entry in reversed(_battle_index().get("Battles") or []):
        name = entry.get("File") or ""
        if name and (_battle_dir() / name).is_file():
            what = entry.get("Label") or entry.get("Content") or entry.get("Zone") or name
            out.append({"spec": f"battle:{name}", "label": f"Recorder: {what}", "file": name})
    return out


def _uploads() -> list[dict]:
    out = []
    for path in sorted(_upload_dir().glob("*.json*"), key=lambda p: p.stat().st_mtime, reverse=True):
        out.append({"spec": f"upload:{path.name}", "label": f"Uploaded: {path.name}", "file": path.name,
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))})
    return out


def _captures() -> list[dict]:
    folder = _capture_dir()
    if not folder.exists():
        return []
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    return [{"file": p.name, "size": p.stat().st_size,
             "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))} for p in files]


def _cached(key, build):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    value = build()
    with _cache_lock:
        _cache[key] = value
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return value


def _capture_path(name: str) -> Path | None:
    return _safe(_capture_dir(), name) if name.endswith(".jsonl") else None


def load(spec: str) -> dict:
    """The replay a source names, with its actions' names and shapes."""
    kind, _, rest = spec.partition(":")
    if kind in ("battle", "upload"):
        path = _safe(_battle_dir() if kind == "battle" else _upload_dir(), rest)
        if path is None:
            raise ValueError("no such battle")
        key = (spec, path.stat().st_mtime)
        replay = _cached(key, lambda: R.from_battle(R.read_battle_file(path), label=path.name))
    elif kind == "capture":
        name, _, visit = rest.rpartition(":")
        path = _capture_path(name)
        if path is None or not visit.isdigit():
            raise ValueError("no such capture")
        # A capture still being written changes under us, so its size is in the key too.
        key = (spec, path.stat().st_mtime, path.stat().st_size)
        replay = _cached(key, lambda: R.from_capture(path, int(visit)))
    else:
        raise ValueError("unknown source")
    index_path = _action_index_path()
    index_key = ("action-index", index_path.stat().st_mtime if index_path.exists() else 0)
    index = _cached(index_key, lambda: R.load_action_index(index_path))
    return R.annotate(dict(replay), index)


# ════════════════════════════════════════════════════════════════════════════
#  Pages
# ════════════════════════════════════════════════════════════════════════════
@dashboard.route("/replay")
@auth.admin_required
def replay():
    return render_template(
        "dashboard/replay.html", section="replay",
        battles=_battles(), uploads=_uploads(), captures=_captures(),
        has_index=_action_index_path().exists(), tool=tool_status(), has_actions_cmd=_has_actions_cmd(),
        busy=runner().busy, left=request.args.get("a", ""), right=request.args.get("b", ""))


@dashboard.route("/replay/visits")
@auth.admin_required
def replay_visits():
    path = _capture_path(request.args.get("file", ""))
    if path is None:
        return jsonify({"ok": False, "error": "no such capture"}), 404
    key = ("visits", path.name, path.stat().st_mtime, path.stat().st_size)
    visits = _cached(key, lambda: R.capture_visits(path))
    return jsonify({"ok": True, "visits": [
        {k: v for k, v in visit.items() if k not in ("start", "end")}
        | {"at": time.strftime("%H:%M:%S", time.gmtime(visit["start"] / 1000))} for visit in visits]})


@dashboard.route("/replay/data")
@auth.admin_required
def replay_data():
    try:
        return jsonify({"ok": True, "replay": load(request.args.get("src", ""))})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@dashboard.route("/replay/upload", methods=["POST"])
@auth.admin_required
def replay_upload():
    upload = request.files.get("run")
    if upload is None or not upload.filename:
        flash("Choose a battle file first.", "err")
        return redirect(url_for("dashboard.replay"))
    raw = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        flash(f"That file is over {MAX_UPLOAD_BYTES // 1048576} MB.", "err")
        return redirect(url_for("dashboard.replay"))
    name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(upload.filename).name).lstrip(".-") or "battle.json.gz"
    if not name.endswith((".json", ".json.gz")):
        name += ".json.gz"
    if not UPLOAD_NAME.match(name):
        name = f"battle-{time.strftime('%Y%m%d-%H%M%S')}.json.gz"
    path = _upload_dir() / name
    path.write_bytes(raw if name.endswith(".json") or raw[:2] == b"\x1f\x8b" else gzip.compress(raw))
    try:
        run = R.read_battle_file(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        path.unlink(missing_ok=True)
        flash(f"That isn't a battle run: {exc}", "err")
        return redirect(url_for("dashboard.replay"))
    for stale in sorted(_upload_dir().glob("*.json*"), key=lambda p: p.stat().st_mtime)[:-KEEP_UPLOADS]:
        stale.unlink(missing_ok=True)
    flash(f"Kept {name}: {len(run['Actors'])} actors, {len(run.get('Actions') or [])} actions.", "ok")
    return redirect(url_for("dashboard.replay", a=f"upload:{name}"))


@dashboard.route("/replay/delete", methods=["POST"])
@auth.admin_required
def replay_delete():
    path = _safe(_upload_dir(), request.form.get("name", ""))
    if path is None:
        flash("No such upload.", "err")
    else:
        path.unlink(missing_ok=True)
        flash(f"Removed {path.name}.", "ok")
    return redirect(url_for("dashboard.replay"))


@dashboard.route("/replay/actions", methods=["POST"])
@auth.admin_required
def replay_read_actions():
    status = tool_status()
    if not status["ok"]:
        flash(status["reason"], "err")
        return redirect(url_for("dashboard.replay"))
    if not _has_actions_cmd():
        flash(f"The active build's {TOOL} predates the actions command. Build the fork again.", "err")
        return redirect(url_for("dashboard.replay"))
    try:
        job = runner().start("Read the action list", [("Write action-index.json", _run_step(["actions"]))])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.replay"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="replay"))
