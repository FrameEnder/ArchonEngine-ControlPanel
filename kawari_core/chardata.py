"""Everything a character is, across every table, with versioned snapshots.

A character in world.db is spread over ~17 tables keyed by content_id. The
editor loads all of them into one ``Character`` object, edits it in memory, and
writes only the columns that actually changed, in a single transaction, after
taking a snapshot. Snapshots live under ``~/.kawari/characters/<content_id>/``
and can be diffed against each other or against the live database, and restored.

Bitmask "unlock all" mirrors upstream exactly: Bitmask::set_all() in
servers/world/src/bitmask.rs is ``self.data = vec![0xFF; N]``. N comes from the
active schema map, never from whatever length happens to be stored, so a column
written by an older build is corrected to the right size on save.
"""

from __future__ import annotations

import json
import sqlite3
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from . import dbtools
from .dbtools import get_schema, query
from . import gamedata

# Tables keyed by content_id that belong to exactly one character.
# friends/linkshell_members relate characters to each other and are excluded
# from snapshots so a restore can never corrupt someone else's rows.
CHARACTER_TABLES = [
    "character", "volatile", "classjob", "customize", "inventory", "quest",
    "aetheryte", "aether_current", "unlock", "content", "grand_company",
    "companion", "buddy", "search_info", "mentor",
]

# (table, column, label, group, note) for the Unlocks tab.
BITMASKS = [
    ("aether_current", "comp_flg_set",   "Flight (aether current zones)", "Travel",
     "The per-zone flight grant. Kawari already sends ENABLE_FLYING on every "
     "non-duty zone-in; this is the client-side gate. Relog to apply."),
    ("aether_current", "unlocked",       "Aether currents (individual)", "Travel",
     "The collectible currents themselves, not the flight grant."),
    ("aetheryte",      "unlocked",       "Aetherytes", "Travel", ""),
    ("unlock",         "chocobo_taxi_stands", "Chocobo porter stands", "Travel", ""),
    ("unlock",         "mounts",         "Mounts", "Collections", ""),
    ("unlock",         "minions",        "Minions", "Collections", ""),
    ("unlock",         "orchestrion_rolls", "Orchestrion rolls", "Collections", ""),
    ("unlock",         "triple_triad_cards", "Triple Triad cards", "Collections", ""),
    ("unlock",         "ornaments",      "Fashion accessories", "Collections", ""),
    ("unlock",         "glasses_styles", "Facewear styles", "Collections", ""),
    ("unlock",         "titles",         "Titles", "Collections", ""),
    ("unlock",         "adventures",     "Sightseeing log", "Collections", ""),
    ("unlock",         "caught_fish",    "Fish log", "Collections", ""),
    ("unlock",         "caught_spearfish", "Spearfishing log", "Collections", ""),
    ("companion",      "unlocked_equip", "Chocobo barding", "Collections", ""),
    ("unlock",         "unlocks",        "Actions, emotes and features", "Progress",
     "The general unlock mask - what !unlock all fills."),
    ("unlock",         "cutscene_seen",  "Cutscenes seen", "Progress", ""),
    ("unlock",         "seen_active_help", "Active help seen", "Progress", ""),
    ("quest",          "completed",      "Quests completed", "Progress",
     "Completing every quest also completes quests that gate one another; "
     "!skipintro does the same thing. Snapshot first if you may want this back."),
    ("quest",          "completed_levequests", "Levequests completed", "Progress", ""),
    ("content",        "unlocked_dungeons", "Dungeons unlocked", "Duties", ""),
    ("content",        "unlocked_trials",   "Trials unlocked", "Duties", ""),
    ("content",        "unlocked_raids",    "Raids unlocked", "Duties", ""),
    ("content",        "unlocked_guildhests", "Guildhests unlocked", "Duties", ""),
    ("content",        "unlocked_special_content", "Special content unlocked", "Duties", ""),
    ("content",        "unlocked_misc_content", "Misc content unlocked", "Duties", ""),
    ("content",        "unlocked_crystalline_conflicts", "Crystalline Conflict unlocked", "Duties", ""),
    ("content",        "unlocked_frontlines", "Frontlines unlocked", "Duties", ""),
]

