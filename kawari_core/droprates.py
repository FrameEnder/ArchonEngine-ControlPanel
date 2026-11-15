"""The server's rates: how often and how much items drop, and how much gil and EXP is given.

The world server reads drop-rates.json from its data folder (beside world.db and glamours.txt;
servers/world/src/server/drop_rates.rs) again whenever it changes, so a change needs no restart. Format 2:

    {"format": 2,
     "items": {"all":        {"all": 100, "group:6": 150, "cat:58": 50, "mounts_minions": 90},
               "expansions": {"0": {"all": 120}},
               "types":      {"2": {"group:3": 200}, "world": {"all": 80}},
               "duties":     {"4": {"all": 300, "mounts_minions": 100}},
               "zones":      {"148": {"cat:45": 0}}},
     "gil": {"all": 100, "kills": 150},
     "exp": {"all": 200, "quests": 100}}

Every rate but ``mounts_minions`` is a multiplier in percent (100: the server's own, retail's where it's known; 0 to
1000). ``mounts_minions`` is the chance in percent (0 to 100) that a mount or minion a coffer may hold drops; 90 when
nothing says otherwise.

Items: a drop happens in a place and is of a kind. The places are scopes here: "all"; "x:N" an expansion; "t:N" a
content type (ContentType row) and "t:world" the open world; "d:N" a duty (ContentFinderCondition); "z:N" an
open-world zone (TerritoryType). The kinds are keys: "all", "group:N" (an ItemUICategory's OrderMajor, the game's own
grouping), "cat:N" (an ItemUICategory) and "mounts_minions". The nearest setting wins: the duty's or zone's own, then
its content type's, then its expansion's, then everywhere's; and in the first of those that says anything for the
item, its category's rate, else its group's, else that place's "all".

Gil and EXP: "all" and a rate for each source that overrides it.

The file of format 1 ({"default": {"mounts_minions": 90}, "duties": {"57": {"mounts_minions": 100}}}, Kawari Web
2.27.0) is read as format 2's items.all and items.duties.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

RATE_FILE = "drop-rates.json"
CATEGORY_FILE = "item-categories.json"
MOUNTS_MINIONS = "mounts_minions"
# The server's own (drop_rates.rs: MOUNTS_MINIONS_PERCENT, MOST_PERCENT.)
MOUNTS_MINIONS_DEFAULT = 90
MOST = 1000
WORLD = "world"

# The sources the server has a rate for (drop_rates.rs: GIL_SOURCES, EXP_SOURCES), with this page's words for them.
GIL_SOURCES = [("kills", "Enemies defeated"), ("quests", "Quest rewards"), ("duties", "Duty clears and roulettes"),
               ("fates", "FATEs")]
EXP_SOURCES = GIL_SOURCES + [("gathering", "Gathering"), ("crafting", "Crafting"), ("hunting_log", "Hunting Log"),
                             ("other", "Anything else a script gives")]

# The file's part for each kind of scope.
PARTS = {"x": "expansions", "t": "types", "d": "duties", "z": "zones"}


def read(path: Path) -> tuple[dict, float | None]:
    """The file as format 2 (an empty one when it isn't there or isn't JSON), and when it was written."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        modified = Path(path).stat().st_mtime
    except (OSError, ValueError):
        return normal({}), None
    return normal(data if isinstance(data, dict) else {}), modified


def rate(value, most: int = MOST) -> int | None:
    """A rate typed in a form or found in the file: a whole percent from 0 to `most`, or None for blank or nonsense."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().rstrip("%").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0, min(most, round(number)))


def valid_key(key: str) -> bool:
    if key in ("all", MOUNTS_MINIONS):
        return True
    kind, _, number = key.partition(":")
    return kind in ("group", "cat") and number.isdigit()


def _setting(raw) -> dict[str, int]:
    """One place's rates, cleaned."""
    out = {}
    for key, value in (raw.items() if isinstance(raw, dict) else ()):
        if valid_key(str(key)):
            number = rate(value, 100 if key == MOUNTS_MINIONS else MOST)
            if number is not None:
                out[str(key)] = number
    return out


