"""The harness: the server's state as JSON, for someone debugging it from a browser signed in as an administrator.

Made for Claude (2026-11-14, Bob: "we should work on building an API or harness of some kind for Claude"): a session
working on the server can open these pages in Bob's browser and read what a diagnostics bundle would hold, as it is
now, without a bundle being made and pushed: whether the server is up and on which build, its log (filtered), the
lines that say something went wrong, the packet captures being written, a character's quest state.

Rules it keeps:

* **Read-only.** Every route is a GET that changes nothing. What changes the server (a restart, a build, a bundle
  sent) stays on the dashboard's own pages, behind their forms and CSRF token, for a person to press.
* **Administrators only**, by the same session as the dashboard: no token to hand out or leak, nothing in a URL.
* **No secrets**: the server's config goes through the same scrubbing as a bundle's.
* **Bounded**: every answer is capped (lines, bytes), so a page can't be made to read a 40 MB capture into memory.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter

from flask import jsonify, request, url_for

from kawari_core import dbtools

from . import __version__, auth, diagnostics
from .admin_base import dashboard, podman, runner, settings

MAX_LOG_LINES = 20000
DEFAULT_LOG_LINES = 400
MAX_ANSWER_LINES = 2000
MAX_LINE_CHARS = 4000
MAX_PATTERN_CHARS = 200
CAPTURE_NAME = re.compile(r"^[A-Za-z0-9_.()-]+\.jsonl$")
# How much of a capture's end is read for its last lines.
CAPTURE_TAIL_BYTES = 8 * 1024 * 1024

# Lines that say something went wrong, and what each means (the fork's docs/src/fork/stability.md.)
PROBLEMS = [
    ("panic", r"panicked at |panicked handling|panicked:", "a panic (a bug): its location is on the line or just above"),
    ("restarted", r"kawari-run: \S+ ended", "a server ended and kawari-run started it again"),
    ("stuck", r"The world server (is|has been) stuck", "a step of the world held it for 15 s or more"),
    ("unstuck", r"step that started at .* has ended", "the stuck step ended"),
    ("out_of_step", r"couldn't be read, so its stream is out of step|desynced with the client's Oodle",
     "a client's packets stopped parsing (its compression stream lost)"),
    ("dropped", r"Dropping zone connection|killed because of a network error|went silent",
     "a zone connection ended other than by logging out"),
    ("error", r" ERROR ", "anything logged as an error"),
]


def _lines_arg(default: int, most: int) -> int:
    try:
        return max(1, min(most, int(request.args.get("lines", default))))
    except ValueError:
        return default


def _pattern(name: str):
    """A regex from the query string, or None; (None, message) when it isn't one."""
    text = request.args.get(name, "")
    if not text:
        return None, ""
    if len(text) > MAX_PATTERN_CHARS:
        return None, f"{name} is longer than {MAX_PATTERN_CHARS} characters"
    try:
        return re.compile(text), ""
    except re.error as exc:
        return None, f"{name} isn't a regular expression: {exc}"


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


def filter_lines(lines: list[str], grep=None, since: str = "") -> list[str]:
    """Lines matching `grep`, from the first whose timestamp's time of day is `since` (HH:MM or HH:MM:SS) or later."""
    if since:
        start = 0
        for index, line in enumerate(lines):
            stamp = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})", line)
            if stamp and stamp.group(1) >= since:
                start = index
                break
        else:
            start = len(lines)
        lines = lines[start:]
    if grep is not None:
        lines = [line for line in lines if grep.search(line)]
    return lines


def problems_in(lines: list[str]) -> dict:
    """The lines that say something went wrong, by kind (a line counts under the first kind it matches)."""
    kinds = [(key, re.compile(pattern), meaning) for key, pattern, meaning in PROBLEMS]
    counts: Counter = Counter()
    found = []
    for line in lines:
        for key, pattern, _ in kinds:
            if pattern.search(line):
                counts[key] += 1
                found.append({"kind": key, "line": _clip(line)})
                break
    return {
        "counts": dict(counts),
        "meanings": {key: meaning for key, _, meaning in kinds},
        "lines": found[-MAX_ANSWER_LINES:],
    }