# Field names are exactly CurrencyStorage in servers/world/src/inventory/currency.rs.
# 'flame_seal' holds seals for WHICHEVER Grand Company is active (upstream TODO:
# "rename to something company agnostic"); the two tomestone slots are seasonal.
# 'dummy' is a padding slot and is deliberately not exposed.
CURRENCIES = [
    ("gil",                   "Gil",                    999_999_999),
    ("mgp",                   "MGP",                    9_999_999),
    ("flame_seal",            "Grand Company seals",    90_000),
    ("allied_seal",           "Allied Seals",           4_000),
    ("wolf_mark",             "Wolf Marks",             20_000),
    ("tomestone_poetics",     "Tomestones: Poetics",    2_000),
    ("tomestone_mathematics", "Tomestones: seasonal A", 2_000),
    ("tomestone_heliometry",  "Tomestones: seasonal B", 2_000),
]
CRYSTAL_CAP = 9_999


@dataclass
class Character:
    content_id: int
    rows: dict = field(default_factory=dict)       # table -> {col: value}
    original: dict = field(default_factory=dict)   # untouched copy for diffing
    columns: dict = field(default_factory=dict)    # table -> [columns present in DB]

    # ── generic access ──────────────────────────────────────────────────
    def get(self, table: str, col: str, default=None):
        return (self.rows.get(table) or {}).get(col, default)

    def set(self, table: str, col: str, value):
        if table not in self.rows:
            raise KeyError(f"character has no '{table}' row")
        if col not in self.columns.get(table, []):
            raise KeyError(f"'{table}.{col}' does not exist in this database")
        self.rows[table][col] = value

    def has(self, table: str, col: str | None = None) -> bool:
        if table not in self.rows:
            return False
        return col is None or col in self.columns.get(table, [])

    # ── JSON columns ────────────────────────────────────────────────────
    def get_json(self, table: str, col: str, default=None):
        raw = self.get(table, col)
        if raw is None:
            return deepcopy(default)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return deepcopy(default)

    def set_json(self, table: str, col: str, value):
        # Compact separators match what serde_json writes for bitmasks.
        self.set(table, col, json.dumps(value, separators=(",", ":")))

    @property
    def name(self) -> str:
        return self.get("character", "name", "")

    # ── changes ─────────────────────────────────────────────────────────
    def changes(self) -> dict:
        out = {}
        for table, row in self.rows.items():
            orig = self.original.get(table, {})
            delta = {c: v for c, v in row.items() if orig.get(c) != v}
            if delta:
                out[table] = delta
        return out

    @property
    def dirty(self) -> bool:
        return bool(self.changes())


# ── bitmask helpers ─────────────────────────────────────────────────────────
def bitmask_length(table: str, col: str) -> int:
    meta = dbtools.get_schema()["world"].get(table, {}).get("columns", {}).get(col)
    if not meta or not meta.get("default"):
        return 0
    try:
        return len(json.loads(meta["default"].strip("'")))
    except ValueError:
        return 0


def bitmask_stats(value) -> tuple[int, int]:
    """(bits set, total bits) for a list of bytes."""
    if not isinstance(value, list):
        return 0, 0
    total = len(value) * 8
    set_bits = sum(bin(int(b) & 0xFF).count("1") for b in value)
    return set_bits, total


def filled(table: str, col: str, byte: int) -> list[int]:
    n = bitmask_length(table, col)
    if n <= 0:
        raise ValueError(f"unknown bitmask length for {table}.{col}")
    return [byte] * n


