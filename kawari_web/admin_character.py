"""The character editor, ported from the desktop app.

Each section is its own form posting to one endpoint, so a failure in one area
cannot silently discard edits in another. Every write snapshots first and is
refused while the character is logged in on a running server, because the world
server writes the character back on logout and would overwrite it.
"""

from __future__ import annotations

import json

from flask import abort, flash, redirect, render_template, request, url_for

from kawari_core import chardata, constants as C
from kawari_core import gamedata as G
from kawari_core.versions import BuildStore

from . import auth
from .admin_base import dashboard, podman, settings


@dashboard.route("/character/<int:content_id>")
@auth.admin_required
def character(content_id: int):
    s = settings()
    try:
        ch = chardata.load(s.world_db, content_id)
    except Exception as exc:                            # noqa: BLE001
        flash(str(exc), "err")
        return redirect(url_for("dashboard.database"))

    inv = ch.get_json("inventory", "contents", {})
    owned = chardata.owned_soul_crystals(inv)
    levels = ch.get_json("classjob", "levels", [])
    exps = ch.get_json("classjob", "exp", [])
    rows = []
    for slot, label, abbr, cap in G.CLASSJOB_SLOTS:
        if slot >= len(levels):
            continue
        rows.append({
            "slot": slot, "label": label, "abbr": abbr, "cap": cap,
            "level": int(levels[slot]),
            "exp": int(exps[slot]) if slot < len(exps) else 0,
            "to_next": G.exp_cap(int(levels[slot])),
            "jobs": [{"name": name, "owned": item_id in owned}
                     for _cid, name, item_id in G.jobs_for_slot(slot)],
        })

    masks = []
    for table, col, label, group, note in chardata.BITMASKS:
        if not ch.has(table, col):
            continue
        value = ch.get_json(table, col, [])
        got, total = chardata.bitmask_stats(value)
        masks.append({"table": table, "col": col, "label": label, "group": group,
                      "note": note, "set": got, "total": total,
                      "percent": round(got * 100 / total, 1) if total else 0})

    currency = inv.get("currency", {}) if isinstance(inv, dict) else {}
    position = ch.get_json("volatile", "position", {"x": 0, "y": 0, "z": 0})
    return render_template(
        "dashboard/character.html", section="database", ch=ch, rows=rows, masks=masks,
        currencies=[(key, label, cap, int((currency.get(key) or {}).get("quantity", 0)))
                    for key, label, cap in chardata.CURRENCIES if key in currency],
        position=position,
        territories=C.TERRITORIES, aetherytes=G.AETHERYTES, titles=G.TITLES,
        classjobs=G.CLASSJOBS, gcs=G.GRAND_COMPANIES, gc_ranks=G.GC_RANKS,
        cities=G.CITY_STATES, remakes=G.REMAKE_MODES,
        gc_rank_values=ch.get_json("grand_company", "company_ranks", [0, 0, 0]),
        buddy_levels=ch.get_json("buddy", "levels", [0, 0, 0]),
        favourites=ch.get_json("aetheryte", "favorite_aetherytes", []),
        snapshots=chardata.list_snapshots(s.data_dir, content_id),
        online=chardata.is_online(s.world_db, content_id),
        server_running=_server_running())


def _server_running() -> bool:
    p = podman()
    return p.mode != "none" and p.inspect(settings().shell.container).get("running", False)


def _guarded(content_id: int):
    """Load a character, refusing if the world server could overwrite the edit."""
    s = settings()
    if chardata.is_online(s.world_db, content_id) and _server_running():
        return None, ("This character is logged in on a running server. The world "
                      "server writes it back on logout and would overwrite the edit.")
    try:
        return chardata.load(s.world_db, content_id), ""
    except Exception as exc:                            # noqa: BLE001
        return None, str(exc)


def _commit(ch, content_id: int, label: str) -> str:
    s = settings()
    store = BuildStore(s.data_dir)
    chardata.snapshot(s.data_dir, s.world_db, content_id, label, store.ledger.active)
    delta = chardata.save(s.world_db, ch)
    return f"{sum(len(v) for v in delta.values())} column(s) across {len(delta)} table(s)"


def _number(name: str, default=0, cast=int):
    try:
        return cast(request.form.get(name, default))
    except (TypeError, ValueError):
        return default


@dashboard.route("/character/<int:content_id>/<section>", methods=["POST"])
@auth.admin_required
def edit_character(content_id: int, section: str):
    ch, error = _guarded(content_id)
    if error:
        flash(error, "err")
        return redirect(url_for("dashboard.character", content_id=content_id))

    handler = {
        "identity": _identity, "location": _location, "classes": _classes,
        "currency": _currency, "company": _company, "unlocks": _unlocks,
        "chocobo": _chocobo, "social": _social, "crystals": _crystals,
    }.get(section)
    if handler is None:
        abort(404)

    try:
        note = handler(ch, content_id)
    except Exception as exc:                            # noqa: BLE001
        flash(f"Could not apply: {exc}", "err")
        return redirect(url_for("dashboard.character", content_id=content_id))

    if not ch.dirty:
        flash("Nothing changed.", "info")
    else:
        written = _commit(ch, content_id, f"before-{section}")
        flash(f"Saved {written}. {note or ''}".strip(), "ok")
    return redirect(url_for("dashboard.character", content_id=content_id))


