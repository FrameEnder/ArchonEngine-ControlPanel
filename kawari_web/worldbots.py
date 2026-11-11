"""Dashboard -> World bots: players about the world, shown as players by the fork's server (docs/src/fork/world-bots.md).

The world server reads world-bots.json from its data folder (the folder mounted at /kawari/data, beside world.db) again
whenever it changes, so a change applies within seconds, without a restart:

    {"format": 1, "enabled": true, "cities": {"129": 30, "130": 10, "132": 20}, "per_aetheryte": 1,
     "per_marketboard": [5, 10], "travel": true}

``cities`` is by TerritoryType: how many bots gather round that town's big aetheryte; every other big aetheryte in a
town or the open world gets ``per_aetheryte``. Each marketboard gets between the two ``per_marketboard`` numbers. The
towns' names come from content-index.json (kawari-questdump content, its "towns"), so no game text is kept here.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from flask import flash, redirect, render_template, request, url_for

from kawari_core import questlab as Q

from . import auth
from .admin_base import dashboard, settings

SETTINGS_FILE = "world-bots.json"
# The server's defaults (servers/world/src/server/world_bots.rs, Settings::default): Bob's numbers of 2026-11-11.
DEFAULTS = {"enabled": False, "cities": {"129": 30, "130": 10, "132": 20}, "per_aetheryte": 1,
            "per_marketboard": [5, 10], "travel": True}
LIMITS = {"city": 200, "per_aetheryte": 20, "per_marketboard": 50}


def settings_path() -> Path:
    return Path(settings().db_dir) / SETTINGS_FILE


def read_settings() -> tuple[dict, float | None]:
    """The file's settings over the defaults, and when it was saved (None without one)."""
    data = json.loads(json.dumps(DEFAULTS))
    path = settings_path()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        modified = path.stat().st_mtime
    except (OSError, ValueError):
        return data, None
    if isinstance(saved, dict):
        for key in DEFAULTS:
            if key in saved:
                data[key] = saved[key]
    return data, modified


def _count(value, limit: int) -> int | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        number = int(float(text))
    except ValueError:
        return None
    return max(0, min(limit, number))


def write_settings(data: dict) -> None:
    """Written beside the file and renamed over it, so the server never reads half of one."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".world-bots-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump({"format": 1, **data}, out, indent=1)
        out.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def towns() -> list[dict]:
    """The towns from content-index.json: id, name, region, expansion (none before the index has them)."""
    path = Q.dumps_root(settings().data_dir) / "content-index.json"
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in index.get("towns") or []:
        try:
            out.append({"id": int(entry["id"]), "name": str(entry.get("name", "")),
                        "region": str(entry.get("region", "")), "expansion": int(entry.get("ex_version", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda town: (town["expansion"], town["region"], town["name"], town["id"]))
    return out


@dashboard.route("/world-bots")
@auth.admin_required
def world_bots():
    data, modified = read_settings()
    town_list = towns()
    known = {town["id"] for town in town_list}
    cities = {int(k): v for k, v in (data.get("cities") or {}).items() if str(k).isdigit()}
    # Cities set by ID that the index doesn't name (or no index yet) are listed by their ID.
    others = sorted(t for t in cities if t not in known)
    return render_template(
        "dashboard/world_bots.html", section="worldbots", data=data, cities=cities, towns=town_list,
        others=others, path=settings_path(),
        modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)) if modified else "")


@dashboard.route("/world-bots", methods=["POST"])
@auth.admin_required
def world_bots_save():
    form = request.form
    cities = {}
    for field, value in form.items():
        if field.startswith("city-") and field[5:].isdigit():
            count = _count(value, LIMITS["city"])
            if count is not None:
                cities[field[5:]] = count
    new_id, new_count = form.get("new_city", "").strip(), _count(form.get("new_count"), LIMITS["city"])
    if new_id.isdigit() and new_count is not None:
        cities[str(int(new_id))] = new_count
    per_aetheryte = _count(form.get("per_aetheryte"), LIMITS["per_aetheryte"])
    low = _count(form.get("market_low"), LIMITS["per_marketboard"])
    high = _count(form.get("market_high"), LIMITS["per_marketboard"])
    low = DEFAULTS["per_marketboard"][0] if low is None else low
    high = DEFAULTS["per_marketboard"][1] if high is None else high
    data = {
        "enabled": form.get("enabled") == "on",
        "cities": dict(sorted(cities.items(), key=lambda kv: int(kv[0]))),
        "per_aetheryte": DEFAULTS["per_aetheryte"] if per_aetheryte is None else per_aetheryte,
        "per_marketboard": [min(low, high), max(low, high)],
        "travel": form.get("travel") == "on",
    }
    write_settings(data)
    total = sum(cities.values())
    flash(f"Saved: world bots {'on' if data['enabled'] else 'off'}, {total} round {len(cities)} cities' aetherytes, "
          f"{data['per_aetheryte']} at every other, {data['per_marketboard'][0]}-{data['per_marketboard'][1]} per "
          "marketboard. The server reads it within a few seconds.", "ok")
    return redirect(url_for("dashboard.world_bots"))