# ── load / save ─────────────────────────────────────────────────────────────
def load(world_db: Path, content_id: int) -> Character:
    conn = sqlite3.connect(f"file:{world_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ch = Character(content_id=content_id)
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in CHARACTER_TABLES:
            if table not in have:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info(`{table}`)")]
            if "content_id" not in cols:
                continue
            row = conn.execute(f"SELECT * FROM `{table}` WHERE content_id = ?",
                               (content_id,)).fetchone()
            ch.columns[table] = cols
            if row is not None:
                ch.rows[table] = {k: row[k] for k in row.keys()}
    finally:
        conn.close()
    if "character" not in ch.rows:
        raise LookupError(f"no character with content_id {content_id}")
    ch.original = deepcopy(ch.rows)
    return ch


def save(world_db: Path, ch: Character) -> dict:
    """Write only changed columns, atomically. Returns the change set."""
    delta = ch.changes()
    if not delta:
        return {}
    conn = sqlite3.connect(str(world_db))
    try:
        conn.execute("BEGIN")
        for table, cols in delta.items():
            assignments = ", ".join(f"`{c}` = ?" for c in cols)
            conn.execute(f"UPDATE `{table}` SET {assignments} WHERE content_id = ?",
                         (*cols.values(), ch.content_id))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    ch.original = deepcopy(ch.rows)
    return delta


# ── snapshots (per-character version control) ───────────────────────────────
def history_dir(config_dir: Path, content_id: int) -> Path:
    return Path(config_dir) / "characters" / str(content_id)


def snapshot(config_dir: Path, world_db: Path, content_id: int, label: str,
             build_sha: str = "") -> Path:
    ch = load(world_db, content_id)
    folder = history_dir(config_dir, content_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in label if c.isalnum() or c in "-_ ").strip().replace(" ", "-")
    path = folder / f"{stamp}{'-' + safe if safe else ''}.json"
    path.write_text(json.dumps({
        "content_id": content_id,
        "name": ch.name,
        "label": label,
        "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "build": build_sha,
        "rows": ch.rows,
    }, indent=1))
    return path


def list_snapshots(config_dir: Path, content_id: int) -> list[dict]:
    folder = history_dir(config_dir, content_id)
    if not folder.exists():
        return []
    out = []
    for f in sorted(folder.glob("*.json"), reverse=True):
        try:
            meta = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        out.append({"path": f, "label": meta.get("label", ""),
                    "taken_at": meta.get("taken_at", ""),
                    "build": meta.get("build", ""), "rows": meta.get("rows", {})})
    return out


def diff_rows(a: dict, b: dict) -> list[str]:
    """Describe the difference between two row sets (a = older, b = newer)."""
    lines = []
    for table in sorted(set(a) | set(b)):
        ra, rb = a.get(table), b.get(table)
        if ra is None:
            lines.append(f"{table}: row added")
            continue
        if rb is None:
            lines.append(f"{table}: row removed")
            continue
        for col in sorted(set(ra) | set(rb)):
            va, vb = ra.get(col), rb.get(col)
            if va == vb:
                continue
            lines.append(f"{table}.{col}: {_describe_change(table, col, va, vb)}")
    return lines


# Columns that really are bitmasks. Everything else that happens to be a list of
# integers (classjob.levels, company_ranks, favourite aetherytes...) is an array
# of values and must be described element-wise, not as a bit count.
_BITMASK_COLUMNS = {(t, c) for t, c, *_rest in BITMASKS} | {
    ("quest", "completed_legacy"), ("quest", "unlocked_map_markers"),
    ("quest", "gathered_gathering_items"), ("content", "cleared_dungeons"),
    ("content", "cleared_trials"), ("content", "cleared_raids"),
    ("content", "cleared_guildhests"), ("content", "cleared_misc_content"),
    ("content", "cleared_crystalline_conflicts"), ("content", "cleared_frontlines"),
    ("content", "cleared_masked_carnivale"),
}


def _describe_change(table: str, col: str, va, vb) -> str:
    try:
        ja, jb = json.loads(va), json.loads(vb)
    except (TypeError, ValueError):
        ja = jb = None
    int_lists = (isinstance(ja, list) and isinstance(jb, list)
                 and all(isinstance(x, int) for x in ja + jb))
    if int_lists and (table, col) in _BITMASK_COLUMNS:
        sa, _ = bitmask_stats(ja)
        sb, total = bitmask_stats(jb)
        size = "" if len(ja) == len(jb) else f" (resized {len(ja)} -> {len(jb)} bytes)"
        return f"{sa:,} -> {sb:,} of {total:,} bits set{size}"
    if int_lists:
        if len(ja) != len(jb):
            return f"{ja} -> {jb}"
        idx = [i for i in range(len(ja)) if ja[i] != jb[i]]
        names = {}
        if table == "classjob" and col in ("levels", "exp"):
            names = {slot: abbr for slot, _n, abbr, _cap in gamedata.CLASSJOB_SLOTS}
        parts = [f"{names.get(i, f'[{i}]')} {ja[i]:,} -> {jb[i]:,}" for i in idx[:8]]
        more = f", +{len(idx) - 8} more" if len(idx) > 8 else ""
        return ", ".join(parts) + more
    if isinstance(ja, dict) and isinstance(jb, dict):
        changed = [k for k in set(ja) | set(jb) if ja.get(k) != jb.get(k)]
        return f"JSON changed ({', '.join(sorted(changed)[:6])}{'...' if len(changed) > 6 else ''})"
    if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
        return f"{va} -> {vb}"
    sa, sb = str(va), str(vb)
    if len(sa) > 60 or len(sb) > 60:
        return f"changed ({len(sa):,} -> {len(sb):,} chars)"
    return f"{sa!r} -> {sb!r}"


def restore(world_db: Path, content_id: int, rows: dict) -> list[str]:
    """Write a snapshot back. Only columns that exist in the current database are
    restored, so a snapshot from an older build restores cleanly after a
    migration (new columns keep their current values)."""
    conn = sqlite3.connect(str(world_db))
    restored = []
    try:
        conn.execute("BEGIN")
        for table, row in rows.items():
            if table not in CHARACTER_TABLES:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info(`{table}`)")]
            if not cols:
                continue
            usable = {c: v for c, v in row.items() if c in cols and c != "content_id"}
            if not usable:
                continue
            exists = conn.execute(f"SELECT 1 FROM `{table}` WHERE content_id = ?",
                                  (content_id,)).fetchone()
            if exists:
                sets = ", ".join(f"`{c}` = ?" for c in usable)
                conn.execute(f"UPDATE `{table}` SET {sets} WHERE content_id = ?",
                             (*usable.values(), content_id))
            else:
                names = ", ".join(f"`{c}`" for c in ["content_id", *usable])
                marks = ", ".join("?" for _ in range(len(usable) + 1))
                conn.execute(f"INSERT INTO `{table}` ({names}) VALUES ({marks})",
                             (content_id, *usable.values()))
            restored.append(table)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    return restored


# ── creation ────────────────────────────────────────────────────────────────
# Mirrors WorldDatabase::create_player_data in
# servers/world/src/database/character.rs. Column defaults come from the active
# schema map (which equals the Rust Default for these types); only the values
# that upstream sets explicitly are overridden below.
INITIAL_HOMEPOINT = {1: 8, 2: 2, 3: 9}       # common::determine_initial_homepoint
ONLINE_STATUS_NEW_ADVENTURER = 32            # OnlineStatus::NewAdventurer
LANGUAGE_ENGLISH = 2                         # SocialListUILanguages::ENGLISH


def default_inventory(world_db: Path) -> str:
    """An empty inventory document.

    Prefers the shape used by a character already in THIS database, so container
    sizes and kinds always match the running build; falls back to the bundled
    template generated from a real 7.56 document.
    """
    cols, rows, err = query(world_db, "SELECT contents FROM inventory LIMIT 1")
    if not err and rows:
        try:
            return json.dumps(_blank_inventory(json.loads(rows[0]["contents"])),
                              separators=(",", ":"))
        except (ValueError, KeyError, TypeError):
            pass
    template = Path(__file__).parent / "data" / "inventory_template.json"
    return template.read_text().strip()


EMPTY_ITEM = {
    "quantity": 0, "item_id": 0, "crafter_content_id": 0, "item_flags": 0,
    "condition": 0, "spiritbond_or_collectability": 0, "glamour_id": 0,
    "materia": [0] * 5, "materia_grades": [0] * 5, "stains": [0, 0],
}


def _blank_inventory(doc: dict) -> dict:
    """Zero every item, keeping container shapes and the fixed item ids that
    currency and crystal slots carry (gil is item 1, fire shard is item 2...)."""
    def blank(item, keep_id=False):
        out = dict(EMPTY_ITEM)
        if keep_id:
            out["item_id"] = item.get("item_id", 0)
        return out

    out = {}
    for key, value in doc.items():
        if key == "pages":
            out[key] = [{"slots": [blank(s) for s in p["slots"]]} for p in value]
        elif key in ("currency", "crystals"):
            out[key] = {n: blank(v, keep_id=True) for n, v in value.items()}
        elif key == "equipped":
            out[key] = {n: blank(v) for n, v in value.items()}
        elif isinstance(value, dict) and "slots" in value:
            out[key] = {"slots": [blank(s) for s in value["slots"]],
                        **({"kind": value["kind"]} if "kind" in value else {})}
        else:
            out[key] = value
    return out


def create_character(world_db: Path, service_account_id: int, name: str,
                     chara_make: str, city_state: int, zone_id: int,
                     classjob_id: int, exp_index: int | None = None) -> int:
    """Create a character exactly as the world server would. Returns content_id."""
    import random
    schema = get_schema()["world"]
    if name_taken(world_db, name):
        raise ValueError(f"A character called '{name}' already exists.")

    content_id = random.randint(1, 0xFFFFFFFF)       # fastrand::u32
    actor_id = random.randint(1, 0xFFFFFFFF)

    # "The first character will get the highest GM privileges to not make
    # running Kawari locally a pain."
    _c, rows, _e = query(world_db, "SELECT COUNT(*) AS n FROM `character`")
    first = (rows[0]["n"] if rows else 0) == 0

    levels = [0] * 35
    if exp_index is not None and 0 <= exp_index < len(levels):
        levels[exp_index] = 1                        # initial level

    overrides = {
        "character": {"service_account_id": service_account_id, "actor_id": actor_id,
                      "name": name, "gm_rank": 90 if first else 0},
        "classjob": {"current_class": classjob_id, "first_class": classjob_id,
                     "levels": json.dumps(levels, separators=(",", ":"))},
        "customize": {"chara_make": chara_make, "city_state": city_state},
        "aetheryte": {"homepoint": INITIAL_HOMEPOINT.get(city_state, 9)},
        "volatile": {"zone_id": zone_id},
        "inventory": {"contents": default_inventory(world_db)},
        "mentor": {"is_novice": 1},                  # all players start as novices
        "search_info": {"online_status": ONLINE_STATUS_NEW_ADVENTURER,
                        "selected_languages": LANGUAGE_ENGLISH},
    }

    conn = sqlite3.connect(str(world_db))
    try:
        conn.execute("BEGIN")
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in CHARACTER_TABLES:
            if table not in have or table not in schema:
                continue
            row = {"content_id": content_id}
            for col, meta in schema[table]["columns"].items():
                if col == "content_id":
                    continue
                if col in overrides.get(table, {}):
                    row[col] = overrides[table][col]
                elif meta["default"] is not None:
                    raw = meta["default"]
                    row[col] = raw.strip("'") if raw.startswith("'") else int(raw)
                else:
                    raise ValueError(f"no value available for {table}.{col}")
            names = ", ".join(f"`{c}`" for c in row)
            marks = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO `{table}` ({names}) VALUES ({marks})",
                         tuple(row.values()))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    return content_id


