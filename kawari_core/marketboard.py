"""The marketboard's prices: the rates per tab and per search category, each
item's own prices, and the settings, all in Kawari's world.db (the
``marketboard_*`` tables, from the migration ``update-2026-10-17_marketboard``).
The world server reads them whenever the board is used, so a change here applies
the next time someone looks, with no restart.

An item's price is ``per_level x level + rarity bonus``; the level is its equip
level or its item level (``level_source``), and HQ multiplies the bonus by
``hq_multiplier``. A category's rates replace only the values it sets; an item's
own price replaces the computed one. ``price`` below must stay the same as
``servers/world/src/marketboard.rs`` in Kawari (see the fork's marketboard.md).

Items, their levels and rarity, and the search categories come from the game
install: ``kawari-questdump items`` writes ``item-index.json`` and
``marketboard-categories.json``. Nothing here is game text.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES_FILE = "marketboard-categories.json"
ITEM_INDEX = "item-index.json"

# The marketboard's tabs (ItemSearchCategory.Category).
TABS = {1: "Weapons", 2: "Armor", 3: "Items", 4: "Housing"}
# Rarity as the bonus index: white, pink, green, blue, purple (Item.Rarity 1, 7, 2, 3, 4).
RARITIES = ["white", "pink", "green", "blue", "purple"]
RARITY_INDEX = {1: 0, 7: 1, 2: 2, 3: 3, 4: 4}
LEVEL_SOURCES = {"equip": "Equip level", "item": "Item level"}

EQUIPMENT_BONUS = [0, 2000, 4000, 8000, 20000]
# The built-in rates (Bob's, 2026-10-17), the same as the migration seeds and the
# server falls back to.
DEFAULT_RATES = {
    1: {"per_level": 100, "level_source": "equip", "rarity": EQUIPMENT_BONUS, "hq_multiplier": 2},
    2: {"per_level": 100, "level_source": "equip", "rarity": EQUIPMENT_BONUS, "hq_multiplier": 2},
    3: {"per_level": 3, "level_source": "item", "rarity": [b // 100 for b in EQUIPMENT_BONUS],
        "hq_multiplier": 2},
    4: {"per_level": 50, "level_source": "item", "rarity": EQUIPMENT_BONUS, "hq_multiplier": 2},
}
SETTINGS = {
    "listings_per_item": ("Listings per item", 50),
    "hq_listings": ("Of which HQ (items that can be)", 10),
    "max_stack": ("Largest stack in a listing", 99),
    "tax_percent": ("Tax buyers pay (%)", 5),
}
RATE_COLUMNS = ["per_level", "level_source", "rarity_white", "rarity_pink", "rarity_green",
                "rarity_blue", "rarity_purple", "hq_multiplier"]
INT_MAX = 2_147_483_647


class MarketboardError(Exception):
    """Something the page should show instead of the board."""


# Prices -------------------------------------------------------------------------------------------

def merge(base: dict, changes: dict) -> dict:
    """``base`` rates with a category's ``changes`` (None keeps the base's)."""
    out = {"per_level": base["per_level"], "level_source": base["level_source"],
           "rarity": list(base["rarity"]), "hq_multiplier": base["hq_multiplier"]}
    for key in ("per_level", "level_source", "hq_multiplier"):
        if changes.get(key) is not None:
            out[key] = changes[key]
    for i, bonus in enumerate(changes.get("rarity") or [None] * 5):
        if bonus is not None:
            out["rarity"][i] = bonus
    return out


def price(item: dict, rates: dict, hq: bool) -> int:
    """One of the item, in gil (as marketboard::price in Kawari)."""
    level = item.get("level_equip", 0) if rates["level_source"] == "equip" else item.get("level_item", 0)
    bonus = rates["rarity"][RARITY_INDEX.get(item.get("rarity", 1), 0)]
    if hq:
        bonus *= rates["hq_multiplier"]
    return max(1, rates["per_level"] * level + bonus)


# The game's items ---------------------------------------------------------------------------------

@dataclass
class Category:
    id: int
    name: str
    tab: int
    order: int
    items: list = field(default_factory=list)


class Board:
    """The marketboard as the game lays it out: tabs, their categories, each one's items."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.categories: dict[int, Category] = {}
        self.items: dict[int, dict] = {}
        self.problem = ""
        cats = self.root / CATEGORIES_FILE
        index = self.root / ITEM_INDEX
        if not cats.exists() or not index.exists():
            self.problem = "missing"
            return
        try:
            for row in json.loads(cats.read_text()):
                self.categories[int(row["id"])] = Category(int(row["id"]), row.get("name", ""),
                                                           int(row["tab"]), int(row.get("order", 0)))
            items = json.loads(index.read_text())
        except (OSError, ValueError, KeyError) as exc:
            self.problem = f"couldn't read the item list: {exc}"
            return
        if items and "search_category" not in items[0]:
            self.problem = "old"
            return
        for item in items:
            cat = self.categories.get(int(item.get("search_category") or 0))
            if cat is None or item.get("untradable"):
                continue
            self.items[int(item["id"])] = item
            cat.items.append(item)
        for cat in self.categories.values():
            cat.items.sort(key=lambda i: int(i["id"]))
        self.built = index.stat().st_mtime

    def __bool__(self) -> bool:
        return not self.problem

    def tab_categories(self, tab: int) -> list[Category]:
        return sorted((c for c in self.categories.values() if c.tab == tab),
                      key=lambda c: (c.order, c.id))

    def search(self, query: str, limit: int = 200) -> list[dict]:
        q = query.casefold().strip()
        if not q:
            return []
        hits = [i for i in self.items.values() if q in i["name"].casefold()]
        hits.sort(key=lambda i: (not i["name"].casefold().startswith(q), len(i["name"]), i["id"]))
        return hits[:limit]


