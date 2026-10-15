"""SQLite tooling for world.db and login.db.

Kawari does not version its Diesel migrations -- upstream regenerates a single
``current_world`` / ``current_login`` migration and records it in
``__diesel_schema_migrations``. A database created by an older build therefore
keeps its old columns forever and nothing self-heals. That produces two failure
modes that look nothing alike:

* login: a missing column makes ``get_user_max_expansion`` return None and
  ``main.rs:92`` unwraps it. The release profile sets ``panic = "abort"``, so the
  whole login process dies and the launcher shows a generic error.
* world: the lobby's character query is ``.load(..).unwrap_or_default()``, so a
  missing column silently yields an EMPTY character list with nothing in the log.

This module diffs a live database against the schema map generated from upstream
and synthesises the ALTER TABLE statements to close the gap.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .schema_map import SCHEMA as BUNDLED_SCHEMA

# The schema the running build expects. Starts as the bundled map (generated at
# upstream 538e289) and is swapped for the per-build map from schemagen when a
# versioned build is activated, so migrations always target the right columns.
_active_schema: dict = BUNDLED_SCHEMA
_active_label: str = "bundled (49c945011)"


def get_schema() -> dict:
    return _active_schema


def schema_label() -> str:
    return _active_label


def set_schema(schema: dict | None, label: str = "") -> None:
    global _active_schema, _active_label
    if schema:
        _active_schema, _active_label = schema, label or "custom"
    else:
        _active_schema, _active_label = BUNDLED_SCHEMA, "bundled (49c945011)"

# Tables that must have one row per character, and the world server dereferences
# them without checking. Buddy is the notorious one:
#   Buddy::belonging_to(&found_character).first(..).unwrap()
# A character with no buddy row aborts the world server on character load.
PER_CHARACTER_TABLES = ["buddy"]


@dataclass
class TableDiff:
    table: str
    exists: bool
    missing_columns: list = field(default_factory=list)   # (name, type, default)
    unaddable: list = field(default_factory=list)          # columns with no safe default
    extra_columns: list = field(default_factory=list)
    row_count: int = 0

    @property
    def ok(self) -> bool:
        return self.exists and not self.missing_columns and not self.unaddable


@dataclass
class DbReport:
    path: Path
    kind: str                # "world" | "login"
    present: bool = False
    readable: bool = False
    error: str = ""
    tables: list = field(default_factory=list)
    missing_rows: list = field(default_factory=list)   # (table, count) per-character gaps
    size: int = 0

    @property
    def ok(self) -> bool:
        return (self.present and self.readable and not self.error
                and all(t.ok for t in self.tables) and not self.missing_rows)

    @property
    def problem_count(self) -> int:
        n = sum(1 for t in self.tables if not t.ok)
        return n + len(self.missing_rows)


def connect(path: Path, read_only: bool = True) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def analyse(path: Path, kind: str) -> DbReport:
    rep = DbReport(path=Path(path), kind=kind)
    if not rep.path.exists():
        return rep
    rep.present = True
    rep.size = rep.path.stat().st_size
    if rep.size == 0:
        rep.error = "File is zero bytes -- the server has not initialised it yet."
        return rep
    try:
        conn = connect(rep.path)
    except sqlite3.Error as exc:
        rep.error = str(exc)
        return rep

    try:
        rep.readable = True
        have_tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        for table, spec in get_schema()[kind].items():
            diff = TableDiff(table=table, exists=table in have_tables)
            if diff.exists:
                cols = {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info(`{table}`)")}
                for name, meta in spec["columns"].items():
                    if name in cols:
                        continue
                    if meta["default"] is None:
                        diff.unaddable.append(name)
                    else:
                        diff.missing_columns.append((name, meta["type"], meta["default"]))
                diff.extra_columns = [c for c in cols if c not in spec["columns"]]
                try:
                    diff.row_count = conn.execute(
                        f"SELECT COUNT(*) FROM `{table}`").fetchone()[0]
                except sqlite3.Error:
                    diff.row_count = -1
            else:
                for name, meta in spec["columns"].items():
                    diff.missing_columns.append((name, meta["type"], meta["default"] or "''"))
            rep.tables.append(diff)

        if kind == "world" and "character" in have_tables:
            for table in PER_CHARACTER_TABLES:
                if table not in have_tables:
                    continue
                try:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM `character` c "
                        f"WHERE NOT EXISTS (SELECT 1 FROM `{table}` t "
                        f"WHERE t.content_id = c.content_id)").fetchone()[0]
                    if n:
                        rep.missing_rows.append((table, n))
                except sqlite3.Error:
                    pass
    finally:
        conn.close()
    return rep


def migration_sql(rep: DbReport) -> str:
    """Build the SQL that brings this database up to the current schema."""
    if rep.ok:
        return ""
    spec = get_schema()[rep.kind]
    out = [
        f"-- {rep.kind}.db migration to the schema of: {_active_label}",
        "-- Generated by Kawari Manager from the upstream Diesel migration.",
        "-- Bitmask TEXT columns are JSON arrays of a FIXED length; the lengths come",
        "-- from resources/data/constants.yml. Bitmask::from_sql unwraps its parse,",
        "-- so a wrong length panics the world server rather than degrading.",
        "",
        "BEGIN TRANSACTION;",
        "",
    ]
    for diff in rep.tables:
        if diff.ok:
            continue
        if not diff.exists:
            out.append(f"-- new table: {diff.table}")
            out.append(spec[diff.table]["ddl"].rstrip() + ";")
            out.append("")
            continue
        for name, ctype, default in diff.missing_columns:
            out.append(f"ALTER TABLE `{diff.table}` ADD COLUMN `{name}` "
                       f"{ctype} NOT NULL DEFAULT {default};")
    if any(d.missing_columns and d.exists for d in rep.tables):
        out.append("")

    for table, _count in rep.missing_rows:
        cols = list(get_schema()[rep.kind][table]["columns"].items())
        names = ", ".join(f"`{c}`" for c, _ in cols)
        vals = []
        for cname, meta in cols:
            if cname == "content_id":
                vals.append("c.`content_id`")
            else:
                vals.append(meta["default"] if meta["default"] is not None else "''")
        out.append(f"-- one {table} row per character (a missing row is fatal on load)")
        out.append(f"INSERT INTO `{table}` ({names})")
        out.append(f"SELECT {', '.join(vals)} FROM `character` c")
        out.append(f"WHERE NOT EXISTS (SELECT 1 FROM `{table}` t "
                   f"WHERE t.content_id = c.content_id);")
        out.append("")

    # tables that exist but are missing rows entirely (fresh table just created)
    for diff in rep.tables:
        if not diff.exists and diff.table in PER_CHARACTER_TABLES:
            cols = list(get_schema()[rep.kind][diff.table]["columns"].items())
            names = ", ".join(f"`{c}`" for c, _ in cols)
            vals = ["c.`content_id`" if c == "content_id"
                    else (m["default"] if m["default"] is not None else "''")
                    for c, m in cols]
            out.append(f"INSERT INTO `{diff.table}` ({names})")
            out.append(f"SELECT {', '.join(vals)} FROM `character` c;")
            out.append("")

    out.append("COMMIT;")
    return "\n".join(out)


def apply_sql(path: Path, sql: str) -> tuple[bool, str]:
    """Run a script transactionally. Returns (ok, message)."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(sql)
            conn.commit()
        finally:
            conn.close()
        return True, "Applied successfully."
    except sqlite3.Error as exc:
        return False, str(exc)