def name_taken(world_db: Path, name: str, excluding: int | None = None) -> bool:
    _c, rows, err = query(world_db, "SELECT content_id FROM `character` WHERE name = ?",
                          (name,))
    if err:
        return False
    return any(r["content_id"] != excluding for r in rows)


def find_by_name(world_db: Path, name: str) -> int | None:
    _c, rows, err = query(world_db, "SELECT content_id FROM `character` WHERE name = ?",
                          (name,))
    return rows[0]["content_id"] if not err and rows else None


# ── soul crystals ───────────────────────────────────────────────────────────
def owned_soul_crystals(inventory: dict) -> set:
    """Item ids of every soul crystal the character has, chest or equipped."""
    owned = set()
    chest = (inventory.get("armoury_soul_crystal") or {}).get("slots", [])
    for slot in chest:
        if slot.get("item_id"):
            owned.add(int(slot["item_id"]))
    equipped = (inventory.get("equipped") or {}).get("soul_crystal") or {}
    if equipped.get("item_id"):
        owned.add(int(equipped["item_id"]))
    return owned


def grant_soul_crystal(inventory: dict, item_id: int) -> bool:
    """Put a soul crystal in the first free armoury slot. False if already owned
    or the chest is full."""
    if item_id in owned_soul_crystals(inventory):
        return False
    chest = (inventory.get("armoury_soul_crystal") or {}).get("slots")
    if not chest:
        return False
    for i, slot in enumerate(chest):
        if not slot.get("item_id"):
            chest[i] = dict(EMPTY_ITEM, item_id=int(item_id), quantity=1,
                            condition=gamedata.SOUL_CRYSTAL_CONDITION)
            return True
    return False


def is_online(world_db: Path, content_id: int) -> bool:
    try:
        conn = sqlite3.connect(f"file:{world_db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT is_online FROM volatile WHERE content_id = ?",
                               (content_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0])
