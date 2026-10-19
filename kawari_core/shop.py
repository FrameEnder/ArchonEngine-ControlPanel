"""The shop: listings, the two currencies, purchases and delivery.

Listings live in the web app's own database, seeded from ``data/shop_seed.json``
(the FINAL FANTASY XIV Online Store's catalogue, gathered from public sources).
Each listing has contents - game items by name and quantity - which are linked
to item IDs from the game install's ``item-index.json`` (written by
``kawari-questdump items``), so nothing here depends on hardcoded game text.

Two prices per listing:

* **Gil** comes out of the buyer's selected character. The world server keeps a
  logged-in character in memory and writes it back on every autosave, so gil
  can only be taken while that character is logged out.
* **Play coins** (the name is configurable) belong to the web account and are
  earned from playtime: every character on the account counts, at a rate the
  administrators set (25 an hour by default).

Delivery works the way retail store purchases do: the item arrives by moogle
mail as a reward letter, to be taken from any mailbox in game. Kawari reads the
mail table whenever the mailbox opens, so a letter added here shows up even for
a character that is logged in.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SEED = Path(__file__).parent / "data" / "shop_seed.json"
ITEM_INDEX = "item-index.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_listing (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    -- [{"name": ..., "qty": n, "item_id": id or absent, "alt": [other names]}]
    contents      TEXT    NOT NULL DEFAULT '[]',
    price_gil     INTEGER NOT NULL DEFAULT 1,
    price_coins   INTEGER NOT NULL DEFAULT 1,
    sell_gil      INTEGER NOT NULL DEFAULT 1,
    sell_coins    INTEGER NOT NULL DEFAULT 1,
    visible       INTEGER NOT NULL DEFAULT 1,
    retail_price  TEXT    NOT NULL DEFAULT '',
    patch         TEXT    NOT NULL DEFAULT '',
    notes         TEXT    NOT NULL DEFAULT '',
    position      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS shop_listing_category ON shop_listing(category, position);

CREATE TABLE IF NOT EXISTS shop_purchase (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    content_id  INTEGER NOT NULL,
    character   TEXT    NOT NULL DEFAULT '',
    listing_id  INTEGER NOT NULL,
    listing     TEXT    NOT NULL,
    currency    TEXT    NOT NULL,
    price       INTEGER NOT NULL,
    letters     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS shop_purchase_user ON shop_purchase(user_id, id DESC);

-- Play coins per web account. minutes_counted is how much of the account's
-- playtime has been turned into coins; carry is the part of a coin still owed
-- (in coin-minutes) so no playtime is lost to rounding.
CREATE TABLE IF NOT EXISTS shop_wallet (
    user_id          INTEGER PRIMARY KEY,
    balance          INTEGER NOT NULL DEFAULT 0,
    minutes_counted  INTEGER NOT NULL DEFAULT 0,
    carry            INTEGER NOT NULL DEFAULT 0,
    earned_total     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shop_coin_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS shop_coin_log_user ON shop_coin_log(user_id, id DESC);

-- Preview images an administrator uploaded for a listing. The lowest position
-- is the cover, used as the listing's thumbnail in place of its game icon.
CREATE TABLE IF NOT EXISTS shop_listing_image (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL,
    file        TEXT    NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS shop_listing_image_listing ON shop_listing_image(listing_id, position, id);
"""

MAX_IMAGES = 8

# Settings (the web app's setting table) and their defaults.
DEFAULTS = {
    "shop_coin_name": "Play Coins",
    "shop_coins_per_hour": "25",
    # Count playtime from before the shop existed when an account's wallet opens.
    "shop_count_past_playtime": "1",
    "shop_show_retail": "1",
    "shop_gil_enabled": "1",
    "shop_coins_enabled": "1",
    "shop_letter": "Thank you for shopping at {site}! Here is your {listing}.",
}

# Kawari's mail: a reward letter from u64::MAX shows the World as the sender
# (servers/world/src/database/mail.rs), like retail's store deliveries.
SENDER_CONTENT_ID = -1          # u64::MAX stored in a signed 64-bit column
LETTER_REWARD = 1               # LetterType::Reward
LETTER_MAX_BYTES = 600          # LETTER_MSG_MAX_LENGTH is 601 including the terminator
ITEMS_PER_LETTER = 5            # six slots; the last is kept for gil, as the client lays it out
ITEM_CONDITION_MAX = 30000
GIL_CAP = 999_999_999

