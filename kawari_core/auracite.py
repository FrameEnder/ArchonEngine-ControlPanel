"""Import an Auracite character backup into an EXISTING character.

Kawari has its own Auracite importer (servers/world/src/auracite.rs), but it can
only CREATE a character: it calls check_is_name_free() and fails with
"Character already exists" otherwise. So a fresh dump of a character you already
imported cannot be applied through the web UI.

This module does the update case. Every mapping below was read out of that same
upstream importer, so the result is what Kawari would have written:

* ``chara_make`` is ``[[26 customize values], voice, guardian, birth_month,
  birth_day, classjob, unk2]`` with the customize order from
  ``customize_data_to_json``. Note the eye colours are crossed over on purpose —
  upstream maps the dump's ``eye_color2`` to ``right_eye_color`` and
  ``eye_color`` to ``left_eye_color``.
* Class levels are indexed by ``ExpArrayIndex``, not by ClassJob id.
* Inventory containers are filled by slot index. Equipped and the armoury index
  positionally, but CURRENCY DOES NOT: slots are 0/3/4/6/7/8/9/10 per
  ``CurrencyStorage::get_slot_mut``, so a positional read would put Grand
  Company seals into the wrong currency.
* Bitmasks are padded or truncated to the active schema's length, so a dump
  taken on a different patch cannot produce a wrong-length value (which would
  panic the world server, since ``Bitmask::from_sql`` unwraps its parse).

Fields upstream ignores, and so do we: ``world``, ``data_center``, ``plate``,
``player_commendations``, ``armory_waist`` (Kawari's equipped storage has a belt
slot but there is no waist armoury container). ``playtime`` is offered as an
opt-in because upstream never imports it.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import chardata
from . import constants as C
from . import dbtools
from . import gamedata as G

CHARACTER_JSON = "character.json"

# CurrencyStorage::get_slot_mut - NOT positional.
CURRENCY_SLOTS = {0: "gil", 3: "flame_seal", 4: "wolf_mark", 6: "tomestone_poetics",
                  7: "tomestone_mathematics", 8: "allied_seal", 9: "mgp",
                  10: "tomestone_heliometry"}

# Auracite container -> where it lives in Kawari's inventory document.
ARMOURY = {
    "armory_off_hand": "armoury_off_hand", "armory_head": "armoury_head",
    "armory_body": "armoury_body", "armory_hands": "armoury_hands",
    "armory_legs": "armoury_legs", "armory_feets": "armoury_feet",
    "armory_ear": "armoury_earring", "armory_neck": "armoury_necklace",
    "armory_wrist": "armoury_bracelet", "armory_rings": "armoury_rings",
    "armory_soul_crystal": "armoury_soul_crystal", "armory_main_hand": "armoury_main_hand",
}

EMPTY_ITEM = {
    "quantity": 0, "item_id": 0, "crafter_content_id": 0, "item_flags": 0,
    "condition": 0, "spiritbond_or_collectability": 0, "glamour_id": 0,
    "materia": [0] * 5, "materia_grades": [0] * 5, "stains": [0, 0],
}

# Straight bitmask copies: dump key -> (table, column)
BITMASK_FIELDS = {
    "unlocks": ("unlock", "unlocks"),
    "seen_active_help": ("unlock", "seen_active_help"),
    "minions": ("unlock", "minions"),
    "mounts": ("unlock", "mounts"),
    "orchestrion_rolls": ("unlock", "orchestrion_rolls"),
    "cutscene_seen": ("unlock", "cutscene_seen"),
    "ornaments": ("unlock", "ornaments"),
    "caught_fish": ("unlock", "caught_fish"),
    "caught_spearfish": ("unlock", "caught_spearfish"),
    "adventures": ("unlock", "adventures"),
    "triple_triad_cards": ("unlock", "triple_triad_cards"),
    "glasses_styles": ("unlock", "glasses_styles"),
    "chocobo_taxi_stands": ("unlock", "chocobo_taxi_stands"),
    "titles": ("unlock", "titles"),
    "unlocked_companion_equip": ("companion", "unlocked_equip"),
    "comp_flg_set": ("aether_current", "comp_flg_set"),
    "unlocked_aether_currents": ("aether_current", "unlocked"),
    "unlocked_aetherytes": ("aetheryte", "unlocked"),
    "completed_quests": ("quest", "completed"),
}
CONTENT_FIELDS = [
    "unlocked_special_content", "unlocked_raids", "unlocked_dungeons",
    "unlocked_guildhests", "unlocked_trials", "unlocked_crystalline_conflicts",
    "unlocked_frontlines", "cleared_raids", "cleared_dungeons", "cleared_guildhests",
    "cleared_trials", "cleared_crystalline_conflicts", "cleared_frontlines",
    "cleared_masked_carnivale", "unlocked_misc_content", "cleared_misc_content",
]

IGNORED_FIELDS = ["world", "data_center", "plate", "player_commendations", "armory_waist"]


def _zone_name(zone_id) -> str:
    if zone_id is None:
        return "?"
    name = dict(C.TERRITORIES).get(int(zone_id))
    return f"{name} ({zone_id})" if name else f"Zone {zone_id}"


class AuraciteError(RuntimeError):
    pass


@dataclass
class Section:
    key: str
    label: str
    detail: str
    default: bool = True


SECTIONS = [
    Section("appearance", "Appearance and identity",
            "chara_make (race, tribe, face, colours), voice, nameday, guardian, "
            "starting city-state."),
    Section("classes", "Classes and jobs",
            "Every class level and EXP, current and first class, rested EXP."),
    Section("inventory", "Inventory, equipment and currency",
            "Bags, equipped gear, all twelve armoury chests and currencies. "
            "Crystals and key items are untouched - the dump has no data for them."),
    Section("unlocks", "Unlocks and collections",
            "Mounts, minions, orchestrion, titles, Triple Triad, fashion accessories, "
            "facewear, fish and spearfish logs, sightseeing, cutscenes, chocobo barding."),
    Section("travel", "Aetherytes and aether currents",
            "Attunements, home point, favourites, free destination, and the "
            "per-zone flight grant."),
    Section("content", "Duties",
            "Unlocked and cleared dungeons, trials, raids, guildhests, PvP, "
            "Masked Carnivale."),
    Section("quests", "Quest progress", "The completed-quest bitmask."),
    Section("profile", "Grand Company, title and mentor status",
            "Active company and the three ranks, displayed title, mentor and "
            "novice flags."),
    Section("position", "Position",
            "Zone, coordinates and rotation at the time of the dump.", False),
    Section("playtime", "Play time",
            "Upstream's importer skips this field; the value is treated as minutes.",
            False),
    Section("name", "Character name",
            "Renames the character to the name in the dump. Off by default.", False),
]


@dataclass
class Dump:
    path: Path
    data: dict = field(default_factory=dict)
    extra_files: list = field(default_factory=list)

    @classmethod
    def load(cls, path) -> "Dump":
        path = Path(path)
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                member = next((n for n in names if Path(n).name == CHARACTER_JSON), None)
                if member is None:
                    raise AuraciteError(
                        f"{path.name} has no {CHARACTER_JSON} - is it an Auracite backup?")
                raw = zf.read(member).decode("utf-8")
                extras = [n for n in names if n != member and not n.endswith("/")]
        except zipfile.BadZipFile as exc:
            raise AuraciteError(f"{path.name} is not a readable zip: {exc}") from exc
        except OSError as exc:
            raise AuraciteError(str(exc)) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise AuraciteError(f"{CHARACTER_JSON} is not valid JSON: {exc}") from exc
        if "name" not in data or "appearance" not in data:
            raise AuraciteError(f"{CHARACTER_JSON} does not look like an Auracite dump.")
        return cls(path=path, data=data, extra_files=extras)

    # ── presentation ────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self.data.get("name", "")

    def summary(self) -> list[tuple[str, str]]:
        d = self.data
        cls = d.get("current_class", 0)
        level = next((c.get("level") for c in d.get("classjob_levels", [])
                      if c.get("value") == cls), None)
        gil = next((i.get("quantity") for i in d.get("currency", {}).get("items", [])
                    if i.get("slot") == 0), 0)
        levels = [c.get("level", 0) for c in d.get("classjob_levels", [])]
        rows = [
            ("Name", d.get("name", "?")),
            ("World", (d.get("world") or {}).get("name", "?")),
            ("Race / tribe", f"{(d.get('race') or {}).get('name','?')} / "
                             f"{(d.get('tribe') or {}).get('name','?')}"),
            ("Current class", f"{G.classjob_name(cls)}"
                              + (f" level {level}" if level else "")),
            ("Classes at 1+", f"{sum(1 for l in levels if l > 0)} of {len(levels)}"
                              f"   (max level {max(levels) if levels else 0})"),
            ("Gil", f"{gil:,}"),
            ("Grand Company", (d.get("grand_company") or {}).get("name", "None")),
            ("Title", (d.get("title") or {}).get("name", "-")),
            ("Zone at dump", _zone_name(d.get("zone_id"))),
            ("Play time", f"{d.get('playtime', 0):,} minutes (assumed unit)"),
        ]
        if self.extra_files:
            rows.append(("Other files",
                         f"{len(self.extra_files)} image(s) - portrait art, not imported"))
        return rows

    # ── field extraction ────────────────────────────────────────────────
    def chara_make(self) -> str:
        d = self.data
        a = d["appearance"]
        try:
            customize = [
                d["race"]["value"], d["gender"]["value"], a["model_type"], a["height"],
                d["tribe"]["value"], a["face_type"], a["hair_style"],
                int(bool(a["has_highlights"])), a["skin_color"],
                a["eye_color2"],                       # -> right_eye_color (upstream crosses these)
                a["hair_color"], a["hair_color2"], a["face_features"],
                a["face_features_color"], a["eyebrows"],
                a["eye_color"],                        # -> left_eye_color
                a["eye_shape"], a["nose_shape"], a["jaw_shape"], a["lip_style"],
                a["lip_color"], a["race_feature_size"], a["race_feature_type"],
                a["bust_size"], a["facepaint"], a["facepaint_color"],
            ]
            content = [[str(int(v)) for v in customize],
                       str(d["voice"]), str(d["guardian"]["value"]),
                       str(d["nameday"]["month"]), str(d["nameday"]["day"]),
                       str(d["current_class"]), "1"]           # unk2 is always 1 upstream
        except (KeyError, TypeError, ValueError) as exc:
            raise AuraciteError(f"appearance data is incomplete: {exc}") from exc
        # Compact separators: byte-identical to what CharaMake::to_json writes,
        # so re-importing an unchanged dump produces no spurious diff.
        return json.dumps({"classid": 118, "classname": "CharaMake", "content": content},
                          separators=(",", ":"))

    def class_arrays(self, levels: list, exps: list) -> tuple[list, list]:
        """Overlay the dump's levels onto existing arrays, by ExpArrayIndex."""
        levels, exps = list(levels), list(exps)
        for entry in self.data.get("classjob_levels", []):
            idx = G.CLASSJOB_EXP_INDEX.get(entry.get("value"))
            if idx is None or idx >= len(levels):
                continue
            levels[idx] = int(entry.get("level") or 0)
            if entry.get("exp") is not None:
                exps[idx] = int(entry["exp"])
        return levels, exps

    def inventory(self, current: dict) -> dict:
        """Rebuild the containers the dump covers, leaving the rest alone."""
        inv = json.loads(json.dumps(current))    # deep copy

        def fill_indexed(container_key: str, slots: list):
            for i in range(len(slots)):
                slots[i] = dict(EMPTY_ITEM)
            for item in self.data.get(container_key, {}).get("items", []):
                slot = int(item.get("slot", -1))
                if 0 <= slot < len(slots):
                    slots[slot] = self._item(item)

        for n in range(4):
            key = f"inventory{n + 1}"
            if key in self.data and n < len(inv.get("pages", [])):
                fill_indexed(key, inv["pages"][n]["slots"])
        for dump_key, inv_key in ARMOURY.items():
            if dump_key in self.data and inv_key in inv:
                fill_indexed(dump_key, inv[inv_key]["slots"])

        if "equipped" in self.data and "equipped" in inv:
            names = list(inv["equipped"])            # EquipSlot order = struct order
            for name in names:
                inv["equipped"][name] = dict(EMPTY_ITEM)
            for item in self.data["equipped"].get("items", []):
                slot = int(item.get("slot", -1))
                if 0 <= slot < len(names):
                    inv["equipped"][names[slot]] = self._item(item)

        if "currency" in self.data and "currency" in inv:
            # Auracite omits zero-quantity currencies, so clearing must KEEP the
            # slot's item id - upstream starts from Inventory::default(), whose
            # currency slots already carry their CurrencyKind id (gil = 1).
            for name, slot in inv["currency"].items():
                inv["currency"][name] = dict(EMPTY_ITEM,
                                             item_id=int(slot.get("item_id", 0)))
            for item in self.data["currency"].get("items", []):
                name = CURRENCY_SLOTS.get(int(item.get("slot", -1)))
                if name and name in inv["currency"]:
                    inv["currency"][name] = self._item(item)
        return inv

    @staticmethod
    def _item(src: dict) -> dict:
        def fixed(values, size):
            values = list(values or [])
            return values[:size] if len(values) == size else [0] * size
        return {
            "quantity": int(src.get("quantity", 0)),
            "item_id": int(src.get("id", 0)),
            "crafter_content_id": int(src.get("crafter_content_id", 0)),
            "item_flags": int(src.get("item_flags", 0)),
            "condition": int(src.get("condition", 0)),
            "spiritbond_or_collectability": int(src.get("spiritbond_or_collectability", 0)),
            "glamour_id": int(src.get("glamour_id", 0)),
            "materia": fixed(src.get("materia"), 5),
            "materia_grades": fixed(src.get("materia_grades"), 5),
            "stains": fixed(src.get("stains"), 2),
        }