def query(path: Path, sql: str, params=(), read_only: bool = True):
    # connect() must be inside the guard: opening a database that does not exist
    # raises OperationalError, and every caller here expects an error string
    # rather than an exception (a server that has never run has no login.db).
    try:
        conn = connect(Path(path), read_only=read_only)
    except sqlite3.Error as exc:
        return [], [], str(exc)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        if not read_only:
            conn.commit()
        return cols, rows, ""
    except sqlite3.Error as exc:
        return [], [], str(exc)
    finally:
        conn.close()


def execute(path: Path, sql: str, params=()) -> tuple[bool, str, int]:
    try:
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return True, "", cur.rowcount
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, str(exc), 0


# ── Account helpers ─────────────────────────────────────────────────────────
def users(login_db: Path):
    cols, rows, err = query(login_db, "SELECT id, username FROM user ORDER BY username")
    return rows, err


def service_accounts(login_db: Path):
    cols, rows, err = query(login_db, """
        SELECT s.id, s.user_id, s.max_ex, s.legacy, u.username
        FROM service_account s LEFT JOIN user u ON u.id = s.user_id
        ORDER BY s.id""")
    return rows, err


def orphan_report(login_db: Path, world_db: Path) -> list[str]:
    """The exact class of breakage that stops a login dead."""
    problems = []
    cols, rows, err = query(login_db, """
        SELECT u.id, u.username FROM user u
        WHERE NOT EXISTS (SELECT 1 FROM service_account s WHERE s.user_id = u.id)""")
    if not err:
        for r in rows:
            problems.append(
                f"Account '{r['username']}' (id {r['id']}) has no service_account row. "
                f"Login will succeed and then abort the login server on "
                f"get_user_max_expansion().unwrap().")

    if world_db.exists():
        cols, chars, err2 = query(world_db,
                                  "SELECT content_id, name, service_account_id FROM character")
        if not err2:
            sa_ids = set()
            _c, sas, err3 = query(login_db, "SELECT id FROM service_account")
            if not err3:
                sa_ids = {r["id"] for r in sas}
            for ch in chars:
                if ch["service_account_id"] not in sa_ids:
                    problems.append(
                        f"Character '{ch['name']}' points at service_account_id "
                        f"{ch['service_account_id']}, which does not exist in login.db. "
                        f"It will never appear in the character list.")
    return problems