# ── sections ────────────────────────────────────────────────────────────────
def _identity(ch, content_id):
    name = (request.form.get("name") or "").strip()
    if name and name != ch.name:
        if chardata.name_taken(settings().world_db, name, excluding=content_id):
            raise ValueError(f"another character is already called '{name}'")
        ch.set("character", "name", name)
    ch.set("character", "gm_rank", _number("gm_rank"))
    if ch.has("character", "warrior_of_light"):
        ch.set("character", "warrior_of_light", 1 if request.form.get("wol") == "on" else 0)
    ch.set("character", "time_played_minutes", _number("time_played"))
    if ch.has("customize"):
        ch.set("customize", "city_state", _number("city_state", 1))
        ch.set("customize", "remake_mode", _number("remake_mode"))
    if ch.has("classjob"):
        ch.set("classjob", "first_class", _number("first_class", 1))
    return ""


def _location(ch, content_id):
    ch.set("volatile", "zone_id", _number("zone_id"))
    ch.set("volatile", "position", json.dumps(
        {"x": _number("x", 0.0, float), "y": _number("y", 0.0, float),
         "z": _number("z", 0.0, float)}, separators=(",", ":")))
    ch.set("volatile", "rotation", _number("rotation", 0.0, float))
    ch.set("volatile", "title", _number("title"))
    ch.set("volatile", "current_mount", _number("mount"))
    if ch.has("aetheryte"):
        ch.set("aetheryte", "homepoint", _number("homepoint"))
        ch.set("aetheryte", "free_aetheryte", _number("free_aetheryte"))
        favourites = [_number(f"fav{i}") for i in range(4)]
        while favourites and favourites[-1] == 0:
            favourites.pop()
        ch.set_json("aetheryte", "favorite_aetherytes", favourites)
    return "Coordinates are stored unscaled; //gm pos takes them multiplied by 100."


def _classes(ch, content_id):
    levels = ch.get_json("classjob", "levels", [])
    exps = ch.get_json("classjob", "exp", [])
    bulk = request.form.get("bulk", "")
    for slot, _label, _abbr, cap in G.CLASSJOB_SLOTS:
        if slot >= len(levels):
            continue
        if bulk == "max_all":
            level = cap
        elif bulk == "max_combat" and (slot <= 6 or 18 <= slot <= 32):
            level = cap
        elif bulk == "max_crafters" and 7 <= slot <= 14:
            level = cap
        elif bulk == "max_gatherers" and 15 <= slot <= 17:
            level = cap
        else:
            level = min(_number(f"level{slot}", levels[slot]), cap)
        levels[slot] = level
        exp = 0 if bulk else _number(f"exp{slot}", exps[slot] if slot < len(exps) else 0)
        if slot < len(exps):
            exps[slot] = min(exp, G.exp_cap(level))
    ch.set_json("classjob", "levels", levels)
    ch.set_json("classjob", "exp", exps)
    ch.set("classjob", "current_class", _number("current_class", 1))
    ch.set("classjob", "rested_exp", _number("rested_exp"))

    if request.form.get("grant_crystals") == "1":
        inv = ch.get_json("inventory", "contents", {})
        granted = []
        for slot, _label, _abbr, _cap in G.CLASSJOB_SLOTS:
            if slot >= len(levels) or levels[slot] <= 0:
                continue
            for _cid, name, item_id in G.jobs_for_slot(slot):
                if chardata.grant_soul_crystal(inv, item_id):
                    granted.append(name)
        if granted:
            ch.set("inventory", "contents", json.dumps(inv, separators=(",", ":")))
            return f"Granted {len(granted)} soul crystal(s)."
    return ""


def _currency(ch, content_id):
    inv = ch.get_json("inventory", "contents", {})
    for key, _label, cap in chardata.CURRENCIES:
        if key in inv.get("currency", {}):
            inv["currency"][key]["quantity"] = max(0, min(cap, _number(f"cur_{key}")))
    ch.set("inventory", "contents", json.dumps(inv, separators=(",", ":")))
    return ""


def _crystals(ch, content_id):
    inv = ch.get_json("inventory", "contents", {})
    amount = max(0, min(chardata.CRYSTAL_CAP, _number("amount", chardata.CRYSTAL_CAP)))
    for key in inv.get("crystals", {}):
        inv["crystals"][key]["quantity"] = amount
    ch.set("inventory", "contents", json.dumps(inv, separators=(",", ":")))
    return f"All shards and crystals set to {amount:,}."


def _company(ch, content_id):
    ch.set("grand_company", "active_company", _number("active_company"))
    ch.set_json("grand_company", "company_ranks",
                [_number("rank1"), _number("rank2"), _number("rank3")])
    return ""


