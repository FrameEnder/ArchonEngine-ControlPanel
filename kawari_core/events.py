"""The server's events: which of the seasonal and special events the fork has built are on, or when.

The world server reads events.json from its data folder (beside world.db and drop-rates.json;
servers/world/src/server/game_events.rs) again whenever it changes and looks at the schedule as it runs, so an event
comes and goes without a restart:

    {"format": 1, "events": {"keybound_brawler": {"mode": "scheduled", "start": 1793174400, "end": 1801000000}}}

``mode`` is "off" (the same as not being there), "on", or "scheduled" with ``start`` and ``end`` in Unix seconds (UTC);
without a start it's on until its end, without an end from its start for good.

What can be listed is the build's own: ``resources/events/fork_events.json`` has every event the fork has built (its
key, label, festival, phase, quests, duties, and when retail ran it). It isn't under ``resources/data``, because a
build's files leave that folder out (2.33.0 looked there and found nothing: Bob, 2026-11-15). No game text is kept
here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EVENT_FILE = "events.json"
BUILT_FILE = Path("resources") / "events" / "fork_events.json"
# A source tree has upstream's festival names; a build's files don't (resources/data is left out of them).
FESTIVAL_FILE = Path("resources") / "data" / "festivals.yml"
MODES = ("off", "on", "scheduled")
# The form's dates: a datetime-local input's value, read as UTC.
FORM_TIME = "%Y-%m-%dT%H:%M"


def built(build_dir: Path | None) -> list[dict]:
    """The events the active build has built; empty for a build without the file (before f9e4b0f's successor of
    2026-11-15, which moved it out of resources/data)."""
    if build_dir is None:
        return []
    try:
        raw = json.loads((Path(build_dir) / BUILT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for event in raw.get("events", []) if isinstance(raw, dict) else []:
        if not isinstance(event, dict) or not re.fullmatch(r"[a-z0-9_]{1,64}", str(event.get("key", ""))):
            continue
        try:
            out.append({
                "key": event["key"], "label": str(event.get("label", ""))[:80],
                "festival": int(event["festival"]), "phase": int(event.get("phase", 0)),
                "quests": [int(q) for q in event.get("quests", [])],
                "duties": [int(d) for d in event.get("duties", [])],
                "retail_start": _seconds(event.get("retail_start")), "retail_end": _seconds(event.get("retail_end")),
                "docs": str(event.get("docs", "")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def festival_names(build_dir: Path | None) -> dict[int, str]:
    """festivals.yml's names by ID (a list of ``- Id: N`` / ``Name: ...`` pairs), where the build has the file."""
    if build_dir is None:
        return {}
    try:
        text = (Path(build_dir) / FESTIVAL_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    return {int(number): name.strip() for number, name in
            re.findall(r"^- Id:\s*(\d+)\s*\n\s+Name:\s*(.+)$", text, flags=re.MULTILINE)}


def _seconds(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < 1 << 40 else None


def normal(data) -> dict:
    """Any file as format 1, with only what the server reads."""
    out = {"format": 1, "events": {}}
    events = data.get("events") if isinstance(data, dict) else None
    for key, raw in (events.items() if isinstance(events, dict) else ()):
        if not re.fullmatch(r"[a-z0-9_]{1,64}", str(key)) or not isinstance(raw, dict):
            continue
        mode = raw.get("mode")
        if mode not in MODES or mode == "off":
            continue
        setting = {"mode": mode}
        if mode == "scheduled":
            for name in ("start", "end"):
                number = _seconds(raw.get(name))
                if number is not None:
                    setting[name] = number
        out["events"][str(key)] = setting
    return out


def read(path: Path) -> tuple[dict, float | None]:
    """The file (an empty one when it isn't there or isn't JSON), and when it was written."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        modified = Path(path).stat().st_mtime
    except (OSError, ValueError):
        return normal({}), None
    return normal(data), modified


def write(path: Path, data: dict) -> None:
    """Written beside the file and renamed over it, so the server never reads half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".events-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(normal(data), out, indent=1, sort_keys=True)
        out.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def is_on(setting: dict | None, now: float) -> bool:
    """The server's rule (game_events.rs: is_on_in)."""
    mode = (setting or {}).get("mode")
    if mode == "on":
        return True
    if mode == "scheduled":
        start, end = setting.get("start"), setting.get("end")
        return (start is None or now >= start) and (end is None or now < end)
    return False


def state(setting: dict | None, now: float) -> tuple[str, str]:
    """What an event is doing now, for the page: a kind ("on", "off", "waiting", "over") and a sentence."""
    mode = (setting or {}).get("mode", "off")
    if mode == "on":
        return "on", "On"
    if mode != "scheduled":
        return "off", "Off"
    start, end = setting.get("start"), setting.get("end")
    if start is not None and now < start:
        return "waiting", f"Starts {clock(start)} UTC"
    if end is not None and now >= end:
        return "over", f"Ended {clock(end)} UTC"
    return "on", f"On until {clock(end)} UTC" if end is not None else "On (no end set)"


def clock(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M")


def to_form(seconds: int | None) -> str:
    """Unix seconds as a datetime-local input's value (UTC)."""
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(FORM_TIME)


def from_form(text: str | None) -> int | None:
    """A datetime-local input's value (read as UTC) as Unix seconds; None for blank or nonsense."""
    text = (text or "").strip()
    if not text:
        return None
    for layout in (FORM_TIME, "%Y-%m-%dT%H:%M:%S"):
        try:
            return _seconds(datetime.strptime(text, layout).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None