def create_service_account(login_db: Path, sa_id: int, user_id: int,
                           max_ex: int, legacy: int) -> tuple[bool, str]:
    ok, err, _ = execute(
        login_db,
        "INSERT INTO service_account (id, user_id, max_ex, legacy) VALUES (?, ?, ?, ?)",
        (sa_id, user_id, max_ex, legacy))
    return ok, err


def columns_of(path: Path, table: str) -> list[str]:
    try:
        conn = connect(Path(path))
    except sqlite3.Error:
        return []
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info(`{table}`)")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# ── account creation ────────────────────────────────────────────────────────
# Mirrors LoginDatabase::add_user in servers/login/src/database.rs: a random u32
# user id, the password stored AS TYPED (Kawari compares it literally - there is
# no hashing), and a service_account row with max_ex = repositories - 1.
def _free_id(conn, table: str) -> int:
    import random
    used = {r[0] for r in conn.execute(f"SELECT id FROM `{table}`")}
    while True:
        candidate = random.randint(1, 0xFFFFFFFF)      # fastrand::u32
        if candidate not in used:
            return candidate


def account_exists(login_db: Path, username: str) -> bool:
    _c, rows, err = query(login_db, "SELECT id FROM user WHERE username = ?", (username,))
    return bool(rows) and not err


def similar_account(login_db: Path, username: str) -> str:
    """A username differing only by case - Kawari's lookup is case-sensitive, so
    this would be a second, separate account that is easy to confuse."""
    _c, rows, err = query(login_db,
                          "SELECT username FROM user WHERE lower(username) = lower(?)",
                          (username,))
    if err or not rows:
        return ""
    for r in rows:
        if r["username"] != username:
            return r["username"]
    return ""