def _unlocks(ch, content_id):
    table = request.form.get("table", "")
    col = request.form.get("col", "")
    fill = 0xFF if request.form.get("value") == "all" else 0
    if not ch.has(table, col):
        raise ValueError(f"{table}.{col} is not in this database")
    # Mirrors Bitmask::set_all(): vec![0xFF; N] at the schema's own length.
    ch.set_json(table, col, chardata.filled(table, col, fill))
    return f"{table}.{col} {'filled' if fill else 'cleared'}."


def _chocobo(ch, content_id):
    ch.set("buddy", "name", (request.form.get("buddy_name") or "").strip()[:20])
    for col in ("rank", "stars", "exp", "color"):
        ch.set("buddy", col, _number(f"buddy_{col}"))
    ch.set_json("buddy", "levels", [_number(f"buddy_level{i}") for i in range(3)])
    return ""


def _social(ch, content_id):
    if ch.has("search_info"):
        ch.set("search_info", "comment", (request.form.get("comment") or "")[:60])
        ch.set("search_info", "online_status", _number("online_status"))
        languages = sum(bit for bit in (1, 2, 4, 8)
                        if request.form.get(f"lang{bit}") == "on")
        ch.set("search_info", "selected_languages", languages)
    if ch.has("mentor"):
        for col in ("is_battle", "is_trade", "is_novice", "is_returner"):
            ch.set("mentor", col, 1 if request.form.get(col) == "on" else 0)
    return ""


# ── snapshots ───────────────────────────────────────────────────────────────
@dashboard.route("/character/<int:content_id>/snapshot", methods=["POST"])
@auth.admin_required
def snapshot_character(content_id: int):
    s = settings()
    label = (request.form.get("label") or "manual").strip()[:40]
    store = BuildStore(s.data_dir)
    chardata.snapshot(s.data_dir, s.world_db, content_id, label, store.ledger.active)
    flash("Snapshot taken.", "ok")
    return redirect(url_for("dashboard.character", content_id=content_id))


@dashboard.route("/character/<int:content_id>/restore", methods=["POST"])
@auth.admin_required
def restore_snapshot(content_id: int):
    s = settings()
    name = request.form.get("snapshot", "")
    match = next((x for x in chardata.list_snapshots(s.data_dir, content_id)
                  if x["path"].name == name), None)
    if not match:
        flash("That snapshot no longer exists.", "err")
        return redirect(url_for("dashboard.character", content_id=content_id))
    _ch, error = _guarded(content_id)
    if error:
        flash(error, "err")
        return redirect(url_for("dashboard.character", content_id=content_id))
    store = BuildStore(s.data_dir)
    chardata.snapshot(s.data_dir, s.world_db, content_id, "before-restore",
                      store.ledger.active)
    try:
        tables = chardata.restore(s.world_db, content_id, match["rows"])
    except Exception as exc:                            # noqa: BLE001
        flash(f"Restore failed: {exc}", "err")
        return redirect(url_for("dashboard.character", content_id=content_id))
    flash(f"Restored {len(tables)} table(s) from {match['taken_at']}.", "ok")
    return redirect(url_for("dashboard.character", content_id=content_id))


@dashboard.route("/character/<int:content_id>/snapshot/delete", methods=["POST"])
@auth.admin_required
def delete_snapshot(content_id: int):
    s = settings()
    name = request.form.get("snapshot", "")
    match = next((x for x in chardata.list_snapshots(s.data_dir, content_id)
                  if x["path"].name == name), None)
    if not match:
        flash("That snapshot no longer exists.", "err")
    else:
        try:
            match["path"].unlink()
            flash(f"Deleted the snapshot from {match['taken_at']}.", "ok")
        except OSError as exc:
            flash(f"Could not delete: {exc}", "err")
    return redirect(url_for("dashboard.character", content_id=content_id))


@dashboard.route("/character/<int:content_id>/diff/<name>")
@auth.admin_required
def snapshot_diff(content_id: int, name: str):
    s = settings()
    match = next((x for x in chardata.list_snapshots(s.data_dir, content_id)
                  if x["path"].name == name), None)
    if not match:
        abort(404)
    live = chardata.load(s.world_db, content_id).rows
    return render_template("dashboard/diff.html", section="database",
                           lines=chardata.diff_rows(match["rows"], live),
                           snapshot=match, content_id=content_id)


@dashboard.route("/character/<int:content_id>/delete", methods=["POST"])
@auth.admin_required
def delete_character(content_id: int):
    s = settings()
    if (request.form.get("confirm") or "").strip() != request.form.get("name", "\0"):
        flash("Type the character's name exactly to confirm deletion.", "err")
        return redirect(url_for("dashboard.character", content_id=content_id))
    s.backups.create("predelete")
    store = BuildStore(s.data_dir)
    chardata.snapshot(s.data_dir, s.world_db, content_id, "before-delete",
                      store.ledger.active)
    ok, error = chardata.delete_character(s.world_db, content_id)
    flash("Character deleted. A backup and snapshot were taken first." if ok
          else f"Delete failed: {error}", "ok" if ok else "err")
    return redirect(url_for("dashboard.database"))