def normal(data: dict) -> dict:
    """Any file as format 2, with only what the server reads."""
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    out = {"format": 2, "items": {"all": _setting(items.get("all")), "expansions": {}, "types": {}, "duties": {},
                                  "zones": {}}, "gil": {}, "exp": {}}
    for part in PARTS.values():
        for key, raw in (items.get(part) or {}).items() if isinstance(items.get(part), dict) else ():
            key = str(key)
            if key.isdigit() or (part == "types" and key == WORLD):
                setting = _setting(raw)
                if setting:
                    out["items"][part][key] = setting
    # Format 1.
    for key, value in _setting(data.get("default")).items():
        out["items"]["all"].setdefault(key, value)
    for key, raw in (data.get("duties") or {}).items() if isinstance(data.get("duties"), dict) else ():
        setting = _setting(raw)
        if str(key).isdigit() and setting:
            merged = out["items"]["duties"].setdefault(str(key), {})
            for name, value in setting.items():
                merged.setdefault(name, value)
    for kind, sources in (("gil", GIL_SOURCES), ("exp", EXP_SOURCES)):
        raw = data.get(kind) if isinstance(data.get(kind), dict) else {}
        for key in ["all"] + [source for source, _ in sources]:
            number = rate(raw.get(key))
            if number is not None:
                out[kind][key] = number
    return out


def setting_of(data: dict, scope: str) -> dict[str, int]:
    """A scope's own rates ("all", "x:0", "t:2", "t:world", "d:4", "z:148")."""
    if scope == "all":
        return data["items"]["all"]
    kind, _, key = scope.partition(":")
    return data["items"].get(PARTS.get(kind, ""), {}).get(key, {})


def set_rate(data: dict, scope: str, key: str, value: int | None) -> None:
    """Sets (or with None, clears) one rate; an emptied place is dropped."""
    if scope == "all":
        setting = data["items"]["all"]
    else:
        kind, _, name = scope.partition(":")
        setting = data["items"][PARTS[kind]].setdefault(name, {})
    if value is None:
        setting.pop(key, None)
    else:
        setting[key] = value
    if scope != "all" and not setting:
        del data["items"][PARTS[kind]][name]


def valid_scope(scope: str) -> bool:
    if scope == "all":
        return True
    kind, _, name = scope.partition(":")
    return kind in PARTS and (name.isdigit() or (kind == "t" and name == WORLD))


def chain(scope: str, content_type: int | str | None = None, expansion: int | None = None) -> list[str]:
    """The scopes a drop in `scope` looks through, nearest first (the server's order): a duty's or zone's own, its
    content type's (the open world's for a zone), its expansion's, everywhere's."""
    kind = scope.partition(":")[0]
    out = [scope] if scope != "all" else []
    if kind in ("d", "z"):
        out.append(f"t:{WORLD if kind == 'z' else content_type}")
        out.append(f"x:{expansion}")
    out.append("all")
    return out


def effective(data: dict, scopes: list[str], key: str) -> tuple[int, str]:
    """The rate `key` comes to through `scopes` (nearest first) and the scope that set it ("" for the server's own):
    in the first scope that says anything for it, the key's own rate, else (for a category) its group's, else (for a
    category or group) that scope's "all". `key` for a category is "cat:N/group:M"."""
    if key == MOUNTS_MINIONS:
        for scope in scopes:
            value = setting_of(data, scope).get(MOUNTS_MINIONS)
            if value is not None:
                return value, scope
        return MOUNTS_MINIONS_DEFAULT, ""
    wanted = key.split("/") + ([] if key == "all" else ["all"])
    for scope in scopes:
        setting = setting_of(data, scope)
        for name in wanted:
            if name in setting:
                return setting[name], scope
    return 100, ""


def write(path: Path, data: dict) -> None:
    """Written beside the file and renamed over it, so the server never reads half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = normal(data)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".drop-rates-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=1, sort_keys=True)
        out.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def count(data: dict) -> dict[str, int]:
    """How many rates the file has, by part (for the page's summary)."""
    items = data["items"]
    return {
        "everywhere": len(items["all"]),
        "expansions": sum(len(s) for s in items["expansions"].values()),
        "types": sum(len(s) for s in items["types"].values()),
        "duties": sum(len(s) for s in items["duties"].values()),
        "zones": sum(len(s) for s in items["zones"].values()),
        "gil": len(data["gil"]),
        "exp": len(data["exp"]),
    }


def load_categories(root: Path) -> list[dict]:
    """item-categories.json (kawari-questdump items): every ItemUICategory with its name and its place in the game's
    own order, sorted by it. Empty when it hasn't been written."""
    try:
        raw = json.loads((Path(root) / CATEGORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            out.append({"id": int(entry["id"]), "name": str(entry.get("name", "")), "major": int(entry.get("major", 0)),
                        "minor": int(entry.get("minor", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda c: (c["major"], c["minor"], c["id"]))


def category_groups(categories: list[dict]) -> list[dict]:
    """The categories by the game's groups (OrderMajor), in order: {"major", "categories"}."""
    groups: dict[int, list[dict]] = {}
    for category in categories:
        groups.setdefault(category["major"], []).append(category)
    return [{"major": major, "categories": groups[major]} for major in sorted(groups)]