def ensure_login_schema(login_db: Path) -> list:
    """Create any missing login tables, recording the migration the way Diesel
    does so the login server does not try to apply it again and abort."""
    created = []
    spec = get_schema()["login"]
    Path(login_db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(login_db))
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, meta in spec.items():
            if table not in have:
                conn.execute(meta["ddl"])
                created.append(table)
        if "__diesel_schema_migrations" not in have:
            conn.execute("CREATE TABLE __diesel_schema_migrations ("
                         "version VARCHAR(50) PRIMARY KEY NOT NULL, "
                         "run_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            created.append("__diesel_schema_migrations")
        row = conn.execute("SELECT version FROM __diesel_schema_migrations "
                           "WHERE version = 'current'").fetchone()
        if not row:
            conn.execute("INSERT INTO __diesel_schema_migrations (version, run_on) "
                         "VALUES ('current', datetime())")
        conn.commit()
    finally:
        conn.close()
    return created


def create_account(login_db: Path, username: str, password: str, max_ex: int,
                   legacy: int = 0) -> tuple[int, int]:
    """Create a login account plus its service account. Returns (user_id, sa_id)."""
    username = username.strip()
    if not username:
        raise ValueError("Username cannot be empty.")
    if not password:
        raise ValueError("Password cannot be empty.")
    ensure_login_schema(login_db)
    if account_exists(login_db, username):
        raise ValueError(f"An account called '{username}' already exists.")

    conn = sqlite3.connect(str(login_db))
    try:
        conn.execute("BEGIN")
        user_id = _free_id(conn, "user")
        sa_id = _free_id(conn, "service_account")
        conn.execute("INSERT INTO user (id, username, password) VALUES (?, ?, ?)",
                     (user_id, username, password))
        conn.execute("INSERT INTO service_account (id, user_id, max_ex, legacy) "
                     "VALUES (?, ?, ?, ?)", (sa_id, user_id, int(max_ex), int(legacy)))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    return user_id, sa_id


def characters(world_db: Path):
    """List characters, tolerating an out-of-date schema.

    This must work on a database that is MISSING columns, because that is
    exactly the situation the user is in when the lobby shows an empty
    character list. Selecting a fixed column set would make the app blind at
    the one moment it needs to see.
    """
    if not Path(world_db).exists():
        return [], ""
    have = set(columns_of(world_db, "character"))
    if not have:
        return [], "No 'character' table in this database."
    wanted = ["content_id", "name", "service_account_id", "actor_id",
              "gm_rank", "time_played_minutes", "warrior_of_light"]
    select = [f"c.`{c}`" if c in have else f"NULL AS `{c}`" for c in wanted]

    vol = set(columns_of(world_db, "volatile"))
    if vol:
        for c in ("zone_id", "position", "is_online"):
            select.append(f"v.`{c}`" if c in vol else f"NULL AS `{c}`")
        join = "LEFT JOIN volatile v ON v.content_id = c.content_id"
    else:
        select += ["NULL AS zone_id", "NULL AS position", "NULL AS is_online"]
        join = ""

    cj = set(columns_of(world_db, "classjob"))
    if "current_class" in cj:
        select.append("j.`current_class`")
        join += " LEFT JOIN classjob j ON j.content_id = c.content_id"
    else:
        select.append("NULL AS current_class")

    sql = (f"SELECT {', '.join(select)} FROM `character` c {join} ORDER BY c.`name`")
    cols, rows, err = query(world_db, sql)
    return rows, err


def set_character_field(world_db: Path, content_id: int, column: str, value) -> tuple[bool, str]:
    allowed = {"name", "gm_rank", "warrior_of_light", "time_played_minutes",
               "service_account_id"}
    if column not in allowed:
        return False, f"Refusing to edit column '{column}'."
    ok, err, _ = execute(world_db,
                         f"UPDATE `character` SET `{column}` = ? WHERE content_id = ?",
                         (value, content_id))
    return ok, err


def teleport_character(world_db: Path, content_id: int, zone_id: int,
                       x: float = 0.0, y: float = 0.0, z: float = 0.0) -> tuple[bool, str]:
    """Offline rescue for a character stranded in broken content.

    volatile.zone_id is where the world server puts you on login, so editing it
    is the way out when chat commands are not reachable.
    """
    pos = json.dumps({"x": float(x), "y": float(y), "z": float(z)},
                     separators=(",", ":"))
    ok, err, n = execute(
        world_db, "UPDATE volatile SET zone_id = ?, position = ? WHERE content_id = ?",
        (zone_id, pos, content_id))
    if ok and n == 0:
        return False, "No volatile row for that character (has it ever logged in?)."
    return ok, err


def delete_character(world_db: Path, content_id: int) -> tuple[bool, str]:
    tables = [t for t in get_schema()["world"]
              if "content_id" in get_schema()["world"][t]["columns"] and t != "friends"]
    try:
        conn = sqlite3.connect(str(world_db))
        try:
            for t in tables:
                try:
                    conn.execute(f"DELETE FROM `{t}` WHERE content_id = ?", (content_id,))
                except sqlite3.Error:
                    pass
            conn.execute("DELETE FROM friends WHERE content_id = ? OR friend_content_id = ?",
                         (content_id, content_id))
            conn.commit()
        finally:
            conn.close()
        return True, ""
    except sqlite3.Error as exc:
        return False, str(exc)


def table_overview(path: Path, kind: str):
    """(table, rows, columns) for the browser."""
    out = []
    try:
        conn = connect(Path(path))
    except sqlite3.Error:
        return out
    try:
        for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            name = r[0]
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM `{name}`").fetchone()[0]
            except sqlite3.Error:
                n = -1
            ncols = len(list(conn.execute(f"PRAGMA table_info(`{name}`)")))
            out.append((name, n, ncols))
    finally:
        conn.close()
    return out