EMPTY_ITEM = {
    "quantity": 0, "item_id": 0, "crafter_content_id": 0, "item_flags": 0,
    "condition": 0, "spiritbond_or_collectability": 0, "glamour_id": 0,
    "materia": [0] * 5, "materia_grades": [0] * 5, "stains": [0, 0],
}


class ShopError(Exception):
    """A purchase that can't go through, with the reason to show the buyer."""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ════════════════════════════════════════════════════════════════════════════
#  Setup and settings
# ════════════════════════════════════════════════════════════════════════════
def init(webapp_db: Path) -> None:
    """Create the tables, and fill the shop from the seed the first time."""
    conn = _connect(webapp_db)
    try:
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) FROM shop_listing").fetchone()[0] == 0
        seeded = conn.execute("SELECT value FROM setting WHERE key = 'shop_seeded'").fetchone()
        if empty and not seeded:
            _insert_seed(conn)
            conn.execute("INSERT INTO setting (key, value) VALUES ('shop_seeded', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_now(),))
        conn.commit()
    finally:
        conn.close()


def _insert_seed(conn: sqlite3.Connection) -> int:
    seed = json.loads(SEED.read_text())
    rows = []
    for position, item in enumerate(seed["listings"]):
        rows.append((item["category"], item["name"], item.get("description", ""),
                     json.dumps(item.get("contents", [])), item.get("retail_price", ""),
                     item.get("patch", ""), item.get("notes", ""), position, _now()))
    conn.executemany(
        "INSERT INTO shop_listing (category, name, description, contents, retail_price, patch, "
        "notes, position, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def reseed(webapp_db: Path) -> int:
    """Throw every listing away and start again from the seed (prices back to 1).

    Uploaded preview images go with them: call all_image_files() first to
    delete their files."""
    conn = _connect(webapp_db)
    try:
        conn.execute("DELETE FROM shop_listing")
        conn.execute("DELETE FROM shop_listing_image")
        count = _insert_seed(conn)
        conn.commit()
        return count
    finally:
        conn.close()


def settings(webapp_db: Path) -> dict:
    out = dict(DEFAULTS)
    conn = _connect(webapp_db)
    try:
        for row in conn.execute("SELECT key, value FROM setting WHERE key LIKE 'shop_%'"):
            if row["key"] in DEFAULTS:
                out[row["key"]] = row["value"]
    finally:
        conn.close()
    return out


def save_settings(webapp_db: Path, values: dict) -> None:
    conn = _connect(webapp_db)
    try:
        for key, value in values.items():
            if key in DEFAULTS:
                conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def coin_rate(cfg: dict) -> int:
    try:
        return max(0, int(cfg.get("shop_coins_per_hour", "25")))
    except ValueError:
        return 25


# ════════════════════════════════════════════════════════════════════════════
#  Listings
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class Listing:
    id: int
    category: str
    name: str
    description: str
    contents: list
    price_gil: int
    price_coins: int
    sell_gil: bool
    sell_coins: bool
    visible: bool
    retail_price: str
    patch: str
    notes: str
    # Filled in by link(): each content entry gains "item" (the game item) or
    # "problem" (why it couldn't be linked.)
    linked: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Every content entry is a known game item, so it can be delivered."""
        return bool(self.linked) and all(e.get("item") for e in self.linked)

    @property
    def problems(self) -> list:
        return [e for e in self.linked if not e.get("item")]

    @classmethod
    def from_row(cls, row) -> "Listing":
        try:
            contents = json.loads(row["contents"] or "[]")
        except ValueError:
            contents = []
        return cls(row["id"], row["category"], row["name"], row["description"], contents,
                   row["price_gil"], row["price_coins"], bool(row["sell_gil"]),
                   bool(row["sell_coins"]), bool(row["visible"]), row["retail_price"],
                   row["patch"], row["notes"])


def categories(webapp_db: Path, include_hidden: bool = False) -> list[tuple[str, int]]:
    """Categories in shop order, with how many listings each has."""
    conn = _connect(webapp_db)
    try:
        sql = ("SELECT category, COUNT(*) AS n, MIN(position) AS p FROM shop_listing "
               + ("" if include_hidden else "WHERE visible = 1 ")
               + "GROUP BY category ORDER BY p")
        return [(r["category"], r["n"]) for r in conn.execute(sql)]
    finally:
        conn.close()


def listings(webapp_db: Path, category: str | None = None, query: str = "",
             include_hidden: bool = False) -> list[Listing]:
    where, params = [], []
    if category:
        where.append("category = ?")
        params.append(category)
    if query:
        where.append("(name LIKE ? OR description LIKE ? OR contents LIKE ?)")
        params += [f"%{query}%"] * 3
    if not include_hidden:
        where.append("visible = 1")
    sql = "SELECT * FROM shop_listing" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY position, id"
    conn = _connect(webapp_db)
    try:
        return [Listing.from_row(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def get(webapp_db: Path, listing_id: int) -> Listing | None:
    conn = _connect(webapp_db)
    try:
        row = conn.execute("SELECT * FROM shop_listing WHERE id = ?", (listing_id,)).fetchone()
    finally:
        conn.close()
    return Listing.from_row(row) if row else None


def save(webapp_db: Path, listing_id: int | None, values: dict) -> int:
    """Create (listing_id None) or update a listing. Returns its id."""
    cols = ("category", "name", "description", "contents", "price_gil", "price_coins",
            "sell_gil", "sell_coins", "visible", "retail_price", "patch", "notes")
    data = {k: values[k] for k in cols if k in values}
    if isinstance(data.get("contents"), list):
        data["contents"] = json.dumps(data["contents"])
    data["updated_at"] = _now()
    conn = _connect(webapp_db)
    try:
        if listing_id is None:
            # Last in its category; a new category goes after all the others.
            position = conn.execute(
                "SELECT MAX(position) + 1 FROM shop_listing WHERE category = ?",
                (data.get("category", ""),)).fetchone()[0]
            if position is None:
                position = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM shop_listing").fetchone()[0]
            data["position"] = position
            keys = list(data)
            cur = conn.execute(
                f"INSERT INTO shop_listing ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                [data[k] for k in keys])
            listing_id = cur.lastrowid
        else:
            conn.execute(f"UPDATE shop_listing SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?",
                         [*data.values(), listing_id])
        conn.commit()
        return int(listing_id)
    finally:
        conn.close()


def delete(webapp_db: Path, listing_id: int) -> None:
    conn = _connect(webapp_db)
    try:
        conn.execute("DELETE FROM shop_listing WHERE id = ?", (listing_id,))
        conn.execute("DELETE FROM shop_listing_image WHERE listing_id = ?", (listing_id,))
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
#  Pictures: uploaded preview images, and game icons as the fallback thumbnail
# ════════════════════════════════════════════════════════════════════════════
def images(webapp_db: Path, listing_id: int) -> list[dict]:
    """A listing's uploaded images, cover first."""
    conn = _connect(webapp_db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM shop_listing_image WHERE listing_id = ? ORDER BY position, id",
            (listing_id,))]
    finally:
        conn.close()


def covers(webapp_db: Path, listing_ids: list[int]) -> dict[int, str]:
    """{listing id: its cover image's file} for the listings that have one."""
    if not listing_ids:
        return {}
    conn = _connect(webapp_db)
    try:
        marks = ", ".join("?" * len(listing_ids))
        out: dict[int, str] = {}
        for row in conn.execute(
                f"SELECT listing_id, file FROM shop_listing_image WHERE listing_id IN ({marks}) "
                f"ORDER BY listing_id, position, id", listing_ids):
            out.setdefault(row["listing_id"], row["file"])
        return out
    finally:
        conn.close()


def add_image(webapp_db: Path, listing_id: int, file: str) -> int:
    conn = _connect(webapp_db)
    try:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM shop_listing_image WHERE listing_id = ?",
            (listing_id,)).fetchone()[0]
        cur = conn.execute("INSERT INTO shop_listing_image (listing_id, file, position, created_at) "
                           "VALUES (?, ?, ?, ?)", (listing_id, file, position, _now()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def remove_image(webapp_db: Path, listing_id: int, image_id: int) -> str:
    """Forget an image; returns its file so the caller can delete it ('' if none)."""
    conn = _connect(webapp_db)
    try:
        row = conn.execute("SELECT file FROM shop_listing_image WHERE id = ? AND listing_id = ?",
                           (image_id, listing_id)).fetchone()
        if not row:
            return ""
        conn.execute("DELETE FROM shop_listing_image WHERE id = ?", (image_id,))
        conn.commit()
        return row["file"]
    finally:
        conn.close()


def make_cover(webapp_db: Path, listing_id: int, image_id: int) -> None:
    conn = _connect(webapp_db)
    try:
        conn.execute(
            "UPDATE shop_listing_image SET position = (SELECT COALESCE(MIN(position), 0) - 1 "
            "FROM shop_listing_image WHERE listing_id = ?) WHERE id = ? AND listing_id = ?",
            (listing_id, image_id, listing_id))
        conn.commit()
    finally:
        conn.close()


def all_image_files(webapp_db: Path, listing_id: int | None = None) -> list[str]:
    conn = _connect(webapp_db)
    try:
        if listing_id is None:
            rows = conn.execute("SELECT file FROM shop_listing_image")
        else:
            rows = conn.execute("SELECT file FROM shop_listing_image WHERE listing_id = ?",
                                (listing_id,))
        return [r["file"] for r in rows]
    finally:
        conn.close()


def icon_of(listing: "Listing") -> int:
    """The game icon that stands for a listing: its first linked item's (0 if none).

    The listing must have been through ItemIndex.link_listing."""
    for entry in listing.linked:
        item = entry.get("item")
        if item and item.get("icon"):
            return int(item["icon"])
    return 0


def set_prices(webapp_db: Path, category: str | None, price_gil: int | None,
               price_coins: int | None) -> int:
    """Set the gil and/or coin price of every listing (in one category, or all)."""
    sets, params = [], []
    if price_gil is not None:
        sets.append("price_gil = ?")
        params.append(price_gil)
    if price_coins is not None:
        sets.append("price_coins = ?")
        params.append(price_coins)
    if not sets:
        return 0
    sql = f"UPDATE shop_listing SET {', '.join(sets)}, updated_at = ?"
    params.append(_now())
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    conn = _connect(webapp_db)
    try:
        count = conn.execute(sql, params).rowcount
        conn.commit()
        return count
    finally:
        conn.close()


# Contents are edited as text, one item per line: "3 x Fantasia", "Fantasia",
# "Ballroom Etiquette - Improper Praise #40359" (an ID picks one of several items
# that share a name.)
def contents_to_text(contents: list) -> str:
    lines = []
    for e in contents:
        line = (f"{e['qty']} x " if int(e.get("qty", 1)) != 1 else "") + e["name"]
        if e.get("item_id"):
            line += f" #{e['item_id']}"
        lines.append(line)
    return "\n".join(lines)


def contents_from_text(text: str, previous: list | None = None) -> list:
    """Parse the editor's text. Alternative names from the seed survive edits of
    the same line, so linking can still try them."""
    alts = {e["name"]: e.get("alt", []) for e in previous or []}
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        qty = 1
        head, sep, rest = line.partition(" x ")
        if sep and head.strip().isdigit():
            qty, line = max(1, int(head.strip())), rest.strip()
        item_id = None
        if "#" in line:
            name_part, _, id_part = line.rpartition("#")
            if id_part.strip().isdigit():
                item_id, line = int(id_part.strip()), name_part.strip()
        entry = {"name": line, "qty": qty}
        if item_id:
            entry["item_id"] = item_id
        if alts.get(line):
            entry["alt"] = alts[line]
        out.append(entry)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Linking contents to game items
# ════════════════════════════════════════════════════════════════════════════
class ItemIndex:
    """item-index.json from kawari-questdump items: every named item."""

    def __init__(self, items: list | None = None, built: float = 0.0):
        self.items = {int(i["id"]): i for i in items or []}
        self.by_name: dict[str, list[dict]] = {}
        for item in self.items.values():
            self.by_name.setdefault(item["name"].casefold(), []).append(item)
        self.built = built

    @classmethod
    def load(cls, root: Path) -> "ItemIndex":
        path = Path(root) / ITEM_INDEX
        if not path.exists():
            return cls()
        try:
            return cls(json.loads(path.read_text()), path.stat().st_mtime)
        except (ValueError, OSError):
            return cls()

    def __bool__(self) -> bool:
        return bool(self.items)

    def link(self, entry: dict) -> dict:
        """The entry with "item" (the game item) or "problem" filled in."""
        out = dict(entry)
        if not self:
            out["problem"] = "The item list hasn't been read from the game yet."
            return out
        if entry.get("item_id"):
            item = self.items.get(int(entry["item_id"]))
            if item:
                out["item"] = item
            else:
                out["problem"] = f"There's no item {entry['item_id']} in the game."
            return out
        # Longest name first: "Zero's Luminary Gauntlets" before "Zero's Gauntlets".
        names = sorted([entry["name"], *entry.get("alt", [])], key=len, reverse=True)
        for name in names:
            found = self.by_name.get(name.casefold(), [])
            if len(found) == 1:
                out["item"] = found[0]
                return out
            if len(found) > 1:
                ids = ", ".join(str(i["id"]) for i in found[:8])
                out["problem"] = (f"{len(found)} items are called “{name}” ({ids}): add the right "
                                  f"one's ID, like “{name} #{found[0]['id']}”.")
                return out
        out["problem"] = f"No item is called “{entry['name']}”."
        return out

    def link_listing(self, listing: Listing) -> Listing:
        listing.linked = [self.link(e) for e in listing.contents]
        return listing

    def search(self, query: str, limit: int = 30) -> list[dict]:
        q = query.casefold().strip()
        if not q:
            return []
        hits = [i for i in self.items.values() if q in i["name"].casefold()]
        hits.sort(key=lambda i: (not i["name"].casefold().startswith(q), len(i["name"])))
        return hits[:limit]


def link_report(webapp_db: Path, index: ItemIndex) -> dict:
    every = [index.link_listing(item) for item in listings(webapp_db, include_hidden=True)]
    ready = [item for item in every if item.ready]
    empty = [item for item in every if not item.contents]
    problems = [item for item in every if item.contents and not item.ready]
    return {"total": len(every), "ready": len(ready), "empty": empty, "problems": problems}


# ════════════════════════════════════════════════════════════════════════════
#  Play coins
# ════════════════════════════════════════════════════════════════════════════
def account_playtime(world_db: Path, content_ids: list[int]) -> int:
    """Total minutes played by these characters (Kawari's character.time_played_minutes)."""
    if not content_ids or not Path(world_db).exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{world_db}?mode=ro", uri=True, timeout=10)
        try:
            marks = ", ".join("?" * len(content_ids))
            row = conn.execute(
                f"SELECT COALESCE(SUM(time_played_minutes), 0) FROM `character` "
                f"WHERE content_id IN ({marks})", content_ids).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0)


def wallet(webapp_db: Path, user_id: int, played_minutes: int | None = None,
           cfg: dict | None = None) -> dict:
    """The account's coins, after crediting any playtime not yet counted.

    played_minutes is the account's total playtime now; None just reads the
    balance. Playtime that goes down (a character deleted) is never taken back.
    """
    cfg = cfg or settings(webapp_db)
    conn = _connect(webapp_db)
    try:
        row = conn.execute("SELECT * FROM shop_wallet WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            start = 0 if cfg.get("shop_count_past_playtime") == "1" else (played_minutes or 0)
            conn.execute("INSERT INTO shop_wallet (user_id, minutes_counted) VALUES (?, ?)",
                         (user_id, start))
            row = conn.execute("SELECT * FROM shop_wallet WHERE user_id = ?", (user_id,)).fetchone()
        balance, counted, carry, earned = (row["balance"], row["minutes_counted"], row["carry"],
                                           row["earned_total"])
        if played_minutes is not None and played_minutes > counted:
            owed = (played_minutes - counted) * coin_rate(cfg) + carry
            coins, carry = divmod(owed, 60)
            balance += coins
            earned += coins
            counted = played_minutes
            conn.execute("UPDATE shop_wallet SET balance = ?, minutes_counted = ?, carry = ?, "
                         "earned_total = ? WHERE user_id = ?",
                         (balance, counted, carry, earned, user_id))
        conn.commit()
    finally:
        conn.close()
    return {"balance": balance, "minutes_counted": counted, "earned_total": earned}


def adjust_coins(webapp_db: Path, user_id: int, amount: int, reason: str) -> int:
    """Add (or take away, with a negative amount) coins by hand. Returns the new balance."""
    wallet(webapp_db, user_id)
    conn = _connect(webapp_db)
    try:
        conn.execute("UPDATE shop_wallet SET balance = MAX(0, balance + ?) WHERE user_id = ?",
                     (amount, user_id))
        conn.execute("INSERT INTO shop_coin_log (user_id, amount, reason, created_at) "
                     "VALUES (?, ?, ?, ?)", (user_id, amount, reason, _now()))
        conn.commit()
        return conn.execute("SELECT balance FROM shop_wallet WHERE user_id = ?",
                            (user_id,)).fetchone()[0]
    finally:
        conn.close()


def wallets(webapp_db: Path) -> dict[int, dict]:
    conn = _connect(webapp_db)
    try:
        return {r["user_id"]: dict(r) for r in conn.execute("SELECT * FROM shop_wallet")}
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
#  Buying
# ════════════════════════════════════════════════════════════════════════════
def character_gil(world_db: Path, content_id: int) -> int | None:
    try:
        conn = sqlite3.connect(f"file:{world_db}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute("SELECT contents FROM inventory WHERE content_id = ?",
                               (content_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return int(json.loads(row[0])["currency"]["gil"]["quantity"])
    except (ValueError, KeyError, TypeError):
        return None


def _slots(linked: list) -> list[dict]:
    """The listing's items as mail attachment slots, splitting quantities past a stack."""
    slots = []
    for entry in linked:
        item = entry["item"]
        qty = max(1, int(entry.get("qty", 1)))
        stack = max(1, int(item.get("stack_size") or 1))
        while qty > 0:
            take = min(qty, stack)
            slots.append(dict(EMPTY_ITEM, item_id=int(item["id"]), quantity=take,
                              condition=ITEM_CONDITION_MAX))
            qty -= take
    return slots


def _message(text: str) -> str:
    """Kawari stores a letter's text as a JSON list of its bytes."""
    data = text.encode("utf-8")[:LETTER_MAX_BYTES]
    while True:
        try:
            data.decode("utf-8")
            break
        except UnicodeDecodeError:
            data = data[:-1]
    return json.dumps(list(data))


def _mail(conn: sqlite3.Connection, content_id: int, text: str, slots: list[dict]) -> int:
    """Add reward letters carrying these items to the character's mailbox. Returns how many."""
    letters = 0
    chunks = [slots[i:i + ITEMS_PER_LETTER] for i in range(0, len(slots), ITEMS_PER_LETTER)] or [[]]
    for chunk in chunks:
        attachments = {"slots": chunk + [dict(EMPTY_ITEM) for _ in range(6 - len(chunk))],
                       "kind": "Invalid"}
        next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM mail").fetchone()[0]
        # Kawari finds a letter by recipient, sender and timestamp, so two letters
        # from the shop can't share a second.
        stamp = int(time.time())
        latest = conn.execute(
            "SELECT MAX(timestamp) FROM mail WHERE recipient_content_id = ? AND sender_content_id = ?",
            (content_id, SENDER_CONTENT_ID)).fetchone()[0]
        if latest is not None and latest >= stamp:
            stamp = latest + 1
        conn.execute(
            "INSERT INTO mail (id, kind, read, timestamp, recipient_content_id, sender_content_id, "
            "message, attached_items) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
            (next_id, LETTER_REWARD, stamp, content_id, SENDER_CONTENT_ID, _message(text),
             json.dumps(attachments, separators=(",", ":"))))
        letters += 1
    return letters


def buy(webapp_db: Path, world_db: Path, *, user_id: int, character: dict, listing: Listing,
        currency: str, online: bool, played_minutes: int, site: str) -> dict:
    """Take the price and mail the listing to the character. Raises ShopError.

    listing must already be linked (ItemIndex.link_listing). online says
    whether the character is logged in on a running server.
    """
    cfg = settings(webapp_db)
    if not listing.visible:
        raise ShopError("That isn't for sale.")
    if not listing.ready:
        raise ShopError("That listing isn't linked to game items yet, so it can't be delivered.")
    content_id = int(character["content_id"])
    text = cfg["shop_letter"].replace("{site}", site).replace("{listing}", listing.name)
    slots = _slots(listing.linked)

    if currency == "gil":
        if cfg.get("shop_gil_enabled") != "1" or not listing.sell_gil:
            raise ShopError("That can't be bought with gil.")
        if online:
            raise ShopError(f"{character['name']} is logged in. Gil can only be spent while the "
                            f"character is logged out, because the server keeps a logged-in "
                            f"character's gil and saves over any change.")
        price = max(0, int(listing.price_gil))
        conn = sqlite3.connect(str(world_db), timeout=10)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT contents FROM inventory WHERE content_id = ?",
                               (content_id,)).fetchone()
            if not row:
                raise ShopError("That character has no inventory yet.")
            inventory = json.loads(row[0])
            gil = inventory["currency"]["gil"]
            if int(gil.get("quantity", 0)) < price:
                raise ShopError(f"{character['name']} has {int(gil.get('quantity', 0)):,} gil; "
                                f"this costs {price:,}.")
            gil["quantity"] = int(gil["quantity"]) - price
            conn.execute("UPDATE inventory SET contents = ? WHERE content_id = ?",
                         (json.dumps(inventory, separators=(",", ":")), content_id))
            letters = _mail(conn, content_id, text, slots)
            conn.commit()
        except ShopError:
            conn.rollback()
            raise
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            conn.rollback()
            raise ShopError(f"The purchase couldn't be saved: {exc}") from exc
        finally:
            conn.close()
    elif currency == "coins":
        if cfg.get("shop_coins_enabled") != "1" or not listing.sell_coins:
            raise ShopError(f"That can't be bought with {cfg['shop_coin_name']}.")
        price = max(0, int(listing.price_coins))
        balance = wallet(webapp_db, user_id, played_minutes, cfg)["balance"]
        if balance < price:
            raise ShopError(f"You have {balance:,} {cfg['shop_coin_name']}; this costs {price:,}.")
        web = _connect(webapp_db)
        try:
            web.execute("BEGIN IMMEDIATE")
            current = web.execute("SELECT balance FROM shop_wallet WHERE user_id = ?",
                                  (user_id,)).fetchone()[0]
            if current < price:
                raise ShopError(f"You have {current:,} {cfg['shop_coin_name']}; this costs {price:,}.")
            web.execute("UPDATE shop_wallet SET balance = balance - ? WHERE user_id = ?",
                        (price, user_id))
            # Mail before committing the coins, so a failed delivery costs nothing.
            conn = sqlite3.connect(str(world_db), timeout=10)
            try:
                letters = _mail(conn, content_id, text, slots)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise ShopError(f"The item couldn't be mailed: {exc}") from exc
            finally:
                conn.close()
            web.commit()
        except ShopError:
            web.rollback()
            raise
        finally:
            web.close()
    else:
        raise ShopError("Pick gil or coins.")

    conn = _connect(webapp_db)
    try:
        conn.execute(
            "INSERT INTO shop_purchase (user_id, content_id, character, listing_id, listing, "
            "currency, price, letters, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, content_id, character.get("name", ""), listing.id, listing.name, currency,
             price, letters, _now()))
        if currency == "coins":
            conn.execute("INSERT INTO shop_coin_log (user_id, amount, reason, created_at) "
                         "VALUES (?, ?, ?, ?)", (user_id, -price, f"Bought {listing.name}", _now()))
        conn.commit()
    finally:
        conn.close()
    return {"price": price, "letters": letters}


def purchases(webapp_db: Path, user_id: int | None = None, limit: int = 50) -> list[dict]:
    conn = _connect(webapp_db)
    try:
        if user_id is None:
            rows = conn.execute("SELECT * FROM shop_purchase ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = conn.execute("SELECT * FROM shop_purchase WHERE user_id = ? ORDER BY id DESC "
                                "LIMIT ?", (user_id, limit))
        return [dict(r) for r in rows]
    finally:
        conn.close()
