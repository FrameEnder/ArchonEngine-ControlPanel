"""Server statistics and connection details, shared by Home and How to Connect."""

from __future__ import annotations

import json
from pathlib import Path

from kawari_core import constants as C
from kawari_core import core as kcore
from kawari_core import dbtools, gamedata as G
from kawari_core.versions import BuildStore


def connection_info(settings) -> dict:
    """Everything a player needs to point a launcher at this server."""
    shell = settings.shell
    cfg = settings.server_config()
    host = shell.ip or "127.0.0.1"
    web_port = shell.web_port
    world_name = cfg.value("world", "world_id", 63)
    return {
        "host": host,
        "web_port": web_port,
        "autoconfig_url": kcore.autoconfig_url(host, web_port),
        "mode": shell.mode,
        "mode_label": C.MODE_LABELS.get(shell.mode, shell.mode),
        "world_id": world_name,
        "login_message": cfg.value("world", "login_message", "Welcome to Kawari!"),
        "lobby_host": cfg.value("lobby", "server_name", host),
        "lobby_port": cfg.value("lobby", "port", 21059),
        "world_host": cfg.value("world", "server_name", host),
        "frontier": cfg.value("frontier", "server_name", ""),
        "login": cfg.value("login", "server_name", ""),
        "patch": cfg.value("patch", "server_name", ""),
        "registration_open": bool(cfg.value("login", "enable_registration", True)),
        "services": [
            {"key": key, "label": label, "port": cfg.value(key, "port", port),
             "protocol": proto}
            for key, label, port, proto, _exposed in C.SERVICES
        ],
    }


def server_stats(settings, podman) -> dict:
    """World and population figures, from the databases and the container."""
    shell = settings.shell
    state = podman.inspect(shell.container) if podman.mode != "none" else {
        "exists": False, "running": False, "status": "unknown"}

    stats = {
        "container": shell.container,
        "exists": state.get("exists", False),
        "running": state.get("running", False),
        "status": state.get("status", "unknown"),
        "podman_mode": podman.mode,
        "characters": 0,
        "online": 0,
        "accounts": 0,
        "highest_level": 0,
        "build": "",
        "patch": "",
        "image": shell.image,
        "image_exists": False,
        "reachable": {},
    }

    if podman.mode != "none":
        stats["image_exists"] = podman.image_exists(f"{shell.image}:latest")

    rows, err = dbtools.characters(settings.world_db)
    if not err:
        stats["characters"] = len(rows)
        stats["online"] = sum(1 for r in rows if r["is_online"])

    users, err = dbtools.users(settings.login_db)
    if not err:
        stats["accounts"] = len(users)

    # Highest level across every character, for a bit of colour on the home page.
    best = 0
    try:
        for row in rows or []:
            ch_levels = dbtools.query(
                settings.world_db, "SELECT levels FROM classjob WHERE content_id = ?",
                (row["content_id"],))[1]
            if ch_levels:
                best = max(best, max(json.loads(ch_levels[0]["levels"]) or [0]))
    except (ValueError, KeyError, TypeError):
        pass
    stats["highest_level"] = best

    try:
        store = BuildStore(settings.data_dir)
        entry = store.ledger.active_entry
        if entry:
            stats["build"] = entry.short
            stats["patch"] = entry.patch_text
    except OSError:
        pass

    # Probe from an address this process can actually reach, which in a container
    # is not the advertised one. Anchor on the web port, then reuse that host.
    probe_host = settings.reachable_host(shell.web_port) or ""
    stats["probe_host"] = probe_host
    stats["probe_candidates"] = settings.probe_candidates()
    if probe_host:
        for key, label, port, _proto, _e in C.SERVICES:
            stats["reachable"][key] = kcore.probe_port(probe_host, port, 0.35)
    elif stats["running"]:
        # The container is up but nothing answers from here: say so rather than
        # showing every service as down.
        stats["probe_failed"] = True
    return stats


