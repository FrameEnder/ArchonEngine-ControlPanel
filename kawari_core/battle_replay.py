"""Battle replays: a retail battle and a Kawari one on the same top-down map (Dashboard -> Battle replay).

Two kinds of source, read into one shape:

* a battle run, Kawari Recorder's (sent to /api/diag/battle) or ``questkit chronofoil --battle``'s, which write the
  same format (Zone, TerritoryId, Actors, Samples, Actions, Events...);
* a Kawari packet capture (the world server's JSONL, servers/world/src/capture.rs), cut into its zone visits at each
  ZoneInit and read from Kawari's own decoding of each packet (its ``decoded`` text).

A replay (format 1), times in seconds from the start of the run or the visit:

    {"format": 1, "label", "source", "territory", "content", "duration", "you",
     "actors": {"<id>": {"kind", "side", "base", "name_id", "layout", "battalion", "level", "max_hp"}},
     "moves":  {"<id>": [[t, x, z, rotation or null], ...]},
     "hp":     {"<id>": [[t, hp], ...]},
     "casts":  [{"t", "src", "action", "target", "time", "x", "z", "r", "cancelled": t or null}],
     "hits":   [{"t", "src", "action", "targets": [{"id", "damage", "heal", "knockback", "missed"}]}],
     "deaths": [[t, id]], "gone": [[t, id]], "marks": [[t, text]], "events": [[t, kind, text]],
     "actions": {"<id>": {"name", "cast", "shape"...}}, "knockbacks": {"<id>": {"distance"...}}}

``actions`` and ``knockbacks`` come from the game install (``kawari-questdump actions`` -> action-index.json), so
no action names are written here; without the index the casts are drawn as dots with their IDs.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

FORMAT = 1

# Effect kinds, as both the client and Kawari number them (ActionEffect's 8-byte entries.)
EFFECT_MISS = 1
DAMAGE_EFFECTS = (3, 5, 6)
EFFECT_HEAL = 4
EFFECT_KNOCKBACK = 31

# The retail cast event's text, as Kawari Recorder and `questkit chronofoil --battle` write it.
CAST_TEXT = re.compile(
    r"action (\d+) spell \d+ type (\d+) on (\d+) ([\d.]+)s at (-?[\d.]+), (-?[\d.]+), (-?[\d.]+) facing (-?[\d.]+)")

# Kawari's decoded text.
NUMBER = r"(-?[\d.]+(?:e-?\d+)?)"
POSITION = re.compile(r"position: Position\(Vec3A\(" + NUMBER + ", " + NUMBER + ", " + NUMBER + r"\)\)")
ROTATION = re.compile(r"\brotation: " + NUMBER)
OBJECT_ID = re.compile(r"ObjectId \((\d+)\)")
TARGET_EFFECT = re.compile(r"TargetEffect\((\w+)(?: \{([^}]*)\})?\)")
FIELD = {name: re.compile(r"\b" + name + r": (\d+)") for name in (
    "base_id", "name_id", "layout_id", "max_health_points", "health_points", "battalion", "level", "action_id",
    "hp", "current_hp", "territory_type", "content_finder_condition_id", "flag", "amount", "knockback_id")}
CAST_TIME = re.compile(r"cast_time: " + NUMBER)
CAST_TARGET = re.compile(r"\btarget: ObjectId \((\d+|INVALID_ACTOR)\)")
OBJECT_KIND = re.compile(r"object_kind: (\w+)(?:\((\w+)\))?")
CONTROL = re.compile(r"^ActorControl(?:Self|Target)?\(\w+ \{ category: (\w+)")
HANDLER = re.compile(r"handler_id: HandlerId \{ type: (\w+)[^,]*, id: (\d+) \}")

MOVED_BY_CLIENT = ("UpdatePositionHandler", "UpdatePositionHandlerInstance")


def _int(pattern: re.Pattern, text: str, default: int = 0) -> int:
    match = pattern.search(text)
    return int(match.group(1)) if match else default


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _side(kind: str, battalion: int, you_battalion: int) -> str:
    if kind == "you":
        return "you"
    if kind == "player":
        return "ally"
    if "battle" in kind:
        return "ally" if battalion == you_battalion else "enemy"
    return "other"


def _empty(label: str, source: str) -> dict:
    return {"format": FORMAT, "label": label, "source": source, "territory": 0, "content": 0, "duration": 0.0,
            "you": None, "actors": {}, "moves": {}, "hp": {}, "casts": [], "hits": [], "deaths": [], "gone": [],
            "marks": [], "events": []}


def _finish(replay: dict) -> dict:
    """Sides from battalions (yours is the player's), sorted lists, the duration."""
    you = replay["you"]
    you_battalion = replay["actors"].get(str(you), {}).get("battalion", 0) if you is not None else 0
    for actor in replay["actors"].values():
        actor["side"] = _side(actor["kind"], actor.get("battalion", 0), you_battalion)
    times = [0.0]
    for track in replay["moves"].values():
        track.sort(key=lambda m: m[0])
        times.append(track[-1][0] if track else 0)
    for track in replay["hp"].values():
        track.sort(key=lambda h: h[0])
    for key in ("casts", "hits"):
        replay[key].sort(key=lambda e: e["t"])
        times += [e["t"] for e in replay[key][-1:]]
    for key in ("deaths", "gone", "marks", "events"):
        replay[key].sort(key=lambda e: e[0])
        times += [e[0] for e in replay[key][-1:]]
    replay["duration"] = _round(max(times), 3)
    return replay


# ════════════════════════════════════════════════════════════════════════════
#  Battle runs (Kawari Recorder, questkit chronofoil --battle)
# ════════════════════════════════════════════════════════════════════════════
def is_battle_run(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("Samples"), list) and isinstance(data.get("Actors"), dict)


def read_battle_file(path: Path) -> dict:
    """A battle file, gzipped or not; a Recorder 2.4 battles.json (a list of runs) gives its first."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, list) and data:
        data = data[0]
    if not is_battle_run(data):
        raise ValueError("not a battle run (no Actors and Samples)")
    return data


def _effects(effects: list) -> dict:
    """What a target's effect entries ([kind, value, ...]) did to it."""
    out = {"damage": 0, "heal": 0, "knockback": None, "missed": False}
    for effect in effects or []:
        if not effect:
            continue
        kind, value = effect[0], (effect[1] if len(effect) > 1 else 0)
        if kind in DAMAGE_EFFECTS:
            out["damage"] += value
        elif kind == EFFECT_HEAL:
            out["heal"] += value
        elif kind == EFFECT_KNOCKBACK:
            out["knockback"] = value
        elif kind == EFFECT_MISS:
            out["missed"] = True
    return out


def from_battle(run: dict, label: str = "") -> dict:
    replay = _empty(label or run.get("Content") or run.get("Zone") or "battle", run.get("Source") or "battle run")
    replay["territory"] = int(run.get("TerritoryId") or 0)
    replay["content"] = int(run.get("ContentFinderCondition") or 0)
    for key, actor in run["Actors"].items():
        kind = actor.get("Kind") or "?"
        if kind == "you":
            replay["you"] = int(key)
        replay["actors"][str(key)] = {
            "kind": kind, "base": actor.get("BaseId", 0), "name_id": actor.get("NameId", 0),
            "layout": actor.get("LayoutId", 0), "battalion": actor.get("Battalion", 0),
            "level": actor.get("Level", 0), "max_hp": actor.get("MaxHp", 0),
            "hitbox": actor.get("HitboxRadius")}
        if actor.get("Died") is not None:
            replay["deaths"].append([actor["Died"], int(key)])
        if actor.get("Position"):
            p = actor["Position"]
            replay["moves"].setdefault(str(key), []).append(
                [actor.get("Seen", 0), _round(p[0]), _round(p[2]), actor.get("Rotation")])
    last_seen = {}
    previous_cast: dict[int, int] = {}
    for sample in run["Samples"]:
        t, actor = sample["S"], str(sample["Id"])
        last_seen[actor] = t
        if sample.get("P"):
            p = sample["P"]
            replay["moves"].setdefault(actor, []).append([t, _round(p[0]), _round(p[2]), sample.get("R")])
        elif sample.get("R") is not None:
            replay["moves"].setdefault(actor, []).append([t, None, None, sample["R"]])
        if sample.get("Hp") is not None:
            replay["hp"].setdefault(actor, []).append([t, sample["Hp"]])
        # Recorder's samples carry what an actor is casting; a cast that starts shows as a new Cast.
        cast = sample.get("Cast") or 0
        if cast and previous_cast.get(sample["Id"]) != cast and not any(
                e.get("Kind") == "cast" for e in run.get("Events") or []):
            p = sample.get("P") or [None, None, None]
            replay["casts"].append({"t": t, "src": sample["Id"], "action": cast,
                                    "target": sample.get("CastTarget") or 0, "time": sample.get("CastTime") or 0,
                                    "x": p[0], "z": p[2], "r": sample.get("R"), "cancelled": None})
        previous_cast[sample["Id"]] = cast
    for event in run.get("Events") or []:
        if event.get("Kind") == "cast":
            match = CAST_TEXT.match(event.get("Text") or "")
            if match:
                action, _type, target, time, x, _y, z, facing = match.groups()
                replay["casts"].append({"t": event["S"], "src": event["Id"], "action": int(action),
                                        "target": int(target), "time": float(time), "x": float(x), "z": float(z),
                                        "r": float(facing), "cancelled": None})
        else:
            replay["events"].append([event["S"], event.get("Kind") or "", event.get("Text") or ""])
    for action in run.get("Actions") or []:
        replay["hits"].append({
            "t": action["S"], "src": action.get("Source"), "action": action.get("Action"),
            "targets": [{"id": target.get("Id"), **_effects(target.get("Effects"))}
                        for target in action.get("Targets") or []]})
        for target in action.get("Targets") or []:
            if target.get("Hp") is not None:
                replay["hp"].setdefault(str(target["Id"]), []).append([action["S"], target["Hp"]])
    end = max(last_seen.values(), default=0)
    for key, actor in run["Actors"].items():
        if actor.get("LastSeen") is not None and actor["LastSeen"] < end - 1:
            replay["gone"].append([actor["LastSeen"], int(key)])
    return _finish(replay)


# ════════════════════════════════════════════════════════════════════════════
#  Kawari packet captures
# ════════════════════════════════════════════════════════════════════════════
def _lines(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def capture_visits(path: Path) -> list[dict]:
    """The zone visits in a capture: one per ZoneInit (and what came before the first, if anything did)."""
    visits: list[dict] = []
    current = None
    for packet in _lines(path):
        if packet.get("dir") == "out" and packet.get("name") == "ZoneInit":
            text = packet.get("decoded") or ""
            current = {"index": len(visits), "territory": _int(FIELD["territory_type"], text),
                       "content": _int(FIELD["content_finder_condition_id"], text), "start": packet["t"],
                       "end": packet["t"], "packets": 0, "spawns": 0, "hits": 0, "casts": 0, "marks": 0}
            visits.append(current)
        if current is None:
            current = {"index": 0, "territory": 0, "content": 0, "start": packet.get("t", 0),
                       "end": packet.get("t", 0), "packets": 0, "spawns": 0, "hits": 0, "casts": 0, "marks": 0}
            visits.append(current)
        current["packets"] += 1
        current["end"] = packet.get("t", current["end"])
        name = packet.get("name") or ""
        if name in ("SpawnNpc", "SpawnPlayer"):
            current["spawns"] += 1
        elif name == "ActorCast":
            current["casts"] += 1
        elif name.startswith(("ActionEffect", "AoeEffect")):
            current["hits"] += 1
        elif packet.get("dir") == "note" and (packet.get("text") or "").startswith("MARK"):
            current["marks"] += 1
    for visit in visits:
        visit["seconds"] = round((visit["end"] - visit["start"]) / 1000, 1)
    return visits


def _spawn(replay: dict, packet: dict, t: float) -> None:
    text = packet.get("decoded") or ""
    actor = packet.get("src")
    common = text[text.find("common: CommonSpawn"):] if "common: CommonSpawn" in text else text
    kind_match = OBJECT_KIND.search(common)
    kind = "?"
    if kind_match:
        main, sub = kind_match.groups()
        if main == "Player":
            kind = "you" if actor == replay["you"] else "player"
        elif main == "BattleNpc":
            kind = "battle npc"
        else:
            kind = re.sub(r"(?<!^)(?=[A-Z])", " ", main).lower()
    if actor == replay["you"]:
        kind = "you"
    replay["actors"][str(actor)] = {
        "kind": kind, "base": _int(FIELD["base_id"], common), "name_id": _int(FIELD["name_id"], common),
        "layout": _int(FIELD["layout_id"], common), "battalion": _int(FIELD["battalion"], common),
        "level": _int(FIELD["level"], common), "max_hp": _int(FIELD["max_health_points"], common)}
    positions = POSITION.findall(text)
    rotation = ROTATION.search(common)
    if positions:
        x, _y, z = positions[-1]
        replay["moves"].setdefault(str(actor), []).append(
            [t, _round(x), _round(z), _round(rotation.group(1), 3) if rotation else None])
    replay["hp"].setdefault(str(actor), []).append([t, _int(FIELD["health_points"], common)])


def _target_effects(text: str) -> list[dict]:
    """Each target's effects from an ActionEffect1 (one list) or an AoeEffectN (a list per target.)"""
    start = text.find("effects: [")
    end = text.find("], targets:", start)
    if start < 0 or end < 0:
        return []
    body = text[start + len("effects: ["):end]
    groups = body.split("], [") if body.startswith("[") else [body]
    out = []
    for group in groups:
        entry = {"damage": 0, "heal": 0, "knockback": None, "missed": False}
        for kind, fields in TARGET_EFFECT.findall(group):
            fields = fields or ""
            if kind in ("Damage", "BlockedDamage", "ParriedDamage", "CriticalDamage"):
                entry["damage"] += _int(FIELD["amount"], fields)
            elif kind == "Heal":
                entry["heal"] += _int(FIELD["amount"], fields)
            elif kind == "Knockback":
                entry["knockback"] = _int(FIELD["knockback_id"], fields)
            elif kind == "Miss":
                entry["missed"] = True
        out.append(entry)
    return out


def from_capture(path: Path, visit_index: int, label: str = "") -> dict:
    visits = capture_visits(path)
    if not 0 <= visit_index < len(visits):
        raise ValueError(f"the capture has {len(visits)} visit(s), not {visit_index + 1}")
    visit = visits[visit_index]
    replay = _empty(label or f"{path.name} visit {visit_index + 1}", f"Kawari capture {path.name}")
    replay["territory"], replay["content"] = visit["territory"], visit["content"]
    t0 = visit["start"]
    open_casts: dict[int, dict] = {}
    index = -1
    for packet in _lines(path):
        name = packet.get("name") or ""
        direction = packet.get("dir")
        # Counted as capture_visits counts them: what comes before the first ZoneInit is a visit of its own.
        if direction == "out" and name == "ZoneInit":
            index += 1
        elif index < 0:
            index = 0
        if index < visit_index:
            continue
        if index > visit_index:
            break
        t = round((packet.get("t", t0) - t0) / 1000, 3)
        text = packet.get("decoded") or ""
        src = packet.get("src")
        if direction == "note":
            note = packet.get("text") or ""
            if note.startswith("MARK"):
                replay["marks"].append([t, note[4:].strip()])
            continue
        if direction == "in":
            if name in MOVED_BY_CLIENT:
                position = POSITION.search(text)
                rotation = ROTATION.search(text)
                you = replay["you"] or src
                if position:
                    x, _y, z = position.groups()
                    replay["moves"].setdefault(str(you), []).append(
                        [t, _round(x), _round(z), _round(rotation.group(1), 3) if rotation else None])
            continue
        if replay["you"] is None and packet.get("dst"):
            replay["you"] = packet["dst"]
        if name == "PlayerSetup":
            replay["you"] = _int(re.compile(r"actor_id: ObjectId \((\d+)\)"), text, replay["you"] or 0)
        elif name in ("SpawnNpc", "SpawnPlayer"):
            _spawn(replay, packet, t)
        elif name in ("ActorMove", "ActorSetPos"):
            position = POSITION.search(text)
            rotation = ROTATION.search(text)
            if position:
                x, _y, z = position.groups()
                replay["moves"].setdefault(str(src), []).append(
                    [t, _round(x), _round(z), _round(rotation.group(1), 3) if rotation else None])
        elif name == "UpdateHpMpTp":
            replay["hp"].setdefault(str(src), []).append([t, _int(FIELD["hp"], text)])
        elif name == "EffectResultBasic":
            replay["hp"].setdefault(str(src), []).append([t, _int(FIELD["current_hp"], text)])
        elif name == "EffectResult":
            replay["hp"].setdefault(str(src), []).append([t, _int(FIELD["health_points"], text)])
        elif name == "DeleteActor":
            gone = OBJECT_ID.search(text)
            if gone:
                replay["gone"].append([t, int(gone.group(1))])
        elif name == "ActorCast":
            position = POSITION.search(text)
            rotation = ROTATION.search(text)
            target = CAST_TARGET.search(text)
            cast_time = CAST_TIME.search(text)
            cast = {"t": t, "src": src, "action": _int(FIELD["action_id"], text),
                    "target": int(target.group(1)) if target and target.group(1).isdigit() else 0,
                    "time": float(cast_time.group(1)) if cast_time else 0.0,
                    "x": _round(position.group(1)) if position else None,
                    "z": _round(position.group(3)) if position else None,
                    "r": _round(rotation.group(1), 3) if rotation else None, "cancelled": None}
            replay["casts"].append(cast)
            open_casts[src] = cast
        elif name.startswith(("ActionEffect", "AoeEffect")):
            targets_at = text.find("targets: [")
            ids = [int(i) for i in OBJECT_ID.findall(text[targets_at:])] if targets_at >= 0 else []
            effects = _target_effects(text)
            replay["hits"].append({"t": t, "src": src, "action": _int(FIELD["action_id"], text), "targets": [
                {"id": target, **(effects[k] if k < len(effects) else _effects([]))}
                for k, target in enumerate(ids)]})
            open_casts.pop(src, None)
        elif name.startswith("ActorControl"):
            control = CONTROL.match(text)
            category = control.group(1) if control else ""
            if category == "Kill":
                replay["deaths"].append([t, src])
            elif category == "CancelCast" and src in open_casts:
                open_casts.pop(src)["cancelled"] = t
        elif name == "DirectorVars":
            handler = HANDLER.search(text)
            replay["events"].append([t, "sequence", f"{handler.group(1).lower() if handler else 'director'} "
                                                    f"{handler.group(2) if handler else ''} sequence "
                                                    f"{_int(FIELD['flag'], text)}"])
    if replay["you"] is not None and str(replay["you"]) in replay["actors"]:
        replay["actors"][str(replay["you"])]["kind"] = "you"
    return _finish(replay)


# ════════════════════════════════════════════════════════════════════════════
#  Shapes, from the game install's action index
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_CONE_ANGLE = 90.0      # as the server's DEFAULT_CONE_ANGLE (gamedata.rs)
DEFAULT_DONUT_HOLE = 0.5       # as the server's DEFAULT_DONUT_HOLE


def _cone_angle(omen: str) -> float:
    match = re.search(r"fan(\d+)", omen or "")
    return float(match.group(1)) if match else DEFAULT_CONE_ANGLE


def _donut_hole(omen: str) -> float:
    match = re.search(r"sircle_(\d{4})", omen or "")
    if match:
        outer, inner = int(match.group(1)[:2]), int(match.group(1)[2:])
        if outer > 0 and inner < outer:
            return inner / outer
    return DEFAULT_DONUT_HOLE


def area_shape(action: dict) -> dict | None:
    """The area an action covers, as the world server's ActionInfo::area_shape (servers/world/src/gamedata.rs), and
    where it's drawn from (`anchor`): the caster, its target, or the spot it was aimed at (the cast's position)."""
    shape = _area_shape(action)
    if shape is not None:
        if (action.get("cast_type") or 0) == 7:
            shape["anchor"] = "ground"
        else:
            shape["anchor"] = "target" if shape.get("on_target") else "caster"
    return shape


def _area_shape(action: dict) -> dict | None:
    reach = action.get("effect_range") or 0
    cast_type = action.get("cast_type") or 0
    width = max(action.get("x_axis") or 0, 1)
    targeted = (action.get("range") or 0) > 0
    if reach == 0:
        return None
    if cast_type == 2:
        return {"shape": "circle", "radius": reach, "on_target": targeted}
    if cast_type == 5:
        return {"shape": "circle", "radius": reach, "on_target": False}
    if cast_type == 7:
        return {"shape": "circle", "radius": reach, "on_target": True}
    if cast_type in (3, 13):
        return {"shape": "cone", "radius": reach, "angle": _cone_angle(action.get("omen", ""))}
    if cast_type in (4, 8, 12):
        return {"shape": "line", "length": reach, "width": width}
    if cast_type == 10:
        return {"shape": "donut", "radius": reach, "hole": _donut_hole(action.get("omen", "")),
                "on_target": targeted}
    if cast_type == 11:
        return {"shape": "cross", "length": reach, "width": width}
    return None


def load_action_index(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
        return None
    return {"actions": {int(a["id"]): a for a in data["actions"] if "id" in a},
            "knockbacks": {int(k["id"]): k for k in data.get("knockbacks") or [] if "id" in k}}


def annotate(replay: dict, index: dict | None) -> dict:
    """The names, cast times and shapes of the actions the replay uses, and its knockbacks' distances."""
    used = {c["action"] for c in replay["casts"]} | {h["action"] for h in replay["hits"]}
    pushes = {t["knockback"] for h in replay["hits"] for t in h["targets"] if t.get("knockback")}
    replay["actions"], replay["knockbacks"] = {}, {}
    if not index:
        return replay
    for action_id in sorted(a for a in used if a is not None):
        row = index["actions"].get(int(action_id))
        if row:
            replay["actions"][str(action_id)] = {"name": row.get("name", ""), "cast": row.get("cast", 0),
                                                 "shape": area_shape(row)}
    for push in sorted(pushes):
        row = index["knockbacks"].get(int(push))
        if row:
            replay["knockbacks"][str(push)] = {"distance": row.get("distance", 0),
                                               "direction": row.get("direction", 0)}
    return replay