def tail_jsonl(path, lines: int, name=None, direction: str = "") -> list:
    """The last `lines` records of a capture that match, oldest first; read from the file's end, at most
    CAPTURE_TAIL_BYTES of it."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        handle.seek(max(0, size - CAPTURE_TAIL_BYTES))
        chunk = handle.read()
    rows = chunk.split(b"\n")
    if size > CAPTURE_TAIL_BYTES:
        rows = rows[1:]                         # the first is cut in half
    out = []
    for raw in reversed(rows):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if direction and record.get("dir") != direction:
            continue
        if name is not None and not name.search(str(record.get("name") or record.get("text") or "")):
            continue
        for key in ("decoded", "hex"):
            if isinstance(record.get(key), str):
                record[key] = _clip(record[key])
        out.append(record)
        if len(out) >= lines:
            break
    out.reverse()
    return out


def _server_state() -> dict:
    p = podman()
    if p.mode == "none":
        return {"podman": "none", "running": False}
    try:
        state = p.inspect(settings().shell.container)
    except Exception as exc:                            # noqa: BLE001
        return {"podman": p.mode, "error": str(exc)}
    return {"podman": p.mode, "container": settings().shell.container, **state}


def _characters() -> tuple[list, str]:
    rows, err = dbtools.characters(settings().world_db)
    return ([{"content_id": row["content_id"], "name": row["name"], "zone_id": row["zone_id"],
              "online": bool(row["is_online"])} for row in rows], err)


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@dashboard.route("/harness")
@auth.admin_required
def harness_index():
    def at(endpoint, **values):
        return url_for(endpoint, **values)
    return jsonify({
        "ok": True,
        "what": "ArchonEngine ControlPanel's harness: the server's state as JSON, read-only, for administrators.",
        "controlpanel": __version__,
        "endpoints": {
            at("dashboard.harness_status"): "is the server up, since when, on which build; captures; who's online",
            at("dashboard.harness_log"): f"the server's log. ?lines= (default {DEFAULT_LOG_LINES}, most "
                                          f"{MAX_LOG_LINES}: how far back to read) &grep=regex &since=HH:MM[:SS] "
                                          f"(time of day, UTC as the log's). At most {MAX_ANSWER_LINES} lines come "
                                          "back, the newest.",
            at("dashboard.harness_problems"): "the log's lines that say something went wrong (panics, restarts, a "
                                               "stuck world, streams out of step, dropped connections, errors), "
                                               "counted by kind. ?lines= as the log's (default all it keeps)",
            at("dashboard.harness_captures"): "the packet captures being written, newest first",
            at("dashboard.harness_capture", name="NAME.jsonl"): "a capture's last records. ?lines= (default 200) "
                                                               "&name=regex (the packet's name, or a note's text) "
                                                               "&dir=in|out|note",
            at("dashboard.harness_characters"): "every character: content_id, name, zone, online",
            at("dashboard.harness_character", content_id=0): "one character's rows and quest summary, as a bundle's "
                                                             "characters.json",
            at("dashboard.harness_config"): "the server's config, secrets removed",
            at("dashboard.harness_bundles"): "the diagnostics bundles kept here",
        },
        "changing_things": "nothing here changes the server. Restarting, building, deploying and sending a bundle "
                           "are on the dashboard's pages (Overview, Updates & branches, Diagnostics).",
    })


@dashboard.route("/harness/status")
@auth.admin_required
def harness_status():
    characters, err = _characters()
    jobs = runner()
    return jsonify({
        "ok": True,
        "now": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "controlpanel": __version__,
        "server": _server_state(),
        "build": diagnostics._build_info(),
        "capture": diagnostics.capture_status(),
        "online": [c for c in characters if c["online"]],
        "characters": len(characters),
        "characters_error": err,
        "job_running": bool(jobs.busy),
    })


@dashboard.route("/harness/log")
@auth.admin_required
def harness_log():
    grep, problem = _pattern("grep")
    if problem:
        return _error(problem)
    since = request.args.get("since", "")
    if since and not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", since):
        return _error("since is HH:MM or HH:MM:SS")
    read = _lines_arg(DEFAULT_LOG_LINES, MAX_LOG_LINES)
    lines = diagnostics._server_log(read).splitlines()
    matched = filter_lines(lines, grep, since)
    return jsonify({
        "ok": True,
        "read": len(lines),
        "matched": len(matched),
        "lines": [_clip(line) for line in matched[-MAX_ANSWER_LINES:]],
    })


@dashboard.route("/harness/log/problems")
@auth.admin_required
def harness_problems():
    read = _lines_arg(MAX_LOG_LINES, MAX_LOG_LINES)
    lines = diagnostics._server_log(read).splitlines()
    return jsonify({"ok": True, "read": len(lines),
                    "from": lines[0][:27] if lines else "", "to": lines[-1][:27] if lines else "",
                    **problems_in(lines)})


@dashboard.route("/harness/captures")
@auth.admin_required
def harness_captures():
    folder = diagnostics._capture_dir()
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    return jsonify({
        "ok": True,
        "enabled": diagnostics.capture_status()["enabled"],
        "files": [{"name": p.name, "bytes": p.stat().st_size,
                   "written": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime)),
                   "url": url_for("dashboard.harness_capture", name=p.name)}
                  for p in files[:50]],
    })


@dashboard.route("/harness/captures/<name>")
@auth.admin_required
def harness_capture(name: str):
    if not CAPTURE_NAME.match(name):
        return _error("not a capture's name")
    path = diagnostics._capture_dir() / name
    if not path.is_file():
        return _error("no such capture", 404)
    pattern, problem = _pattern("name")
    if problem:
        return _error(problem)
    direction = request.args.get("dir", "")
    if direction not in ("", "in", "out", "note"):
        return _error("dir is in, out or note")
    records = tail_jsonl(path, _lines_arg(200, MAX_ANSWER_LINES), pattern, direction)
    return jsonify({"ok": True, "name": name, "bytes": path.stat().st_size, "records": records})


@dashboard.route("/harness/characters")
@auth.admin_required
def harness_characters():
    characters, err = _characters()
    return jsonify({"ok": not err, "error": err, "characters": characters})


@dashboard.route("/harness/characters/<int:content_id>")
@auth.admin_required
def harness_character(content_id: int):
    return jsonify({"ok": True, **diagnostics._character_state(content_id)})


@dashboard.route("/harness/config")
@auth.admin_required
def harness_config():
    return jsonify({"ok": True, "config": diagnostics._sanitised_config()})


@dashboard.route("/harness/bundles")
@auth.admin_required
def harness_bundles():
    return jsonify({"ok": True, "bundles": [{k: b[k] for k in ("name", "bytes", "when")}
                                            for b in diagnostics.kept_bundles()]})
