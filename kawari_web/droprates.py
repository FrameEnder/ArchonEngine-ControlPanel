"""Dashboard -> Drop rates: how often and how much items drop, everywhere down to one duty or zone and one category of
item, and how much gil and EXP each source gives (2.32.0; before, only a duty's mounts and minions).

The rates are the world server's drop-rates.json (kawari_core.droprates has the format and the rule: the nearest
setting wins); the server reads it again whenever it changes, so a change needs no restart. The page lists every duty
and open-world zone of content-index.json (kawari-questdump content) by expansion, and the items' categories and the
game's own groups of them from item-categories.json (kawari-questdump items), so no game text is kept here.
"""

from __future__ import annotations

import time
from pathlib import Path

from flask import flash, redirect, render_template, request, url_for

from kawari_core import content_progress as CP
from kawari_core import droprates as D
from kawari_core import questlab as Q

from . import auth
from .admin_base import dashboard, runner, settings
from .questlab import _active_build, _run_step, tool_status

TOOL = "kawari-questdump"
# This page's words for the game's groups of item categories (ItemUICategory.OrderMajor), as the game orders its
# inventory; a group it doesn't know is "Group N". The categories in each are listed under the table, from the game.
GROUP_LABELS = {1: "Arms", 2: "Tools", 3: "Armor", 4: "Accessories", 5: "Medicines & meals", 6: "Materials",
                7: "Other"}
# How many blank rows the category rules start with (the page's script adds more.)
BLANK_RULES = 3


def rate_path() -> Path:
    return Path(settings().db_dir) / D.RATE_FILE


def _dumps() -> Path:
    return Q.dumps_root(settings().data_dir)


def _tool() -> dict:
    """Can the active build's kawari-questdump write the item categories?"""
    status = dict(tool_status())
    build = _active_build()
    status["has_categories"] = False
    if status.get("ok") and build is not None:
        try:
            status["has_categories"] = D.CATEGORY_FILE.encode() in (build / TOOL).read_bytes()
        except OSError:
            pass
    return status


def _columns(groups: list[dict], mounts: bool) -> list[dict]:
    """The table's columns: all items, each of the game's groups, and (for duties) mounts and minions."""
    columns = [{"key": "all", "label": "All items", "most": D.MOST, "title": "Every item with no nearer rate"}]
    for group in groups:
        names = ", ".join(c["name"] for c in group["categories"])
        columns.append({"key": f"group:{group['major']}", "most": D.MOST, "title": names,
                        "label": GROUP_LABELS.get(group["major"], f"Group {group['major']}")})
    if mounts:
        columns.append({"key": D.MOUNTS_MINIONS, "label": "Mounts & minions", "most": 100,
                        "title": "The chance a mount or minion a coffer may hold drops (not a multiplier)"})
    return columns


def _row(data: dict, scope: str, columns: list[dict], content_type=None, expansion=None) -> list[dict]:
    """A row's cells: its own rate for each column, and what it comes to without one (the placeholder)."""
    own = D.setting_of(data, scope)
    inherited = D.chain(scope, content_type, expansion)[1:] if scope != "all" else []
    cells = []
    for column in columns:
        key = column["key"]
        value, _ = D.effective(data, inherited, key)
        # A group without a rate takes this row's own "all" before anything further away.
        if key.startswith("group:") and "all" in own:
            value = own["all"]
        cells.append({"name": f"i|{scope}|{key}", "value": own.get(key, ""), "inherited": value,
                      "most": column["most"]})
    return cells


def _page(data: dict):
    index = CP.load(_dumps(), _active_build())
    categories = D.load_categories(_dumps())
    groups = D.category_groups(categories)
    columns = _columns(groups, mounts=True)
    zone_columns = _columns(groups, mounts=False)
    expansion_name = lambda n: index.expansion_names.get(n) or f"Expansion {n}"  # noqa: E731

    broad = [{"label": "Everywhere", "note": "", "cells": _row(data, "all", columns)}]
    for expansion in index.expansions():
        broad.append({"label": expansion_name(expansion), "note": "expansion",
                      "cells": _row(data, f"x:{expansion}", columns)})
    types = sorted({d.content_type for d in index.duties},
                   key=lambda t: (CP.FEATURED_TYPES.index(t) if t in CP.FEATURED_TYPES else 99, t))
    for content_type in types:
        broad.append({"label": index.type_name(content_type), "note": "kind of duty",
                      "cells": _row(data, f"t:{content_type}", columns)})
    broad.append({"label": "Open world", "note": "enemies out in the zones",
                  "cells": _row(data, f"t:{D.WORLD}", columns)})

    expansions = []
    for expansion in index.expansions():
        duty_groups = []
        for content_type in types:
            duties = [d for d in index.duties if d.expansion == expansion and d.content_type == content_type]
            if duties:
                duty_groups.append({"name": index.type_name(content_type), "rows": [
                    {"name": d.name, "level": d.level, "ready": d.scripted, "set": bool(D.setting_of(data, f"d:{d.id}")),
                     "cells": _row(data, f"d:{d.id}", columns, d.content_type, d.expansion)} for d in duties]})
        zones = [{"name": z.name, "region": z.region, "ready": bool(z.spawns),
                  "set": bool(D.setting_of(data, f"z:{z.id}")),
                  "cells": _row(data, f"z:{z.id}", zone_columns, None, z.expansion)}
                 for z in index.zones if z.expansion == expansion]
        own = sum(row["set"] for group in duty_groups for row in group["rows"]) + sum(z["set"] for z in zones)
        expansions.append({"id": expansion, "name": expansion_name(expansion), "duty_groups": duty_groups,
                           "zones": zones, "own": own,
                           "duties": sum(len(group["rows"]) for group in duty_groups)})

    # The scopes and categories a rule by category can name, and the rules there are.
    scopes = [("", [("all", "Everywhere")]),
              ("Expansions", [(f"x:{e}", expansion_name(e)) for e in index.expansions()]),
              ("Kinds of content", [(f"t:{t}", index.type_name(t)) for t in types] + [(f"t:{D.WORLD}", "Open world")])]
    for expansion in index.expansions():
        duties = [(f"d:{d.id}", d.name) for d in index.duties if d.expansion == expansion]
        if duties:
            scopes.append((f"Duties: {expansion_name(expansion)}", duties))
        zones = [(f"z:{z.id}", z.name) for z in index.zones if z.expansion == expansion]
        if zones:
            scopes.append((f"Zones: {expansion_name(expansion)}", zones))
    known = {scope for _, options in scopes for scope, _ in options}
    names = {c["id"]: c["name"] for c in categories}
    rules = []
    for scope in ["all"] + [f"{kind}:{name}" for kind, part in D.PARTS.items() for name in data["items"][part]]:
        for key, value in sorted(D.setting_of(data, scope).items()):
            if key.startswith("cat:"):
                rules.append({"scope": scope, "category": int(key[4:]), "rate": value,
                              "unknown_scope": scope not in known, "unknown_category": int(key[4:]) not in names})
    return {
        "loaded": index.loaded, "categories": categories, "groups": groups, "columns": columns,
        "zone_columns": zone_columns, "broad": broad, "expansions": expansions, "scopes": scopes, "rules": rules,
        "group_labels": GROUP_LABELS, "blank_rules": BLANK_RULES,
    }


