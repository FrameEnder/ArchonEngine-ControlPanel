"""Script Check (Quest Lab): the server's event scripts checked against the client's.

A copy of FrameEnder/Kawari's `tools/questkit/questkit/clientcheck.py` (`questkit clientcheck`), reading the active
build's `resources/scripts` instead of a source tree; keep the two the same.

The client's script for an event (kawari-questdump's `scripts custom|opening`, or `dump` for quests) says which scenes
exist, what each returns and the constants it compares them with; the server's script has to play those scenes and
read those answers. This reports, for every server script that has a client script dumped:

* scenes the server plays that the client doesn't have (it would play nothing, or the wrong thing);
* constants the server names the same as the client's but with another value (MAIN_MENU_RESULT_SPECIALSHOP = 2);
* names that look like the sheet's parameters (ACTOR0, ENEMY1...) that the sheet doesn't have (nil in Lua);
* scenes the client has that the server never plays (a branch skipped on purpose, or missed);
* the server script's TODO, HACK, FIXME and "not sure" lines.

The dumps are Square Enix's files and stay out of the repository; the report has scene numbers, names of constants
and parameters, and the server's own lines, nothing of the game's text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_SCENE_FN = re.compile(r":OnScene(\d+)\b")
_PLAY_ARG = re.compile(r"\bplay_scene\s*\(\s*([A-Za-z_][\w.]*|\d+)")
_NUM_CONST = re.compile(
    r"^\s*(?:local\s+)?([A-Z][A-Z0-9_]*)\s*(?:<const>)?\s*=\s*(0[xX][0-9a-fA-F]+|-?\d+)\s*(?:--.*)?$", re.M)
_LUA_ASSIGN = re.compile(r"^\s*(?:local\s+)?([A-Z][A-Z0-9_]*)\s*(?:<const>)?\s*=(?!=)", re.M)
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9_]*\b")
# Sheet parameter names carry an index: ACTOR0, EOBJECT3, SEQ_1_ACTOR2, ITEM1...
_PARAM_LIKE = re.compile(r"^[A-Z][A-Z_]*\d")
_CLIENT_CONST = re.compile(r'SETTABLE\s+R\d+\["([A-Z][A-Z0-9_]*)"\]\s*=\s*(-?\d+(?:\.\d+)?)\s*$', re.M)
_NOTE = re.compile(r"--.*(\b(TODO|HACK|FIXME|XXX)\b|\b[Nn]ot sure\b)")


def strip_lua(text: str) -> str:
    """Lua without comments and string literals, for scanning tokens."""
    text = re.sub(r"--\[(=*)\[.*?\]\1\]", " ", text, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"\[(=*)\[.*?\]\1\]", '""', text, flags=re.S)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    return re.sub(r"'(?:\\.|[^'\\\n])*'", "''", text)


def shared_names(scripts: Path) -> set[str]:
    """Names the server's shared scripts define (scene flags, event types...), so they aren't taken for parameters."""
    names: set[str] = set()
    for rel in ("Global.lua", "Init.lua"):
        path = scripts / rel
        if path.exists():
            names |= set(_LUA_ASSIGN.findall(strip_lua(path.read_text())))
    return names


def _number(text: str) -> float:
    return float(int(text, 16)) if text.lower().startswith("0x") else float(text)


@dataclass
class Check:
    kind: str  # custom, opening or quest
    name: str
    server_script: Path
    client_scenes: list[int] = field(default_factory=list)
    played: list[int] = field(default_factory=list)
    unknown_scenes: list[int] = field(default_factory=list)  # played by the server, not in the client
    unplayed: list[int] = field(default_factory=list)  # in the client, never played by the server
    constant_mismatches: list[tuple[str, float, float]] = field(default_factory=list)  # name, server, client
    unknown_params: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def issues(self) -> bool:
        return bool(self.unknown_scenes or self.constant_mismatches or self.unknown_params)


def check(kind: str, name: str, server_script: Path, listing: str, params: dict[str, int],
          known: set[str]) -> Check:
    lua = server_script.read_text()
    code = strip_lua(lua)
    result = Check(kind, name, server_script)
    result.client_scenes = sorted({int(n) for n in _SCENE_FN.findall(listing)})

    constants = {n: _number(v) for n, v in _NUM_CONST.findall(code)}
    played = set()
    for const, value in constants.items():
        # A SCENE constant counts when the script uses it, not just defines it (a note about a scene, as
        # RegYok6Tuliyollal's SCENE_00001, isn't playing it.)
        used = len(re.findall(rf"\b{const}\b", code)) > 1
        if const.startswith("SCENE") and used and value == int(value) and value < 65536:
            played.add(int(value))
    for arg in _PLAY_ARG.findall(code):
        if arg.isdigit():
            played.add(int(arg))
        elif arg in constants:
            played.add(int(constants[arg]))
        elif arg in params:
            played.add(int(params[arg]))
    # The sheet's scene parameters (SEQ_2_EOBJECT0_EVENTACTIONOK...), however the script plays them.
    for token in set(_TOKEN.findall(code)):
        if token.startswith("SEQ_") and token in params:
            played.add(int(params[token]))
    # Kawari's own scene for "returned from a nested event" isn't a client scene.
    played.discard(255)
    result.played = sorted(played)
    if result.client_scenes:
        result.unknown_scenes = [n for n in result.played if n not in result.client_scenes]
    result.unplayed = [n for n in result.client_scenes if n not in played]

    client_constants = {n: float(v) for n, v in _CLIENT_CONST.findall(listing)}
    for const, value in sorted(constants.items()):
        if const in client_constants and client_constants[const] != value:
            result.constant_mismatches.append((const, value, client_constants[const]))

    defined = set(_LUA_ASSIGN.findall(code)) | known
    tokens = set(_TOKEN.findall(code))
    result.unknown_params = sorted(t for t in tokens if _PARAM_LIKE.match(t) and t not in params
                                   and t not in defined)
    result.notes = [line.strip() for line in lua.splitlines() if _NOTE.search(line)]
    return result


def _params(row_path: Path) -> dict[str, int]:
    try:
        row = json.loads(row_path.read_text())
    except (OSError, ValueError):
        return {}
    return {p["name"]: p["value"] for p in row.get("params", []) if isinstance(p.get("value"), int)}


def collect(dumps: Path, scripts: Path) -> tuple[list[Check], list[str]]:
    """Every server script with a client script in `dumps` (kawari-questdump's output folder), checked; and the
    server's custom and opening scripts that have none there."""
    known = shared_names(scripts)
    checks: list[Check] = []
    missing: list[str] = []
    for kind in ("custom", "opening"):
        root = dumps / kind
        index_path = root / "index.json"
        if not index_path.exists():
            continue
        for entry in json.loads(index_path.read_text()):
            server = scripts / entry["server_script"]
            if not server.exists():
                continue
            listing = root / Path(entry["path"]).with_suffix(".txt")
            if not entry.get("found") or not listing.exists():
                missing.append(entry["server_script"])
                continue
            checks.append(check(kind, entry["name"], server, listing.read_text(),
                                _params(listing.with_suffix(".json")), known))
    # Quests, as `kawari-questdump dump` lays them out: <script>/<script>.txt and quest.json.
    for quest_json in sorted(dumps.glob("*/quest.json")):
        folder = quest_json.parent
        name = folder.name
        listing = folder / f"{name}.txt"
        number = name[-5:]
        if not number.isdigit() or not listing.exists():
            continue
        server = scripts / "events" / "quest" / f"{int(number) // 100:03}" / f"{name}.lua"
        if server.exists():
            checks.append(check("quest", name, server, listing.read_text(), _params(quest_json), known))
    return checks, missing


def report(checks: list[Check], missing: list[str], show_all: bool = False, scripts: Path | None = None) -> str:
    lines = ["# Server scripts against the client's", ""]
    flagged = [c for c in checks if c.issues]
    lines.append(f"{len(checks)} server scripts checked against their client scripts; {len(flagged)} need a look"
                 f" (scenes the client doesn't have, constants that differ, parameters the sheet doesn't have).")
    if missing:
        lines.append(f"{len(missing)} server scripts have no client script in the dumps: "
                     + ", ".join(f"`{m}`" for m in missing[:20]) + (" ..." if len(missing) > 20 else ""))
    lines.append("")
    for c in sorted(checks, key=lambda c: (not c.issues, c.kind, c.name)):
        if not (c.issues or show_all or c.notes or c.unplayed):
            continue
        rel = (f"resources/scripts/{c.server_script.relative_to(scripts)}"
               if scripts and c.server_script.is_relative_to(scripts) else c.server_script)
        lines.append(f"## {c.name} ({c.kind}){' - needs a look' if c.issues else ''}")
        lines.append("")
        lines.append(f"`{rel}`: client scenes {c.client_scenes or 'none'}, the server plays {c.played or 'none'}.")
        if c.unknown_scenes:
            lines.append(f"* **Plays scenes the client doesn't have:** {c.unknown_scenes}")
        for const, server, client in c.constant_mismatches:
            lines.append(f"* **{const}** is {server:g} here, {client:g} in the client's script")
        if c.unknown_params:
            lines.append(f"* **Names the sheet doesn't have** (nil in Lua): {', '.join(c.unknown_params)}")
        if c.unplayed:
            lines.append(f"* Client scenes never played: {c.unplayed}")
        for note in c.notes:
            lines.append(f"* Note in the script: `{note}`")
        lines.append("")
    return "\n".join(lines) + "\n"
