"""Quest Lab: the quest-scripting loop, around kawari-questdump.

The loop this supports is: dump a quest from the game (kawari-questdump, in the
fork), read what the client does, script it in Lua, build, put a character at
the right point in the quest, test, repeat. Everything here is plain Python
over files and one database row, so it can be tested without a server:

* **Dumps** are folders kawari-questdump wrote under ``<data>/quest-dumps/``:
  ``<script>/quest.json``, ``<script>.luab`` and ``<script>.txt`` (the listing).
* **The quest index** is ``quest-index.json`` from ``kawari-questdump index``:
  the whole quest graph, used for search and for prerequisites. Optional - a
  build of the fork without that command still gets everything else, with
  prerequisites limited to what has been dumped.
* **Scripts** are read from the ACTIVE BUILD, not a checkout:
  ``resources/scripts/events/quest/<id // 100>/<script>.lua``. What is checked
  is what the running server actually has.
* **Quest state** lives in two world.db columns: ``quest.completed``, a JSON
  byte array where quest N is bit ``0x80 >> (N % 8)`` of byte ``N // 8``
  (Kawari's QuestBitmaskTransformation - most significant bit first, unlike
  every other bitmask), and ``quest.active``, a JSON list of
  ``{"id", "sequence", "variables": [6 bytes]}``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# A story quest's script name carries its area: ManFst005 is Gridania (Forest),
# SubSea is Limsa Lominsa, ManWil is Ul'dah. Used to choose between "any one
# of" prerequisites so a Gridania character is not given Limsa's quests.
_REGION = re.compile(r"^[A-Z][a-z]{2}([A-Z][a-z]{2})")
SEQUENCE_FINISH = 255
JOIN_ANY = 2


# ════════════════════════════════════════════════════════════════════════════
#  Dumps
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class QuestDump:
    script: str
    folder: Path
    quest: dict

    @property
    def quest_id(self) -> int:
        return int(self.quest.get("quest_id", 0))

    @property
    def name(self) -> str:
        return self.quest.get("name", "")

    @property
    def level(self) -> int:
        return int(self.quest.get("level", 0) or 0)

    @property
    def luab(self) -> Path:
        return self.folder / f"{self.script}.luab"

    @property
    def listing_path(self) -> Path:
        return self.folder / f"{self.script}.txt"

    def listing(self) -> str:
        try:
            return self.listing_path.read_text(errors="replace")
        except OSError:
            return ""

    def files(self) -> list[Path]:
        return sorted(p for p in self.folder.iterdir() if p.is_file())


def dumps_root(data_dir) -> Path:
    return Path(data_dir) / "quest-dumps"


def scan_dumps(root: Path) -> list[QuestDump]:
    """Every dumped quest, in story order (level, then quest ID)."""
    out = []
    if not root.is_dir():
        return out
    for folder in root.iterdir():
        qfile = folder / "quest.json"
        if not qfile.is_file():
            continue
        try:
            quest = json.loads(qfile.read_text())
        except (OSError, ValueError):
            continue
        out.append(QuestDump(script=quest.get("script") or folder.name,
                             folder=folder, quest=quest))
    out.sort(key=lambda d: (d.level, d.quest_id))
    return out


def find_dump(root: Path, quest_id: int) -> QuestDump | None:
    for dump in scan_dumps(root):
        if dump.quest_id == quest_id:
            return dump
    return None


def load_index(root: Path) -> dict[int, dict]:
    """quest-index.json as {quest_id: entry}; empty when it has not been made."""
    path = root / "quest-index.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {int(q["id"]): q for q in data.get("quests", []) if "id" in q}


def search(index: dict[int, dict], dumps: list[QuestDump], query: str,
           limit: int = 60) -> list[dict]:
    """Quests matching an ID, script name or part of a name.

    Searches the index when there is one, else the dumps - so search works
    before the first ``index`` run, just over less.
    """
    query = query.strip()
    if not query:
        return []
    pool = list(index.values()) if index else [
        {"id": d.quest_id, "script": d.script, "name": d.name, "level": d.level,
         "place_name": d.quest.get("place") or ""} for d in dumps]
    if query.isdigit():
        number = int(query)
        number = number - 65536 if number >= 65536 else number
        return [q for q in pool if int(q["id"]) == number][:limit]
    low = query.lower()
    exact = [q for q in pool if q.get("script", "").lower() == low
             or q.get("name", "").lower() == low]
    partial = [q for q in pool if q not in exact and (
        low in q.get("name", "").lower() or low in q.get("script", "").lower())]
    partial.sort(key=lambda q: (int(q.get("level") or 0), int(q["id"])))
    return (exact + partial)[:limit]


# ════════════════════════════════════════════════════════════════════════════
#  The fork's scripts
# ════════════════════════════════════════════════════════════════════════════
def script_rel_path(script: str) -> str | None:
    """events/quest/<id // 100>/<script>.lua - the same folder rule the client uses."""
    tail = script[-5:]
    if len(script) < 5 or not tail.isdigit():
        return None
    return f"events/quest/{int(tail) // 100:03d}/{script}.lua"