def human_minutes(minutes: int) -> str:
    """Play time the way the game shows it."""
    minutes = int(minutes or 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def character_detail(settings, content_id: int) -> dict:
    """Per-character figures for the profile.

    Returns both the flat lists the older layout used and a `summary` dict plus
    `groups` mapping, which is what the tiled layout reads - so a template can
    pick either without the data being computed twice.
    """
    from kawari_core import chardata

    out: dict = {"levels": [], "unlocks": [], "stats": [], "name": "",
                 "content_id": content_id, "summary": {}, "groups": {}}
    try:
        ch = chardata.load(settings.world_db, content_id)
    except Exception:                                   # noqa: BLE001
        return out

    out["name"] = ch.name
    levels = ch.get_json("classjob", "levels", [])
    exps = ch.get_json("classjob", "exp", [])
    owned = chardata.owned_soul_crystals(ch.get_json("inventory", "contents", {}))
    for slot, label, abbr, cap in G.CLASSJOB_SLOTS:
        if slot >= len(levels):
            continue
        level = int(levels[slot])
        jobs = G.jobs_for_slot(slot)
        out["levels"].append({
            "label": label, "abbr": abbr, "level": level, "cap": cap,
            "exp": int(exps[slot]) if slot < len(exps) else 0,
            "to_next": G.EXP_TO_NEXT.get(level, 0) if 0 < level < G.LEVEL_CAP else 0,
            "jobs": [{"name": name, "owned": item_id in owned}
                     for _cid, name, item_id in jobs],
        })

    for table, col, label, group, _note in chardata.BITMASKS:
        if not ch.has(table, col):
            continue
        value = ch.get_json(table, col, [])
        got, total = chardata.bitmask_stats(value)
        out["unlocks"].append({"label": label, "group": group, "set": got,
                               "total": total,
                               "percent": round(got * 100 / total, 1) if total else 0})

    inv = ch.get_json("inventory", "contents", {})
    currency = inv.get("currency", {}) if isinstance(inv, dict) else {}
    gc = int(ch.get("grand_company", "active_company", 0) or 0)
    gc_name = dict(G.GRAND_COMPANIES).get(gc, "None")
    ranks = ch.get_json("grand_company", "company_ranks", [0, 0, 0])
    gc_rank = ""
    if gc and 1 <= gc <= 3 and len(ranks) >= gc:
        gc_rank = dict(G.GC_RANKS.get(gc, [])).get(int(ranks[gc - 1]), "")
    title_id = int(ch.get("volatile", "title", 0) or 0)
    zone_id = int(ch.get("volatile", "zone_id", 0) or 0)
    # ── structured summary for the tiled layout ─────────────────────────
    current_id = int(ch.get("classjob", "current_class", 1) or 1)
    current_slot = G.CLASSJOB_EXP_INDEX.get(current_id)
    levelled = [row for row in out["levels"] if row["level"] > 0]
    groups: dict = {}
    for row in out["unlocks"]:
        groups.setdefault(row["group"], []).append(row)
    out["groups"] = groups

    def total_of(group: str) -> tuple:
        rows = groups.get(group, [])
        got = sum(r["set"] for r in rows)
        total = sum(r["total"] for r in rows)
        return got, total

    duties_got, duties_total = total_of("Duties")
    out["summary"] = {
        "current_class": G.classjob_name(current_id),
        "current_level": (int(levels[current_slot])
                          if current_slot is not None and current_slot < len(levels) else 0),
        "levelled": len(levelled),
        "slot_count": len(out["levels"]),
        "highest": max((row["level"] for row in out["levels"]), default=0),
        "gil": int((currency.get("gil") or {}).get("quantity", 0)),
        "mgp": int((currency.get("mgp") or {}).get("quantity", 0)),
        "title": dict(G.TITLES).get(title_id, ""),
        "grand_company": gc_name,
        "gc_rank": gc_rank,
        "home_point": dict(G.AETHERYTES).get(
            int(ch.get("aetheryte", "homepoint", 0) or 0), ""),
        "zone": dict(C.TERRITORIES).get(zone_id, f"Zone {zone_id}"),
        "played": human_minutes(ch.get("character", "time_played_minutes", 0)),
        "warrior_of_light": bool(ch.get("character", "warrior_of_light", 0)),
        "duties": f"{duties_got:,} / {duties_total:,}",
        "duties_percent": round(duties_got * 100 / duties_total, 1) if duties_total else 0,
    }

    out["stats"] = [
        ("Current class", G.classjob_name(current_id)),
        ("Title", dict(G.TITLES).get(title_id, "-") or "-"),
        ("Grand Company", f"{gc_name}{' - ' + gc_rank if gc_rank else ''}"),
        ("Gil", f"{int((currency.get('gil') or {}).get('quantity', 0)):,}"),
        ("MGP", f"{int((currency.get('mgp') or {}).get('quantity', 0)):,}"),
        ("Play time", f"{int(ch.get('character', 'time_played_minutes', 0) or 0):,} minutes"),
        ("Last zone", dict(C.TERRITORIES).get(zone_id, f"Zone {zone_id}")),
        ("Home point", dict(G.AETHERYTES).get(
            int(ch.get("aetheryte", "homepoint", 0) or 0), "-")),
        ("Warrior of Light", "Yes" if ch.get("character", "warrior_of_light", 0) else "No"),
    ]
    return out
