"""Diagnostics: Dashboard -> Diagnostics.

Collects what is needed to debug the server from somewhere that cannot see it,
and hands it over as one bundle in a private GitHub repository:

* the world server's recent log (from the container);
* packet captures, when the server's packet_capture option is on - every packet
  between the server and the game, as JSONL (see servers/world/src/capture.rs);
* the saved quest state and position of the chosen characters, straight from
  world.db;
* snapshots from the Kawari Probe Dalamud plugin, which reads what the GAME
  CLIENT believes (its quest list and variables, zone, position), so the
  server's view and the client's can be compared at the same moment;
* which build is running.

Nothing here touches the game files: quest dumps and the like stay on this
server. The server config is included with anything that looks like a secret
removed.

The probe uploads to POST /api/diag/snapshot with the same per-account token as
the Kawari Companion sync (Settings -> Character sync).

Kawari Recorder (2.6+) sends each retail battle it records to POST /api/diag/battle
(the same token): one gzipped JSON file per battle, kept in <web>/diag/battles and
filed in the diagnostics repository under battles/ as soon as it arrives, so they
can be read without anyone exporting anything.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from flask import (
    abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for,
)

from kawari_core import dbtools
from kawari_core.versions import BuildStore

from . import __version__, auth, webdb
from .admin_base import dashboard, podman, settings
from .syncapi import _bearer, api, owner_of

# Where the server writes things, as seen from INSIDE the game container. The
# data directory is mounted there, so both survive container rebuilds.
CONTAINER_CAPTURE_DIR = "/kawari/data/captures"
CONTAINER_NAVMESH_DIR = "/kawari/data/navimesh"

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
# Bundles kept here as zips (sent or downloaded), newest first; the older ones are deleted.
KEEP_BUNDLES = 20
BUNDLE_ZIP = re.compile(r"^\d{8}-\d{6}[a-z0-9-]*\.zip$")
# A battle file is gzipped JSON with every packet in it; a long dungeon can be tens of MB. The app's 8 MB request limit
# is for forms, so the battle route reads its body itself, up to this.
MAX_BATTLE_BYTES = 64 * 1024 * 1024
BATTLE_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]+(-[a-z0-9-]+)?(-[0-9]+)?\.json\.gz$")
KEEP_SNAPSHOTS = 200
MAX_CAPTURE_BYTES = 40 * 1024 * 1024     # uncompressed, across all captures in a bundle
LOG_LINES = 4000

SECRET_KEY = re.compile(r"pass|secret|token|key", re.IGNORECASE)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ════════════════════════════════════════════════════════════════════════════
#  Where things are
# ════════════════════════════════════════════════════════════════════════════
def _diag_dir() -> Path:
    path = settings().web_dir / "diag"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_dir() -> Path:
    path = _diag_dir() / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bundle_dir() -> Path:
    """The bundles built here, as zips: to download again or hand to someone without the repository."""
    path = _diag_dir() / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _capture_dir() -> Path:
    """The capture folder as this process sees it (the server writes it in the data dir)."""
    return settings().db_dir / "captures"


def _get(key: str, default: str = "") -> str:
    return webdb.get_setting(settings().webapp_db, f"diag_{key}", default)


def _set(key: str, value: str) -> None:
    webdb.set_setting(settings().webapp_db, f"diag_{key}", value)


# ════════════════════════════════════════════════════════════════════════════
#  Probe snapshots (from the Dalamud plugin)
# ════════════════════════════════════════════════════════════════════════════
@api.route("/api/diag/ping")
def diag_ping():
    """Lets the probe check its URL and token."""
    user_id = owner_of(settings().webapp_db, _bearer())
    if user_id is None:
        return jsonify({"ok": False, "error": "unknown or missing token"}), 401
    return jsonify({"ok": True, "server": "kawari-web", "accepts": "diag-snapshot/1 diag-battle/1"})


@api.route("/api/diag/snapshot", methods=["POST"])
def diag_snapshot():
    user_id = owner_of(settings().webapp_db, _bearer())
    if user_id is None:
        return jsonify({"ok": False, "error": "unknown or missing token"}), 401

    raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"ok": False, "error": "empty body"}), 400
    if len(raw) > MAX_SNAPSHOT_BYTES:
        return jsonify({"ok": False, "error": "snapshot too large"}), 413
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return jsonify({"ok": False, "error": f"not valid JSON: {exc}"}), 400
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    payload["_received"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload["_account"] = user_id
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.json"
    (_snapshot_dir() / name).write_text(json.dumps(payload, indent=1))

    # Keep the folder bounded.
    files = sorted(_snapshot_dir().glob("*.json"))
    for old in files[:-KEEP_SNAPSHOTS]:
        old.unlink(missing_ok=True)
    return jsonify({"ok": True, "stored": name})


# ════════════════════════════════════════════════════════════════════════════
#  Battles (from Kawari Recorder)
# ════════════════════════════════════════════════════════════════════════════
_battle_lock = threading.Lock()


def _battle_dir() -> Path:
    path = _diag_dir() / "battles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _battle_index() -> dict:
    path = _battle_dir() / "index.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    return {"Battles": []}


def _save_battle_index(index: dict) -> None:
    path = _battle_dir() / "index.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(index, indent=1))
    temporary.replace(path)


def _battle_summary(raw: bytes) -> dict:
    """The few things the page shows about a battle, read from the file itself."""
    try:
        battle = json.loads(gzip.decompress(raw))
    except (OSError, ValueError):
        return {}
    events = battle.get("Events") or []
    setup = battle.get("Setup") or {}
    return {
        "Start": battle.get("Start"), "End": battle.get("End"),
        "TerritoryId": battle.get("TerritoryId"), "Zone": battle.get("Zone", ""),
        "Content": battle.get("Content", ""), "Field": bool(battle.get("Field")),
        "Actors": len(battle.get("Actors") or {}), "Actions": len(battle.get("Actions") or []),
        "Marks": sum(1 for e in events if e.get("Kind") in ("mark", "auto mark")),
        "Job": setup.get("JobName", ""), "Level": setup.get("Level"),
        "Recorder": battle.get("RecorderVersion", ""),
        "HooksFailed": [k for k, v in (battle.get("Hooks") or {}).items() if str(v).startswith("FAILED")],
    }


def _file_battle(name: str) -> None:
    """Commits a battle (and the battle list) to the diagnostics repository, if one is set up. Runs in the background."""
    if not (_get("repo") and _get("token")):
        return
    with _battle_lock:
        index = _battle_index()
        entry = next((b for b in index["Battles"] if b.get("File") == name), None)
        path = _battle_dir() / name
        if entry is None or not path.exists():
            return
        public_index = {"Battles": [{k: v for k, v in b.items() if not k.startswith("_")} for b in index["Battles"]]}
        files = {
            f"battles/{name}": path.read_bytes(),
            "battles/index.json": json.dumps(public_index, indent=1).encode(),
            "battles/latest.txt": f"battles/{name}\n".encode(),
        }
        try:
            url = _github().commit_files(files, f"Battle: {entry.get('Zone', '')} {entry.get('Content', '')}"
                                                f"{' (' + entry['Label'] + ')' if entry.get('Label') else ''}".strip())
            entry["_filed"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            entry["_url"] = url
            entry.pop("_error", None)
        except Exception as exc:                        # noqa: BLE001
            entry["_error"] = str(exc)
        _save_battle_index(index)


@api.route("/api/diag/battle", methods=["POST"])
def diag_battle():
    """A battle from Kawari Recorder: ?file=<its file name>&label=<the run's label>&finished=1, the body its .json.gz."""
    user_id = owner_of(settings().webapp_db, _bearer())
    if user_id is None:
        return jsonify({"ok": False, "error": "unknown or missing token"}), 401
    name = request.args.get("file", "")
    if not BATTLE_NAME.match(name):
        return jsonify({"ok": False, "error": "file must be a Recorder battle name (<date>-<time>-<territory>-<what>.json.gz)"}), 400
    try:
        length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        return jsonify({"ok": False, "error": "empty body"}), 400
    if length > MAX_BATTLE_BYTES:
        return jsonify({"ok": False, "error": f"battle too large (over {MAX_BATTLE_BYTES // 1048576} MB)"}), 413
    raw = request.environ["wsgi.input"].read(length)
    summary = _battle_summary(raw)
    if not summary:
        return jsonify({"ok": False, "error": "not a gzipped battle"}), 400

    with _battle_lock:
        (_battle_dir() / name).write_bytes(raw)
        index = _battle_index()
        entry = next((b for b in index["Battles"] if b.get("File") == name), None)
        if entry is None:
            entry = {"File": name}
            index["Battles"].append(entry)
        entry.update(summary)
        entry["Label"] = request.args.get("label", "")[:200]
        entry["Finished"] = request.args.get("finished", "1") == "1"
        entry["Bytes"] = len(raw)
        entry["_received"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        entry["_account"] = user_id
        index["Battles"].sort(key=lambda b: str(b.get("Start") or ""))
        _save_battle_index(index)
    threading.Thread(target=_file_battle, args=(name,), daemon=True).start()
    return jsonify({"ok": True, "stored": name, "filing": bool(_get("repo") and _get("token"))})


def _recent_battles(since: float) -> list[dict]:
    """Battles received since then, for a bundle's README."""
    out = []
    for entry in _battle_index()["Battles"]:
        path = _battle_dir() / entry.get("File", "")
        if path.exists() and path.stat().st_mtime >= since:
            out.append(entry)
    return out


def _recent_snapshots(since: float, limit: int = 30) -> list[Path]:
    files = [p for p in sorted(_snapshot_dir().glob("*.json")) if p.stat().st_mtime >= since]
    return files[-limit:]


# ════════════════════════════════════════════════════════════════════════════
#  Server-side state
# ════════════════════════════════════════════════════════════════════════════
def _character_state(content_id: int) -> dict:
    """Quest and position rows for one character, read without the schema map so
    an out-of-date map can't hide anything."""
    s = settings()
    out: dict = {"content_id": content_id}
    try:
        conn = sqlite3.connect(f"file:{s.world_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"content_id": content_id, "error": str(exc)}
    try:
        for table in ("character", "volatile", "quest", "classjob"):
            try:
                row = conn.execute(f"SELECT * FROM `{table}` WHERE content_id = ?",
                                   (content_id,)).fetchone()
            except sqlite3.Error as exc:
                out[table] = {"error": str(exc)}
                continue
            if row is None:
                out[table] = None
                continue
            record = {}
            for key in row.keys():
                value = row[key]
                if isinstance(value, bytes):
                    value = value.hex()
                elif isinstance(value, str) and value[:1] in "[{":
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                record[key] = value
            if table == "character":
                # Only what identifies it; the rest is noise here.
                record = {k: record.get(k) for k in
                          ("content_id", "name", "actor_id", "city_state", "gm_rank")}
            out[table] = record
        quest = out.get("quest")
        if isinstance(quest, dict):
            out["quest_summary"] = _summarise_quests(quest)
    finally:
        conn.close()
    return out


def _summarise_quests(row: dict) -> dict:
    """Active quests with their variables split into bytes and nibbles, plus the
    completed quest ids decoded from the bitmask."""
    active = []
    raw_active = row.get("active")
    entries = raw_active if isinstance(raw_active, list) else []
    for quest in entries:
        if not isinstance(quest, dict):
            continue
        variables = list(quest.get("variables") or [0] * 6)
        active.append({
            "id": quest.get("id"),
            "sequence": quest.get("sequence"),
            "variables_hex": " ".join(f"{v:02x}" for v in variables),
            "nibbles": {f"UI8{'ABCDEF'[i]}": {"H": v >> 4, "L": v & 0x0F}
                        for i, v in enumerate(variables[:6])},
        })

    completed = []
    bitmask = row.get("completed")
    data = bitmask.get("data") if isinstance(bitmask, dict) else bitmask
    if isinstance(data, list):
        for index, byte in enumerate(data):
            if not isinstance(byte, int):
                continue
            # Kawari's quest bitmask is the client's: quest N is bit 0x80 >> (N % 8) of byte N // 8 (the world
            # server's QuestBitmaskTransformation). Until 2.24.1 this read 1 << bit, listing the wrong quests.
            for bit in range(8):
                if byte & (0x80 >> bit):
                    completed.append(index * 8 + bit)
    return {"active": active, "completed_ids": completed}


def _server_log(lines: int = LOG_LINES) -> str:
    p = podman()
    if p.mode == "none":
        return "(no Podman access, so no log)"
    try:
        return ANSI.sub("", p.logs(settings().shell.container, lines))
    except Exception as exc:                            # noqa: BLE001
        return f"(could not read the log: {exc})"


def _sanitised_config() -> dict:
    def scrub(value):
        if isinstance(value, dict):
            return {k: ("(removed)" if SECRET_KEY.search(str(k)) else scrub(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    cfg = settings().server_config()
    return scrub(cfg.data) if not cfg.error else {"error": cfg.error}


def _build_info() -> dict:
    store = BuildStore(settings().data_dir)
    entry = store.ledger.active_entry
    if not entry:
        return {"active": None}
    return {
        "active": entry.sha, "short": entry.short, "message": entry.commit_message,
        "date": entry.commit_date, "source": entry.source, "notes": entry.notes,
        "game_version": entry.game_version,
    }


def _captures(since: float) -> list[Path]:
    """Capture files written to since `since`, newest first, within the size cap."""
    folder = _capture_dir()
    if not folder.exists():
        return []
    files = sorted((p for p in folder.glob("*.jsonl") if p.stat().st_mtime >= since),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    chosen, total = [], 0
    for path in files:
        size = path.stat().st_size
        if chosen and total + size > MAX_CAPTURE_BYTES:
            break
        chosen.append(path)
        total += size
    return chosen


def capture_status() -> dict:
    cfg = settings().server_config()
    world = cfg.data.get("world") or {}
    filesystem = cfg.data.get("filesystem") or {}
    folder = _capture_dir()
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True) if folder.exists() else []
    return {
        "enabled": bool(world.get("packet_capture", False)),
        "dir_ok": world.get("packet_capture_dir") == CONTAINER_CAPTURE_DIR,
        "navmesh_kept": filesystem.get("navimesh_path") == CONTAINER_NAVMESH_DIR,
        "files": [{"name": p.name, "size": p.stat().st_size,
                   "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))}
                  for p in files[:10]],
        "total": sum(p.stat().st_size for p in files),
    }


# ════════════════════════════════════════════════════════════════════════════
#  GitHub
# ════════════════════════════════════════════════════════════════════════════
class GitHubError(RuntimeError):
    pass


class GitHub:
    """Just enough of the GitHub REST API to add one commit of files to a branch."""

    def __init__(self, repo: str, token: str, branch: str = "main"):
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo or ""):
            raise GitHubError("The repository should look like owner/name.")
        if not token:
            raise GitHubError("No GitHub token is saved.")
        self.repo, self.token, self.branch = repo, token, branch or "main"

    def _call(self, method: str, path: str, body=None, ok=(200, 201)):
        url = f"https://api.github.com/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kawari-web-diagnostics",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise GitHubError(f"could not reach GitHub: {exc.reason}") from exc
        parsed = json.loads(payload) if payload else {}
        if status not in ok:
            message = parsed.get("message", "") if isinstance(parsed, dict) else ""
            raise GitHubError(f"GitHub said {status} for {method} {path}: {message}")
        return status, parsed

    def check(self) -> str:
        _status, info = self._call("GET", "")
        private = info.get("private")
        return (f"{info.get('full_name')} is reachable"
                + ("" if private else " - WARNING: it is PUBLIC"))

    def _head(self):
        status, ref = self._call("GET", f"/git/ref/heads/{self.branch}", ok=(200, 404, 409))
        if status == 200:
            return ref["object"]["sha"]
        # An empty repository has no branch yet: create it with a first file.
        self._call("PUT", "/contents/README.md", {
            "message": "Start the diagnostics repository",
            "content": base64.b64encode(
                b"# Kawari diagnostics\n\nBundles sent from ArchonEngine ControlPanel's Diagnostics page.\n"
            ).decode(),
            "branch": self.branch,
        })
        _status, ref = self._call("GET", f"/git/ref/heads/{self.branch}")
        return ref["object"]["sha"]

    def commit_files(self, files: dict, message: str) -> str:
        """files: {path: bytes}. Returns the new commit's URL."""
        parent = self._head()
        _s, commit = self._call("GET", f"/git/commits/{parent}")
        tree = []
        for path, content in files.items():
            _s, blob = self._call("POST", "/git/blobs", {
                "content": base64.b64encode(content).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        _s, new_tree = self._call("POST", "/git/trees",
                                  {"base_tree": commit["tree"]["sha"], "tree": tree})
        _s, new_commit = self._call("POST", "/git/commits", {
            "message": message, "tree": new_tree["sha"], "parents": [parent]})
        self._call("PATCH", f"/git/refs/heads/{self.branch}", {"sha": new_commit["sha"]})
        return new_commit.get("html_url", new_commit["sha"])


def _github() -> GitHub:
    return GitHub(_get("repo"), _get("token"), _get("branch", "main"))


# ════════════════════════════════════════════════════════════════════════════
#  The bundle
# ════════════════════════════════════════════════════════════════════════════
def build_bundle(note: str, content_ids: list[int], minutes: int,
                 quest_test: dict | None = None) -> tuple[str, dict]:
    """Returns (folder name, {path: bytes}). quest_test is Quest Lab's test session, if the bundle ends one: which
    quest was tested, on whom, from which step, since when (it goes in meta.json for questkit triage.)"""
    since = time.time() - minutes * 60
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", note.lower()).strip("-")[:40]
    folder = f"bundles/{stamp}{('-' + slug) if slug else ''}"
    files: dict[str, bytes] = {}

    characters = [_character_state(cid) for cid in content_ids]
    snapshots = _recent_snapshots(since)
    captures = _captures(since)
    # Whoever was playing in the window's captures first, marked, so a bundle reads as theirs (Bob's read as the
    # first character in the database's, 2026-10-26.)
    played = _played_in(captures)
    for ch in characters:
        ch["played_in_window"] = ch.get("content_id") in played
    characters.sort(key=lambda ch: not ch["played_in_window"])
    battles = _recent_battles(since)
    build = _build_info()

    meta = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": note,
        "window_minutes": minutes,
        "build": build,
        "kawari_web": __version__,
        "files": {
            "server_log": "server.log",
            "characters": "characters.json",
            "config": "config.json",
            "captures": [f"captures/{p.name}.gz" for p in captures],
            "snapshots": [f"snapshots/{p.name}" for p in snapshots],
            # Filed on their own as they arrived; these are the ones from this bundle's window.
            "battles": [f"battles/{b['File']}" for b in battles],
        },
    }
    if quest_test:
        meta["quest_test"] = quest_test

    files[f"{folder}/meta.json"] = json.dumps(meta, indent=1).encode()
    files[f"{folder}/server.log"] = _server_log().encode("utf-8", "replace")
    files[f"{folder}/characters.json"] = json.dumps(characters, indent=1).encode()
    files[f"{folder}/config.json"] = json.dumps(_sanitised_config(), indent=1).encode()
    for path in captures:
        files[f"{folder}/captures/{path.name}.gz"] = gzip.compress(path.read_bytes())
    for path in snapshots:
        files[f"{folder}/snapshots/{path.name}"] = path.read_bytes()
    files[f"{folder}/README.md"] = _readme(meta, characters, captures, snapshots).encode()
    files["latest.txt"] = (folder + "\n").encode()
    return folder, files


def _played_in(captures: list) -> set:
    """The content IDs the captures' PlayerSetup packets name (each capture is one session's.)"""
    played = set()
    for path in captures:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"PlayerSetup"' in line:
                        match = re.search(r"content_id: (\d+)", line)
                        if match:
                            played.add(int(match.group(1)))
                        break
        except OSError:
            pass
    return played


def _marks(captures: list) -> list[tuple[str, str, int, str]]:
    """The `!mark` notes in the captures (the world server writes them as `note` lines starting with MARK, from fork
    build 2026-11-05 on): (UTC time, capture file, line number, the note), in time order."""
    marks = []
    for path in captures:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for number, line in enumerate(f, start=1):
                    if '"dir":"note"' not in line.replace(" ", ""):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    text = str(entry.get("text") or "")
                    if not text.startswith("MARK"):
                        continue
                    when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(entry.get("t", 0)) / 1000))
                    marks.append((when, Path(path).name, number, text[len("MARK"):].strip()))
        except OSError:
            pass
    return sorted(marks)


def _readme(meta: dict, characters: list, captures: list, snapshots: list) -> str:
    lines = [f"# Diagnostics {meta['created']}", ""]
    if meta["note"]:
        lines += ["> " + meta["note"].replace("\n", "\n> "), ""]
    build = meta["build"]
    lines.append(f"* Build: `{build.get('short') or build.get('active') or 'unknown'}`"
                 + (f" ({build['message']})" if build.get("message") else ""))
    lines.append(f"* Window: last {meta['window_minutes']} minutes")
    test = meta.get("quest_test")
    if test:
        lines.append(f"* Quest test: quest {test.get('quest')} ({test.get('name', '')}) on {test.get('character', '')},"
                     f" from {test.get('from', '')}, started {test.get('started', '')}")
    lines.append(f"* Packet captures: {len(captures)}, probe snapshots: {len(snapshots)}")
    marks = _marks(captures)
    if marks:
        lines.append(f"* Marks (`!mark` in game): {len(marks)}")
        for when, name, number, text in marks:
            lines.append(f"  * {when} UTC, `captures/{name}.gz` line {number}: {text}")
    battles = meta["files"].get("battles") or []
    if battles:
        lines.append(f"* Retail battles from Kawari Recorder in this window (in the repository's battles/): {len(battles)}")
        for path in battles:
            lines.append(f"  * `{path}`")
    lines.append("")
    for ch in characters:
        name = (ch.get("character") or {}).get("name") or ch.get("content_id")
        volatile = ch.get("volatile") or {}
        lines.append(f"## {name}" + (" (played in this window)" if ch.get("played_in_window") else ""))
        lines.append(f"Zone {volatile.get('zone_id')}, position {volatile.get('position')}")
        lines.append("")
        summary = ch.get("quest_summary") or {}
        if summary.get("active"):
            lines.append("| Quest | Sequence | Variables (A-F) |")
            lines.append("|---|---|---|")
            for q in summary["active"]:
                lines.append(f"| {q['id']} | {q['sequence']} | `{q['variables_hex']}` |")
            lines.append("")
    return "\n".join(lines) + "\n"


# ════════════════════════════════════════════════════════════════════════════
#  Page
# ════════════════════════════════════════════════════════════════════════════
def bundle_zip(folder: str, files: dict) -> bytes:
    """A bundle as one zip, its files under the bundle's own folder name (`20261114-024122-note/README.md`), as the
    repository has them; `latest.txt` is the repository's and stays out."""
    name = folder.rsplit("/", 1)[-1]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            if not path.startswith(folder + "/"):
                continue
            # Captures are gzipped already: stored, not squeezed twice.
            kind = zipfile.ZIP_STORED if path.endswith(".gz") else zipfile.ZIP_DEFLATED
            archive.writestr(f"{name}/{path[len(folder) + 1:]}", data, compress_type=kind)
    return buffer.getvalue()


def keep_bundle(folder: str, files: dict) -> Path:
    """Keeps a built bundle as a zip in the bundles folder, and deletes all but the newest KEEP_BUNDLES."""
    target = _bundle_dir() / (folder.rsplit("/", 1)[-1] + ".zip")
    target.write_bytes(bundle_zip(folder, files))
    for old in kept_bundles()[KEEP_BUNDLES:]:
        (_bundle_dir() / old["name"]).unlink(missing_ok=True)
    return target


def kept_bundles() -> list[dict]:
    """The kept bundles, newest first: name, size, when."""
    out = []
    for path in _bundle_dir().glob("*.zip"):
        if BUNDLE_ZIP.match(path.name):
            stat = path.stat()
            out.append({"name": path.name, "bytes": stat.st_size,
                        "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)), "mtime": stat.st_mtime})
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def _bundle_form() -> tuple[str, int, list[int]]:
    note = (request.form.get("note") or "").strip()[:2000]
    try:
        minutes = max(5, min(24 * 60, int(request.form.get("minutes", "60"))))
    except ValueError:
        minutes = 60
    content_ids = [int(x) for x in request.form.getlist("characters") if x.isdigit()]
    _set("characters", ",".join(str(c) for c in content_ids))
    return note, minutes, content_ids


@dashboard.route("/diagnostics")
@auth.admin_required
def diagnostics():
    s = settings()
    rows, _err = dbtools.characters(s.world_db)
    characters = [dict(r) for r in rows or []]
    snapshots = sorted(_snapshot_dir().glob("*.json"), reverse=True)[:10]
    latest = None
    if snapshots:
        try:
            latest = json.loads(snapshots[0].read_text())
        except ValueError:
            latest = None
    battles = list(reversed(_battle_index()["Battles"]))[:30]
    return render_template(
        "dashboard/diagnostics.html", section="diagnostics", battles=battles,
        capture=capture_status(), characters=characters,
        repo=_get("repo"), branch=_get("branch", "main"), has_token=bool(_get("token")),
        last_sent=_get("last_sent"), last_url=_get("last_url"),
        snapshots=[p.name for p in snapshots], latest=latest,
        default_ids=[int(x) for x in _get("characters").split(",") if x.isdigit()],
        quest_test=_quest_test(), bundles=kept_bundles(),
    )


def _quest_test():
    from .questtest import current_test
    return current_test()


def set_capture(enabled: bool, restart: bool) -> tuple[bool, str]:
    """Turn the world server's packet capture on or off in config.yaml, and restart the server if asked (it only
    reads the option at startup.) Returns (whether it was saved, what happened.)"""
    s = settings()
    cfg = s.server_config()
    if cfg.error:
        return False, f"config.yaml can't be edited: {cfg.error}"
    cfg.set_value("world", "packet_capture", enabled)
    if enabled:
        cfg.set_value("world", "packet_capture_dir", CONTAINER_CAPTURE_DIR)
    try:
        cfg.save(advertise_ip=s.shell.ip, write_server_names=False)
    except Exception as exc:                            # noqa: BLE001
        return False, f"Could not save config.yaml: {exc}"
    what = "Packet capture is on" if enabled else "Packet capture is off"
    if restart and podman().mode != "none":
        result = podman().restart(s.shell.container)
        if result.ok:
            return True, f"{what}, and the server is restarting."
        return True, f"{what} in config.yaml, but the restart failed: {result.error}"
    return True, f"{what} in config.yaml; restart the server for it to take effect."


@dashboard.route("/diagnostics/capture", methods=["POST"])
@auth.admin_required
def diagnostics_capture():
    s = settings()
    cfg = s.server_config()
    if cfg.error:
        flash(f"config.yaml can't be edited: {cfg.error}", "err")
        return redirect(url_for("dashboard.diagnostics"))

    action = request.form.get("action", "")
    if action == "on":
        cfg.set_value("world", "packet_capture", True)
        cfg.set_value("world", "packet_capture_dir", CONTAINER_CAPTURE_DIR)
    elif action == "off":
        cfg.set_value("world", "packet_capture", False)
    elif action == "navmesh":
        cfg.set_value("filesystem", "navimesh_path", CONTAINER_NAVMESH_DIR)
    elif action == "clear":
        removed = 0
        for path in _capture_dir().glob("*.jsonl"):
            path.unlink(missing_ok=True)
            removed += 1
        flash(f"Deleted {removed} capture file(s).", "ok")
        return redirect(url_for("dashboard.diagnostics"))
    else:
        return redirect(url_for("dashboard.diagnostics"))

    try:
        cfg.save(advertise_ip=s.shell.ip, write_server_names=False)
    except Exception as exc:                            # noqa: BLE001
        flash(f"Could not save config.yaml: {exc}", "err")
        return redirect(url_for("dashboard.diagnostics"))

    if request.form.get("restart") == "on" and podman().mode != "none":
        result = podman().restart(s.shell.container)
        flash("Saved, and the server is restarting." if result.ok
              else f"Saved, but the restart failed: {result.error}", "ok" if result.ok else "err")
    else:
        flash("Saved. Restart the server for it to take effect.", "ok")
    return redirect(url_for("dashboard.diagnostics"))


@dashboard.route("/diagnostics/repo", methods=["POST"])
@auth.admin_required
def diagnostics_repo():
    repo = (request.form.get("repo") or "").strip()
    branch = (request.form.get("branch") or "main").strip() or "main"
    token = (request.form.get("token") or "").strip()
    _set("repo", repo)
    _set("branch", branch)
    if token:
        _set("token", token)
    if request.form.get("action") == "test":
        try:
            flash(_github().check(), "ok")
        except GitHubError as exc:
            flash(str(exc), "err")
    else:
        flash("Saved.", "ok")
    return redirect(url_for("dashboard.diagnostics"))


@dashboard.route("/diagnostics/send", methods=["POST"])
@auth.admin_required
def diagnostics_send():
    note, minutes, content_ids = _bundle_form()

    try:
        github = _github()
        folder, files = build_bundle(note, content_ids, minutes)
        keep_bundle(folder, files)
        size = sum(len(v) for v in files.values())
        url = github.commit_files(files, f"Diagnostics: {note[:60] or folder}")
    except GitHubError as exc:
        flash(f"Not sent: {exc}", "err")
        return redirect(url_for("dashboard.diagnostics"))
    except Exception as exc:                            # noqa: BLE001
        flash(f"Not sent: {exc}", "err")
        return redirect(url_for("dashboard.diagnostics"))

    _set("last_sent", f"{time.strftime('%Y-%m-%d %H:%M')} - {folder} "
                      f"({len(files)} files, {size // 1024} KiB)")
    _set("last_url", url)
    flash(f"Sent {folder} ({len(files)} files, {size // 1024} KiB).", "ok")
    return redirect(url_for("dashboard.diagnostics"))


@dashboard.route("/diagnostics/download", methods=["POST"])
@auth.admin_required
def diagnostics_download():
    """The same bundle as Send makes, as a zip to download: for troubleshooting by hand, or someone without the
    repository. Needs no repository or token. A copy is kept with the others."""
    note, minutes, content_ids = _bundle_form()
    try:
        folder, files = build_bundle(note, content_ids, minutes)
        target = keep_bundle(folder, files)
    except Exception as exc:                            # noqa: BLE001
        flash(f"Not built: {exc}", "err")
        return redirect(url_for("dashboard.diagnostics"))
    return send_file(target, mimetype="application/zip", as_attachment=True, download_name=target.name)


@dashboard.route("/diagnostics/bundle/<name>")
@auth.admin_required
def diagnostics_bundle(name: str):
    target = _bundle_dir() / name
    if not BUNDLE_ZIP.match(name) or not target.is_file():
        abort(404)
    return send_file(target, mimetype="application/zip", as_attachment=True, download_name=name)


@dashboard.route("/diagnostics/bundle/<name>/delete", methods=["POST"])
@auth.admin_required
def diagnostics_bundle_delete(name: str):
    target = _bundle_dir() / name
    if BUNDLE_ZIP.match(name) and target.is_file():
        target.unlink()
        flash(f"Deleted {name}.", "ok")
    return redirect(url_for("dashboard.diagnostics"))


@dashboard.route("/diagnostics/battle/<name>/file", methods=["POST"])
@auth.admin_required
def diagnostics_file_battle(name: str):
    """Files a battle again (after the repository was set up, or a failed try)."""
    if BATTLE_NAME.match(name) and (_battle_dir() / name).exists():
        threading.Thread(target=_file_battle, args=(name,), daemon=True).start()
        flash(f"Filing {name} in the repository.", "ok")
    return redirect(url_for("dashboard.diagnostics"))


@dashboard.route("/diagnostics/snapshot/<name>")
@auth.admin_required
def diagnostics_snapshot(name: str):
    if not re.match(r"^[0-9-]+\.json$", name):
        return ("Not found", 404)
    path = _snapshot_dir() / name
    if not path.exists():
        return ("Not found", 404)
    return current_app.response_class(path.read_text(), mimetype="application/json")