# world.db -----------------------------------------------------------------------------------------

def _connect(world_db: Path, write: bool = False) -> sqlite3.Connection:
    if not Path(world_db).exists():
        raise MarketboardError("There's no world database yet: start the server once.")
    if write:
        conn = sqlite3.connect(str(world_db), timeout=10)
    else:
        conn = sqlite3.connect(f"file:{world_db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "marketboard_rate" not in have:
        conn.close()
        raise MarketboardError("The world database has no marketboard tables yet: update the server "
                               "to a build with the marketboard and start it once.")
    return conn


def _changes(row) -> dict:
    return {
        "per_level": row["per_level"],
        "level_source": row["level_source"] if row["level_source"] in LEVEL_SOURCES else None,
        "rarity": [row[f"rarity_{r}"] for r in RARITIES],
        "hq_multiplier": row["hq_multiplier"],
    }


@dataclass
class Prices:
    """Everything the board's prices come from."""
    tabs: dict            # tab -> full rates
    categories: dict      # category id -> changes
    items: dict           # item id -> (nq or None, hq or None)
    settings: dict
    sold: dict            # item id -> how many sales

    def rates(self, tab: int, category: int) -> dict:
        return merge(self.tabs.get(tab, DEFAULT_RATES[3]), self.categories.get(category, {}))

    def item_prices(self, item: dict, category: Category) -> dict:
        rates = self.rates(category.tab, category.id)
        own_nq, own_hq = self.items.get(int(item["id"]), (None, None))
        return {
            "nq": own_nq if own_nq is not None else price(item, rates, False),
            "hq": own_hq if own_hq is not None else price(item, rates, True),
            "computed_nq": price(item, rates, False),
            "computed_hq": price(item, rates, True),
            "own_nq": own_nq,
            "own_hq": own_hq,
        }


def load(world_db: Path) -> Prices:
    with _connect(world_db) as conn:
        tabs = {tab: dict(rates) for tab, rates in DEFAULT_RATES.items()}
        categories = {}
        for row in conn.execute("SELECT * FROM marketboard_rate"):
            scope = row["scope"]
            kind, _, number = scope.partition(":")
            if not number.isdigit():
                continue
            if kind == "tab" and int(number) in tabs:
                tabs[int(number)] = merge(DEFAULT_RATES[int(number)], _changes(row))
            elif kind == "category":
                categories[int(number)] = _changes(row)
        items = {r["item_id"]: (r["nq_price"], r["hq_price"])
                 for r in conn.execute("SELECT * FROM marketboard_item")}
        settings = {key: default for key, (_, default) in SETTINGS.items()}
        for row in conn.execute("SELECT key, value FROM marketboard_setting"):
            settings[row["key"]] = row["value"]
        sold = {r[0]: r[1] for r in conn.execute(
            "SELECT item_id, COUNT(*) FROM marketboard_sale GROUP BY item_id")}
    return Prices(tabs, categories, items, settings, sold)


def _number(value, minimum: int = 0) -> int | None:
    """A form field as a whole number, None if it's empty; MarketboardError if it isn't one."""
    text = str(value if value is not None else "").replace(",", "").strip()
    if text == "":
        return None
    if not text.isdigit():
        raise MarketboardError(f"“{value}” isn't a whole number.")
    return max(minimum, min(INT_MAX, int(text)))


def save_rates(world_db: Path, scope: str, form: dict, keep_blank: bool) -> None:
    """A tab's (every value required: blank means the built-in one) or a category's (blank means
    the tab's) rates."""
    values = {"per_level": _number(form.get("per_level")),
              "hq_multiplier": _number(form.get("hq_multiplier"))}
    source = (form.get("level_source") or "").strip()
    values["level_source"] = source if source in LEVEL_SOURCES else None
    for r in RARITIES:
        values[f"rarity_{r}"] = _number(form.get(f"rarity_{r}"))
    if not keep_blank:
        tab = int(scope.split(":")[1])
        base = DEFAULT_RATES[tab]
        defaults = {"per_level": base["per_level"], "level_source": base["level_source"],
                    "hq_multiplier": base["hq_multiplier"],
                    **{f"rarity_{r}": base["rarity"][i] for i, r in enumerate(RARITIES)}}
        values = {k: (v if v is not None else defaults[k]) for k, v in values.items()}
    with _connect(world_db, write=True) as conn:
        if keep_blank and all(v is None for v in values.values()):
            conn.execute("DELETE FROM marketboard_rate WHERE scope = ?", (scope,))
            return
        conn.execute(
            f"INSERT OR REPLACE INTO marketboard_rate (scope, {', '.join(RATE_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' * len(RATE_COLUMNS))})",
            (scope, *[values[c] for c in RATE_COLUMNS]))


def clear_rates(world_db: Path, scope: str) -> None:
    """A category back to its tab's rates, or a tab back to the built-in ones."""
    with _connect(world_db, write=True) as conn:
        conn.execute("DELETE FROM marketboard_rate WHERE scope = ?", (scope,))


def save_item_prices(world_db: Path, prices: dict) -> int:
    """{item id: (nq or None, hq or None)}; both None removes the item's own prices. Returns how
    many items have their own prices afterwards among those given."""
    own = 0
    with _connect(world_db, write=True) as conn:
        for item_id, (nq, hq) in prices.items():
            if nq is None and hq is None:
                conn.execute("DELETE FROM marketboard_item WHERE item_id = ?", (item_id,))
            else:
                own += 1
                conn.execute("INSERT OR REPLACE INTO marketboard_item (item_id, nq_price, hq_price) "
                             "VALUES (?, ?, ?)", (item_id, nq, hq))
    return own


def save_settings(world_db: Path, form: dict) -> None:
    values = {}
    for key, (_, default) in SETTINGS.items():
        number = _number(form.get(key))
        values[key] = default if number is None else number
    values["max_stack"] = max(1, values["max_stack"])
    values["hq_listings"] = min(values["hq_listings"], values["listings_per_item"])
    with _connect(world_db, write=True) as conn:
        for key, value in values.items():
            conn.execute("INSERT OR REPLACE INTO marketboard_setting (key, value) VALUES (?, ?)",
                         (key, value))


def recent_sales(world_db: Path, limit: int = 50) -> list[dict]:
    with _connect(world_db) as conn:
        sales = [dict(r) for r in conn.execute(
            "SELECT * FROM marketboard_sale ORDER BY id DESC LIMIT ?", (limit,))]
    for sale in sales:
        sale["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(sale["sold_at"]))
    return sales