def _sized(table: str, col: str, values) -> list:
    """Pad or truncate a bitmask to what the active schema expects."""
    want = chardata.bitmask_length(table, col)
    values = [int(v) & 0xFF for v in (values or [])]
    if want and len(values) != want:
        values = (values + [0] * want)[:want]
    return values


def build_changes(ch: chardata.Character, dump: Dump, sections: set) -> tuple[dict, list]:
    """Return ({table: {column: value}}, [notes]) without touching the database."""
    out: dict = {}
    notes: list = []

    def put(table, col, value):
        if not ch.has(table, col):
            notes.append(f"skipped {table}.{col} - not in this database "
                         f"(run the migration on the Database page)")
            return
        out.setdefault(table, {})[col] = value

    def put_mask(table, col, values, dump_key=""):
        want = chardata.bitmask_length(table, col)
        if want and len(values or []) != want:
            notes.append(f"{dump_key or col}: dump has {len(values or [])} bytes, this "
                         f"build expects {want} - padded/truncated to fit")
        put(table, col, json.dumps(_sized(table, col, values), separators=(",", ":")))

    d = dump.data

    if "appearance" in sections:
        put("customize", "chara_make", dump.chara_make())
        if "city_state" in d:
            put("customize", "city_state", int(d["city_state"]["value"]))

    if "classes" in sections:
        levels, exps = dump.class_arrays(
            ch.get_json("classjob", "levels", [0] * G.CLASSJOB_ARRAY_SIZE),
            ch.get_json("classjob", "exp", [0] * G.CLASSJOB_ARRAY_SIZE))
        put("classjob", "levels", json.dumps(levels, separators=(",", ":")))
        put("classjob", "exp", json.dumps(exps, separators=(",", ":")))
        for col, key in (("current_class", "current_class"), ("first_class", "first_class"),
                         ("rested_exp", "rested_exp")):
            if key in d:
                put("classjob", col, int(d[key]))

    if "inventory" in sections:
        current = ch.get_json("inventory", "contents", {})
        if isinstance(current, dict) and current:
            put("inventory", "contents",
                json.dumps(dump.inventory(current), separators=(",", ":")))
        else:
            notes.append("inventory skipped - the character has no inventory document")

    if "unlocks" in sections:
        for key, (table, col) in BITMASK_FIELDS.items():
            if table in ("unlock", "companion") and key in d:
                put_mask(table, col, d[key], key)

    if "travel" in sections:
        for key in ("comp_flg_set", "unlocked_aether_currents", "unlocked_aetherytes"):
            if key in d:
                table, col = BITMASK_FIELDS[key]
                put_mask(table, col, d[key], key)
        if "homepoint" in d:
            put("aetheryte", "homepoint", int(d["homepoint"]))
        if "free_aetheryte" in d:
            put("aetheryte", "free_aetheryte", int(d["free_aetheryte"]))
        if "favorite_aetherytes" in d:
            put("aetheryte", "favorite_aetherytes",
                json.dumps([int(v) for v in d["favorite_aetherytes"]], separators=(",", ":")))

    if "content" in sections:
        for key in CONTENT_FIELDS:
            if key in d:
                put_mask("content", key, d[key], key)

    if "quests" in sections and "completed_quests" in d:
        put_mask("quest", "completed", d["completed_quests"], "completed_quests")

    if "profile" in sections:
        if "grand_company" in d:
            put("grand_company", "active_company", int(d["grand_company"]["value"]))
        if "grand_company_ranks" in d:
            ranks = [int(v) for v in d["grand_company_ranks"]][:3]
            put("grand_company", "company_ranks",
                json.dumps((ranks + [0, 0, 0])[:3], separators=(",", ":")))
        if "title" in d:
            put("volatile", "title", int(d["title"]["value"]))
        for col, key in (("is_battle", "is_battle_mentor"), ("is_trade", "is_trade_mentor"),
                         ("is_novice", "is_novice"), ("is_returner", "is_returner")):
            if key in d:
                put("mentor", col, int(bool(d[key])))

    if "position" in sections:
        if "zone_id" in d:
            put("volatile", "zone_id", int(d["zone_id"]))
        if "position_x" in d:
            put("volatile", "position", json.dumps(
                {"x": float(d["position_x"]), "y": float(d["position_y"]),
                 "z": float(d["position_z"])}, separators=(",", ":")))
        if "rotation" in d:
            put("volatile", "rotation", float(d["rotation"]))

    if "playtime" in sections and "playtime" in d:
        put("character", "time_played_minutes", int(d["playtime"]))

    if "name" in sections and d.get("name"):
        put("character", "name", str(d["name"]))

    ignored = [f for f in IGNORED_FIELDS if f in d]
    if ignored:
        notes.append("not imported (no equivalent in Kawari, same as upstream): "
                     + ", ".join(ignored))
    return out, notes


