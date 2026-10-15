"""Regenerate the database schema map from upstream Kawari source at a given commit.

The bundled ``schema_map.py`` was generated at one commit (538e289). Every time
Kawari changes its Diesel migrations, a map frozen at an older commit would diff
a database against the wrong column list and synthesise the wrong migration.

So the schema is versioned alongside the build: when a build is installed, this
module fetches the five source files it needs from the configured source (see
kawari_core/upstream.py) at that exact commit and derives the map the same way
the bundled one was made. It reads raw.githubusercontent.com, which has no rate
limit, so this works unauthenticated on a public repository.

Derivation rules (each learned the hard way):

* Columns and types come from ``servers/*/migrations/current_*/up.sql``.
* A TEXT column's default depends on its Rust type in ``models.rs``, and that
  lookup MUST be keyed per table. ``levels`` is ``BuddyLevels`` (3 bytes) on
  ``buddy`` but ``ClassLevels`` (35) on ``classjob``; keying by column name alone
  produced a 35-byte buddy default that would panic the world server.
* ``Bitmask<CONST>`` / ``QuestBitmask<CONST>`` lengths come from
  ``resources/data/constants.yml``.
* ``define_sql_array!(Name, ty, LEN)`` gives a fixed length; with no LEN the
  array is variable-length and defaults to ``[]``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import upstream

# Bump when the derivation changes, so cached maps from an older generator are
# regenerated instead of silently reused. v2 added BOOL/DOUBLE columns, which v1
# dropped entirely (volatile.rotation, volatile.is_online, mail.read,
# linkshells.is_crossworld) - a migration could not have detected them missing.
SCHEMA_FORMAT = 2
COLUMN_TYPES = "BIGINT|INTEGER|TEXT|BOOL|DOUBLE|REAL|FLOAT"

SOURCES = {
    "world_sql": "servers/world/migrations/current_world/up.sql",
    "login_sql": "servers/login/migrations/current_login/up.sql",
    "models":    "servers/world/src/database/models.rs",
    "lib":       "servers/world/src/lib.rs",
    "constants": "resources/data/constants.yml",
}

# Free-form TEXT columns with no derivable default. Values mirror what the server
# itself writes for a fresh character.
OVERRIDES = {
    ("world", "quest", "active"): "'[]'",
    ("world", "aetheryte", "favorite_aetherytes"): "'[]'",
    ("world", "mail", "attached_items"): "''",
    ("world", "mail", "message"): "''",
    ("world", "search_info", "comment"): "''",
    ("world", "linkshells", "name"): "''",
    ("world", "buddy", "name"): "''",
    ("world", "volatile", "position"): "'{\"x\":0.0,\"y\":0.0,\"z\":0.0}'",
}


class SchemaGenError(RuntimeError):
    pass


def fetch_sources(sha: str, timeout: int = 30) -> dict:
    """The five files the map is derived from, read from the configured source.

    A fork is read from the fork: if it changed a migration, deriving the map
    from upstream instead would diff the database against the wrong column list.
    """
    out = {}
    for key, path in SOURCES.items():
        try:
            out[key] = upstream.fetch_text(sha, path, timeout=timeout)
        except Exception as exc:                       # noqa: BLE001
            raise SchemaGenError(f"could not fetch {path} at {sha[:9]}: {exc}") from exc
    return out


def _parse_constants(text: str) -> dict:
    """constants.yml is flat KEY: int. Parse without requiring PyYAML."""
    consts = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*:\s*(\d+)\s*$", line)
        if m:
            consts[m.group(1)] = int(m.group(2))
    return consts


def generate(sources: dict) -> dict:
    consts = _parse_constants(sources["constants"])
    models = sources["models"]
    lib = sources["lib"]

    arrays = {}
    for m in re.finditer(r"define_sql_array!\((\w+),\s*[\w:]+(?:,\s*(\w+|\d+))?\)", lib):
        name, length = m.group(1), m.group(2)
        if length is None:
            arrays[name] = 0
        elif length.isdigit():
            arrays[name] = int(length)
        elif length in consts:
            arrays[name] = consts[length]

    struct_table, struct_fields = {}, {}
    for sm in re.finditer(r"((?:#\[[^\]]*\]\s*)*)pub struct (\w+) \{(.*?)\n\}", models, re.S):
        attrs, name, body = sm.group(1), sm.group(2), sm.group(3)
        tm = re.search(r"table_name\s*=\s*(?:super::)?schema::(\w+)", attrs)
        if tm:
            struct_table[name] = tm.group(1)
        struct_fields[name] = dict(re.findall(r"pub (\w+): ([\w:<>, ]+),", body))

    table_ty: dict[str, dict[str, str]] = {}
    for sname, table in struct_table.items():
        for field, ty in struct_fields.get(sname, {}).items():
            table_ty.setdefault(table, {}).setdefault(field, ty.strip())

    def default_for(table: str, col: str, sqltype: str):
        if sqltype != "TEXT":
            return "0"
        ty = table_ty.get(table, {}).get(col, "")
        bm = re.match(r"(?:Quest)?Bitmask<(\w+)>", ty)
        if bm and bm.group(1) in consts:
            return "'[%s]'" % ",".join(["0"] * consts[bm.group(1)])
        if ty in arrays:
            return "'[%s]'" % ",".join(["0"] * arrays[ty])
        return None

    schema = {}
    for key, dbname in (("world_sql", "world"), ("login_sql", "login")):
        tables = {}
        for m in re.finditer(r"CREATE TABLE `(\w+)`\((.*?)\n\);", sources[key], re.S):
            table = m.group(1)
            cols = {}
            for col, ty in re.findall(rf"`(\w+)`\s+({COLUMN_TYPES})", m.group(2)):
                cols[col] = {"type": ty, "default": default_for(table, col, ty)}
            tables[table] = {"columns": cols, "ddl": m.group(0)}
        if not tables:
            raise SchemaGenError(f"no tables parsed from {SOURCES[key]}")
        schema[dbname] = tables

    for (db, table, col), value in OVERRIDES.items():
        if table in schema.get(db, {}) and col in schema[db][table]["columns"]:
            schema[db][table]["columns"][col]["default"] = value

    _sanity_check(schema)
    return schema


def _sanity_check(schema: dict) -> None:
    """Catch the classes of generator bug that have bitten before."""
    world = schema.get("world", {})
    for must in ("character", "volatile", "quest", "inventory"):
        if must not in world:
            raise SchemaGenError(f"generated world schema has no '{must}' table")
    for table, col in (("volatile", "rotation"), ("volatile", "is_online")):
        if col not in world.get(table, {}).get("columns", {}):
            raise SchemaGenError(
                f"generated schema is missing {table}.{col} - the column-type "
                f"pattern did not match its type")
    buddy = world.get("buddy", {}).get("columns", {}).get("levels")
    if buddy and buddy["default"] and buddy["default"].count("0") > 8:
        raise SchemaGenError(
            "buddy.levels resolved to a long array - per-table type lookup failed")
    if "service_account" not in schema.get("login", {}):
        raise SchemaGenError("generated login schema has no 'service_account' table")


def cache_path(config_dir: Path, sha: str) -> Path:
    return Path(config_dir) / "schemas" / f"{sha}-v{SCHEMA_FORMAT}.json"


def load_or_generate(config_dir: Path, sha: str, allow_network: bool = True) -> dict:
    path = cache_path(config_dir, sha)
    if path.exists():
        return json.loads(path.read_text())
    if not allow_network:
        raise SchemaGenError(f"no cached schema for {sha[:9]} and network disabled")
    schema = generate(fetch_sources(sha))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=1))
    return schema


def summarise_diff(old: dict, new: dict) -> list[str]:
    """Human-readable differences between two schema maps."""
    lines = []
    for db in ("world", "login"):
        o, n = old.get(db, {}), new.get(db, {})
        for table in sorted(set(n) - set(o)):
            lines.append(f"{db}.{table}: new table")
        for table in sorted(set(o) - set(n)):
            lines.append(f"{db}.{table}: table removed")
        for table in sorted(set(o) & set(n)):
            oc, nc = o[table]["columns"], n[table]["columns"]
            added = sorted(set(nc) - set(oc))
            removed = sorted(set(oc) - set(nc))
            if added:
                lines.append(f"{db}.{table}: + {', '.join(added)}")
            if removed:
                lines.append(f"{db}.{table}: - {', '.join(removed)}")
    return lines