def find_script(build_dir: Path | None, script: str) -> Path | None:
    if build_dir is None:
        return None
    rel = script_rel_path(script)
    if rel is None:
        return None
    path = Path(build_dir) / "resources" / "scripts" / rel
    return path if path.is_file() else None


_LUA_ASSIGN = re.compile(r"^\s*(?:local\s+)?([A-Z][A-Z0-9_]*)\s*(?:<const>)?\s*=(?!=)", re.M)
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9_]*\b")
# Quest-sheet names all carry an index: ACTOR0, EOBJECT3, SEQ_1_ACTOR2,
# SEQ_1_EOBJECT0_EVENTACTIONOK, QUESTBATTLE0, ITEM1 ...
_PARAM_LIKE = re.compile(r"^[A-Z][A-Z_]*\d")
_SCENE_FN = re.compile(r":OnScene(\d+)\b")
_PLAY_ARG = re.compile(r"\b(?:questPlay|play_scene)\s*\(\s*[\w.:]+\s*,\s*([A-Za-z_]\w*|\d+)")
_NUM_CONST = re.compile(r"^\s*(?:local\s+)?([A-Z][A-Z0-9_]*)\s*(?:<const>)?\s*=\s*(\d+)\s*$", re.M)


def strip_lua(text: str) -> str:
    """Lua with comments and string literals removed, for token scanning."""
    text = re.sub(r"--\[(=*)\[.*?\]\1\]", " ", text, flags=re.S)   # block comments
    text = re.sub(r"--[^\n]*", " ", text)                           # line comments
    text = re.sub(r"\[(=*)\[.*?\]\1\]", '""', text, flags=re.S)    # long strings
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", text)
    return text


def global_names(build_dir: Path | None) -> set[str]:
    """Names the server's shared scripts define, so they are not mistaken for
    quest variables the Quest sheet should have."""
    names: set[str] = set()
    if build_dir is None:
        return names
    scripts = Path(build_dir) / "resources" / "scripts"
    for rel in ("Global.lua", "Init.lua"):
        try:
            names |= set(_LUA_ASSIGN.findall(strip_lua((scripts / rel).read_text())))
        except OSError:
            pass
    return names


def header_notes(lua_text: str) -> tuple[str, list[str], list[str]]:
    """(title, outline lines, NOTE lines) from the comment block at the top.

    The fork's quest scripts open with ``-- Quest: Name (Lv. N)``, an outline of
    the sequences, and ``-- NOTE:`` lines for anything known to be off.
    """
    title, outline, notes = "", [], []
    for line in lua_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        body = stripped[2:].strip()
        if body.lower().startswith("quest:"):
            title = body[6:].strip()
        elif body.upper().startswith("NOTE:"):
            notes.append(body[5:].strip())
        elif body:
            outline.append(body)
    return title, outline, notes