def apply_changes(ch: chardata.Character, changes: dict) -> None:
    """Stage the changes on the in-memory character (caller saves)."""
    for table, cols in changes.items():
        for col, value in cols.items():
            ch.set(table, col, value)


# ════════════════════════════════════════════════════════════════════════════
#  Export: a Kawari character back out as an Auracite backup
# ════════════════════════════════════════════════════════════════════════════
CUSTOMIZE_KEYS = [
    "race", "gender", "model_type", "height", "tribe", "face_type", "hair_style",
    "has_highlights", "skin_color", "eye_color2", "hair_color", "hair_color2",
    "face_features", "face_features_color", "eyebrows", "eye_color", "eye_shape",
    "nose_shape", "jaw_shape", "lip_style", "lip_color", "race_feature_size",
    "race_feature_type", "bust_size", "facepaint", "facepaint_color",
]


def _named(value, name=""):
    return {"name": name, "value": int(value)}


def _container(items: list) -> dict:
    return {"items": items}


def _export_item(slot: int, item: dict) -> dict:
    return {
        "slot": int(slot),
        "quantity": int(item.get("quantity", 0)),
        "id": int(item.get("item_id", 0)),
        "crafter_content_id": int(item.get("crafter_content_id", 0)),
        "item_flags": int(item.get("item_flags", 0)),
        "condition": int(item.get("condition", 0)),
        "spiritbond_or_collectability": int(item.get("spiritbond_or_collectability", 0)),
        "glamour_id": int(item.get("glamour_id", 0)),
        "materia": list(item.get("materia", [0] * 5)),
        "materia_grades": list(item.get("materia_grades", [0] * 5)),
        "stains": list(item.get("stains", [0, 0])),
    }