@dashboard.route("/drop-rates")
@auth.admin_required
def drop_rates():
    data, modified = D.read(rate_path())
    return render_template(
        "dashboard/drop_rates.html", section="droprates", data=data, counts=D.count(data), path=rate_path(),
        gil_sources=D.GIL_SOURCES, exp_sources=D.EXP_SOURCES, most=D.MOST, tool=_tool(),
        mounts_default=D.MOUNTS_MINIONS_DEFAULT,
        modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)) if modified else "", **_page(data))


def _unshown(old: dict, page: dict) -> dict:
    """What the file has that the page couldn't show, to keep through a save: a place the content index doesn't have
    (or every duty and zone, when there's no index), a group the item categories don't have, and the rules by
    category when the categories haven't been read."""
    kept = D.normal({})
    scopes = {scope for _, options in page["scopes"] for scope, _ in options}
    keys = {column["key"] for column in page["columns"]}
    for scope in ["all"] + [f"{kind}:{name}" for kind, part in D.PARTS.items() for name in old["items"][part]]:
        for key, value in D.setting_of(old, scope).items():
            # Every rule by category is listed once the categories are read, whatever its place.
            shown = bool(page["categories"]) if key.startswith("cat:") else scope in scopes and key in keys
            if not shown:
                D.set_rate(kept, scope, key, value)
    return kept


def from_form(form, kept: dict | None = None) -> dict:
    """The file a saved form makes: every rate typed, and nothing for a blank (the page shows the whole file, but for
    `kept`: what it couldn't show)."""
    data = kept if kept is not None else D.normal({})
    for field, value in form.items():
        parts = field.split("|")
        if len(parts) == 3 and parts[0] == "i" and D.valid_scope(parts[1]) and D.valid_key(parts[2]):
            number = D.rate(value, 100 if parts[2] == D.MOUNTS_MINIONS else D.MOST)
            if number is not None:
                D.set_rate(data, parts[1], parts[2], number)
        elif len(parts) == 2 and parts[0] in ("g", "e"):
            kind, sources = ("gil", D.GIL_SOURCES) if parts[0] == "g" else ("exp", D.EXP_SOURCES)
            number = D.rate(value)
            if number is not None and parts[1] in ["all"] + [source for source, _ in sources]:
                data[kind][parts[1]] = number
    # The rules by category: a scope, a category and a rate each.
    for scope, category, value in zip(form.getlist("rule-scope"), form.getlist("rule-category"),
                                      form.getlist("rule-rate")):
        number = D.rate(value)
        if number is not None and D.valid_scope(scope) and category.isdigit():
            D.set_rate(data, scope, f"cat:{int(category)}", number)
    return data


@dashboard.route("/drop-rates", methods=["POST"])
@auth.admin_required
def drop_rates_save():
    action = request.form.get("action", "save")
    if action == "categories":
        status = _tool()
        if not status.get("ok"):
            flash(status.get("reason") or "kawari-questdump can't run.", "err")
        elif not status["has_categories"]:
            flash("The active build's kawari-questdump can't write the item categories yet: build the fork again.",
                  "err")
        else:
            job = runner().start("Read the items and their categories from the game",
                                 [("Write item-index.json and item-categories.json", _run_step(["items"]))])
            return redirect(url_for("dashboard.job", job_id=job.id))
        return redirect(url_for("dashboard.drop_rates"))
    old, _ = D.read(rate_path())
    data = from_form(request.form, _unshown(old, _page(old)))
    D.write(rate_path(), data)
    counts = D.count(data)
    items = sum(counts[key] for key in ("everywhere", "expansions", "types", "duties", "zones"))
    flash(f"Saved: {items} item rates, {counts['gil']} for gil and {counts['exp']} for EXP. The server reads them at "
          "once.", "ok")
    return redirect(url_for("dashboard.drop_rates"))