@dataclass
class Analysis:
    scripted: bool
    title: str = ""
    outline: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    unverified: bool = False
    client_scenes: list = field(default_factory=list)   # OnSceneNNNNN in the client script
    played: list = field(default_factory=list)          # scene numbers the Lua refers to
    unplayed: list = field(default_factory=list)        # client scenes the Lua never names
    unknown_scenes: list = field(default_factory=list)  # named by the Lua, absent from the client
    unknown_names: list = field(default_factory=list)   # quest variables the Quest sheet lacks
    used_params: list = field(default_factory=list)

    @property
    def status(self) -> str:
        """none | issues | unverified | scripted - the worst that applies."""
        if not self.scripted:
            return "none"
        if self.unknown_names or self.unknown_scenes:
            return "issues"
        if self.unverified:
            return "unverified"
        return "scripted"


STATUS_LABELS = {
    "none": "not scripted",
    "issues": "needs a look",
    "unverified": "unverified",
    "scripted": "scripted",
}


def analyse(quest: dict, listing: str, lua_text: str | None,
            known_globals: set[str] | None = None) -> Analysis:
    """Check a script against its dump.

    Two things are worth knowing before going in-game. A quest variable the
    script names that the Quest sheet does not have is a typo, or a script
    written against a different game version - in Lua it is silently nil. And
    a client scene the script never plays is either intentionally skipped, or
    a branch that was missed.
    """
    params = {p.get("name", ""): p.get("value") for p in quest.get("params", [])}
    client = sorted({int(n) for n in _SCENE_FN.findall(listing)})
    if lua_text is None:
        return Analysis(scripted=False, client_scenes=client, unplayed=client)

    title, outline, notes = header_notes(lua_text)
    code = strip_lua(lua_text)
    tokens = set(_TOKEN.findall(code))
    defined = set(_LUA_ASSIGN.findall(code)) | (known_globals or set())

    used = sorted(t for t in tokens if t in params)
    unknown = sorted(t for t in tokens if _PARAM_LIKE.match(t) and t not in params
                     and t not in defined)
    # A scene counts as played when the script names it. Three ways appear in
    # the fork: the Quest sheet's SEQ_* variables, literal numbers, and - where
    # the sheet does not name a quest's scenes, so the numbers came from
    # Sapphire - local constants such as SCENE_ACCEPT = 0.
    constants = {name: int(value) for name, value in _NUM_CONST.findall(code)}
    played = set()
    for name in used:
        if name.startswith("SEQ_") and isinstance(params.get(name), int):
            played.add(int(params[name]))
    for name, value in constants.items():
        if name.startswith("SCENE"):
            played.add(value)
    for arg in _PLAY_ARG.findall(code):
        if arg.isdigit():
            played.add(int(arg))
        elif arg in constants:
            played.add(constants[arg])
        elif isinstance(params.get(arg), int) and arg.startswith("SEQ_"):
            played.add(int(params[arg]))

    joined = " ".join(notes).lower()
    return Analysis(
        scripted=True, title=title, outline=outline, notes=notes,
        unverified="unverified" in joined or "unverified" in " ".join(outline).lower(),
        client_scenes=client,
        played=sorted(played),
        unplayed=[n for n in client if n not in played],
        unknown_scenes=sorted(n for n in played if client and n not in client),
        unknown_names=unknown,
        used_params=used,
    )


def gm_commands(quest: dict) -> list[tuple[str, str]]:
    """(label, command) pairs for testing this quest live, without relogging.

    These are the client's own //gm commands, which Kawari implements as GM
    scripts - they act on the logged-in character immediately, where a
    database edit needs the character logged out.
    """
    qid = quest.get("quest_id", 0)
    sequences = sorted({int(t.get("complete_sequence", 0)) for t in quest.get("todos", [])
                        if t.get("complete_sequence")} - {0, SEQUENCE_FINISH})
    out = [("Accept", f"//gm quest accept {qid}")]
    for seq in sequences or [1]:
        out.append((f"Sequence {seq}", f"//gm quest sequence {qid} {seq}"))
    out += [("Ready to turn in", f"//gm quest sequence {qid} {SEQUENCE_FINISH}"),
            ("Complete", f"//gm quest complete {qid}"),
            ("Un-complete", f"//gm quest incomplete {qid}"),
            ("Abandon", f"//gm quest cancel {qid}"),
            ("Inspect", f"//gm quest inspect {qid}")]
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Prerequisites
# ════════════════════════════════════════════════════════════════════════════
def region_of(script: str) -> str:
    m = _REGION.match(script or "")
    return m.group(1) if m else ""