def export_character(ch: chardata.Character, world_name: str = "") -> dict:
    """Build an Auracite-shaped character.json from a Kawari character.

    Emits every key upstream's importer reads, so the result round-trips: export
    then import produces no changes. Occupied slots only, as Auracite does.
    """
    data: dict = {}
    cm = ch.get_json("customize", "chara_make", {})
    content = cm.get("content") if isinstance(cm, dict) else None
    if not content or len(content) < 7:
        raise AuraciteError("character has no usable chara_make to export")
    customize = [int(v) for v in content[0]]
    appearance = dict(zip(CUSTOMIZE_KEYS, customize))

    data["name"] = ch.get("character", "name", "")
    data["world"] = {"name": world_name, "value": 0}
    data["data_center"] = {"name": "", "value": 0}
    data["city_state"] = _named(ch.get("customize", "city_state", 1))
    data["nameday"] = {"day": int(content[4]), "month": int(content[3])}
    data["guardian"] = _named(int(content[2]))
    data["gender"] = _named(appearance["gender"])
    data["tribe"] = _named(appearance["tribe"])
    data["race"] = _named(appearance["race"])

    levels = ch.get_json("classjob", "levels", [])
    exps = ch.get_json("classjob", "exp", [])
    jobs = []
    for cid, name, _abbr in G.CLASSJOBS:
        idx = G.CLASSJOB_EXP_INDEX.get(cid)
        if idx is None or idx >= len(levels):
            continue
        jobs.append({"name": name, "level": int(levels[idx]),
                     "exp": int(exps[idx]) if idx < len(exps) else 0, "value": cid})
    data["classjob_levels"] = jobs

    data["grand_company"] = _named(ch.get("grand_company", "active_company", 0))
    data["grand_company_ranks"] = [int(v) for v in
                                   ch.get_json("grand_company", "company_ranks", [0, 0, 0])]
    title_id = int(ch.get("volatile", "title", 0) or 0)
    data["title"] = _named(title_id, dict(G.TITLES).get(title_id, ""))
    data["playtime"] = int(ch.get("character", "time_played_minutes", 0) or 0)
    data["voice"] = int(content[1])
    data["plate"] = {}
    data["is_battle_mentor"] = bool(ch.get("mentor", "is_battle", 0))
    data["is_trade_mentor"] = bool(ch.get("mentor", "is_trade", 0))
    data["is_novice"] = bool(ch.get("mentor", "is_novice", 0))
    data["is_returner"] = bool(ch.get("mentor", "is_returner", 0))
    data["player_commendations"] = 0
    # has_highlights is a bool in the dump but a 0/1 byte in chara_make
    appearance["has_highlights"] = bool(appearance["has_highlights"])
    data["appearance"] = {k: appearance[k] for k in CUSTOMIZE_KEYS
                          if k not in ("race", "gender", "tribe")}

    inv = ch.get_json("inventory", "contents", {})
    for n in range(4):
        pages = inv.get("pages", [])
        slots = pages[n]["slots"] if n < len(pages) else []
        data[f"inventory{n + 1}"] = _container(
            [_export_item(i, it) for i, it in enumerate(slots) if it.get("item_id")])
    equipped = inv.get("equipped", {})
    data["equipped"] = _container(
        [_export_item(i, it) for i, (_n, it) in enumerate(equipped.items())
         if it.get("item_id")])
    currency = inv.get("currency", {})
    rev = {v: k for k, v in CURRENCY_SLOTS.items()}
    data["currency"] = _container(
        [_export_item(rev[name], it) for name, it in currency.items()
         if name in rev and it.get("quantity")])
    for dump_key, inv_key in ARMOURY.items():
        slots = (inv.get(inv_key) or {}).get("slots", [])
        data[dump_key] = _container(
            [_export_item(i, it) for i, it in enumerate(slots) if it.get("item_id")])
    data["armory_waist"] = _container([])       # Kawari has no waist armoury container

    # Pad/truncate to the active schema, so a backup taken from a database
    # written by an older build is still correct for the current one.
    for key, (table, col) in BITMASK_FIELDS.items():
        data[key] = _sized(table, col, ch.get_json(table, col, []))
    for key in CONTENT_FIELDS:
        data[key] = _sized("content", key, ch.get_json("content", key, []))

    data["homepoint"] = int(ch.get("aetheryte", "homepoint", 0) or 0)
    data["favorite_aetherytes"] = [int(v) for v in
                                   ch.get_json("aetheryte", "favorite_aetherytes", [])]
    data["free_aetheryte"] = int(ch.get("aetheryte", "free_aetheryte", 0) or 0)
    data["current_class"] = int(ch.get("classjob", "current_class", 1) or 1)
    data["first_class"] = int(ch.get("classjob", "first_class", 1) or 1)
    data["rested_exp"] = int(ch.get("classjob", "rested_exp", 0) or 0)

    pos = ch.get_json("volatile", "position", {"x": 0.0, "y": 0.0, "z": 0.0})
    data["position_x"] = float(pos.get("x", 0.0))
    data["position_y"] = float(pos.get("y", 0.0))
    data["position_z"] = float(pos.get("z", 0.0))
    data["rotation"] = float(ch.get("volatile", "rotation", 0.0) or 0.0)
    data["zone_id"] = int(ch.get("volatile", "zone_id", 0) or 0)
    return data


def write_backup(path, data: dict) -> Path:
    """Write an Auracite-compatible zip containing character.json."""
    path = Path(path)
    if path.suffix.lower() != ".zip":
        path = path.with_suffix(".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(CHARACTER_JSON, json.dumps(data, indent=1))
    return path


def suggest_filename(name: str) -> str:
    import time
    safe = "".join(c if c.isalnum() else "_" for c in (name or "character"))
    return f"{safe}_-_{time.strftime('%-m-%-d-%y')}.zip"


def name_conflict(world_db, content_id: int, name: str) -> bool:
    """Another character already using this name."""
    cols, rows, err = dbtools.query(
        world_db, "SELECT content_id FROM `character` WHERE name = ?", (name,))
    if err:
        return False
    return any(r["content_id"] != content_id for r in rows)