def graph(index: dict[int, dict], dumps: list[QuestDump]) -> dict[int, dict]:
    """The quest graph to walk: the index, topped up with any dumped quest the
    index lacks (or everything dumped, when there is no index)."""
    out = dict(index)
    for d in dumps:
        if d.quest_id not in out:
            out[d.quest_id] = {
                "id": d.quest_id, "script": d.script, "name": d.name,
                "level": d.level, "previous": d.quest.get("previous_quests", []),
                "previous_join": d.quest.get("previous_join", 0),
            }
    return out


def prerequisites(graph_: dict[int, dict], quest_id: int,
                  already_done=frozenset()) -> tuple[list[int], list[int]]:
    """The quests to complete before ``quest_id`` can be accepted.

    Returns (to_complete, unknown): to_complete is ordered dependencies-first;
    unknown are prerequisites the graph has no entry for (so their own
    prerequisites cannot be followed - build the index to fix that).

    "Any one of" prerequisites (PreviousQuestJoin 2) take one branch: one the
    character has already done if there is one, else one from the same area
    as the target, else the first. Completing every branch would hand a
    Gridania character Limsa's and Ul'dah's story too.
    """
    target = graph_.get(quest_id, {})
    home = region_of(target.get("script", ""))
    order: list[int] = []
    unknown: list[int] = []
    chosen: set[int] = set()
    visiting: set[int] = set()

    def visit(qid: int):
        if qid in chosen or qid in visiting:
            return
        if qid in already_done and qid != quest_id:
            # Done is done: do not reach past it and complete what came before
            # (a character GM-completed to this point never did those).
            chosen.add(qid)
            return
        entry = graph_.get(qid)
        if entry is None:
            if qid not in unknown:
                unknown.append(qid)
            return
        visiting.add(qid)
        prev = [int(p) for p in entry.get("previous", []) if int(p)]
        if int(entry.get("previous_join") or 0) == JOIN_ANY and len(prev) > 1:
            if not any(p in already_done or p in chosen for p in prev):
                same_area = [p for p in prev
                             if region_of(graph_.get(p, {}).get("script", "")) == home]
                prev = [(same_area or prev)[0]]
            else:
                prev = []
        for p in prev:
            visit(p)
        visiting.discard(qid)
        if qid != quest_id:
            chosen.add(qid)
            if qid not in already_done:
                order.append(qid)

    visit(quest_id)
    return order, unknown


def successors(graph_: dict[int, dict], quest_id: int) -> list[dict]:
    return sorted((q for q in graph_.values() if quest_id in [int(p) for p in q.get("previous", [])]),
                  key=lambda q: (int(q.get("level") or 0), int(q["id"])))


# ════════════════════════════════════════════════════════════════════════════
#  Quest state on a character
# ════════════════════════════════════════════════════════════════════════════
def _completed_bytes(ch) -> list[int]:
    from . import chardata
    data = list(ch.get_json("quest", "completed", []) or [])
    want = chardata.bitmask_length("quest", "completed") or len(data)
    if len(data) < want:
        data += [0] * (want - len(data))
    return data


def is_completed(ch, quest_id: int) -> bool:
    data = ch.get_json("quest", "completed", []) or []
    byte, mask = divmod(quest_id, 8)
    return byte < len(data) and bool(data[byte] & (0x80 >> mask))


def completed_set(ch, candidates) -> set[int]:
    return {q for q in candidates if is_completed(ch, q)}


def set_completed(ch, quest_ids, done: bool = True) -> int:
    """Mark quests completed (or not). Returns how many actually changed."""
    data = _completed_bytes(ch)
    changed = 0
    for qid in quest_ids:
        byte, bit = divmod(int(qid), 8)
        if byte >= len(data):
            raise ValueError(f"quest {qid} is past the end of the completed-quest "
                             f"bitmask ({len(data)} bytes) - is the schema map current?")
        mask = 0x80 >> bit
        before = data[byte]
        data[byte] = (before | mask) if done else (before & ~mask)
        changed += data[byte] != before
    if changed:
        ch.set_json("quest", "completed", data)
    return changed


def active_quests(ch) -> list[dict]:
    return list(ch.get_json("quest", "active", []) or [])


def set_active(ch, quest_id: int, sequence: int) -> None:
    """Put a quest in the journal at ``sequence``, with its variables cleared."""
    if not 0 <= sequence <= 255:
        raise ValueError("a sequence is 0-255 (255 is ready to turn in)")
    quests = [q for q in active_quests(ch) if int(q.get("id", -1)) != quest_id]
    quests.append({"id": quest_id, "sequence": int(sequence), "variables": [0] * 6})
    ch.set_json("quest", "active", quests)


def remove_active(ch, quest_id: int) -> bool:
    quests = active_quests(ch)
    kept = [q for q in quests if int(q.get("id", -1)) != quest_id]
    if len(kept) != len(quests):
        ch.set_json("quest", "active", kept)
        return True
    return False


def raise_level(ch, level: int) -> str:
    """Bring the character's current class up to ``level`` if it is below it."""
    from . import gamedata as G
    current = int(ch.get("classjob", "current_class", 0) or 0)
    slot = G.CLASSJOB_EXP_INDEX.get(current)
    levels = list(ch.get_json("classjob", "levels", []) or [])
    if slot is None or slot >= len(levels):
        return ""
    if int(levels[slot]) >= level:
        return ""
    old = int(levels[slot])
    levels[slot] = level
    ch.set_json("classjob", "levels", levels)
    exps = list(ch.get_json("classjob", "exp", []) or [])
    if slot < len(exps):
        exps[slot] = 0
        ch.set_json("classjob", "exp", exps)
    return f"raised the current class from {old} to {level}"


STATES = {
    "available": "Ready to accept (prerequisites done, quest not started)",
    "active": "In progress, at a chosen sequence",
    "completed": "Completed",
    "reset": "Never started (removed from the journal and the completed list)",
}


def prepare(ch, graph_: dict[int, dict], quest_id: int, state: str, sequence: int = 1,
            with_prerequisites: bool = True, level_up: bool = True) -> list[str]:
    """Put a character at a point in a quest. Returns what was done, in words."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    done: list[str] = []
    if with_prerequisites and state != "reset":
        candidates = set(graph_)
        order, unknown = prerequisites(graph_, quest_id, completed_set(ch, candidates))
        if order:
            set_completed(ch, order, True)
            done.append(f"completed {len(order)} prerequisite quest(s)")
        if unknown:
            done.append(f"{len(unknown)} prerequisite(s) are not in the quest index, so "
                        f"their own prerequisites were not followed: "
                        f"{', '.join(map(str, unknown[:8]))}")
            set_completed(ch, unknown, True)
    if level_up and state != "reset":
        level = int(graph_.get(quest_id, {}).get("level") or 0)
        if level:
            note = raise_level(ch, level)
            if note:
                done.append(note)

    if state == "available":
        set_completed(ch, [quest_id], False)
        if remove_active(ch, quest_id):
            done.append("removed it from the journal")
        done.append("left it ready to accept")
    elif state == "active":
        set_completed(ch, [quest_id], False)
        set_active(ch, quest_id, sequence)
        done.append(f"accepted it at sequence {sequence}")
    elif state == "completed":
        remove_active(ch, quest_id)
        set_completed(ch, [quest_id], True)
        done.append("marked it completed")
    elif state == "reset":
        remove_active(ch, quest_id)
        set_completed(ch, [quest_id], False)
        done.append("removed it from the journal and the completed list")
    return done
