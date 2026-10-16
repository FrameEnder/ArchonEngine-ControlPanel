"""The Shop tab: browse the store's listings, buy with gil or play coins, and
(administrators) edit every listing and the shop's settings. The logic is
kawari_core.shop."""

from __future__ import annotations

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)

from kawari_core import chardata
from kawari_core import shop as S

from . import auth, webdb

shop = Blueprint("shop", __name__, url_prefix="/shop")

PAGE_SIZE = 60
TOOL = "kawari-questdump"


def _settings():
    return current_app.config["SETTINGS"]


def _index() -> S.ItemIndex:
    from kawari_core import questlab as Q
    return S.ItemIndex.load(Q.dumps_root(_settings().data_dir))


def _server_running() -> bool:
    podman = current_app.config["PODMAN"]
    if podman.mode == "none":
        return False
    try:
        return podman.inspect(_settings().shell.container).get("running", False)
    except Exception:                                   # noqa: BLE001
        return False


def _characters() -> list[dict]:
    if not getattr(g, "user", None):
        return []
    s = _settings()
    return auth.characters_for(s.world_db, s.login_db, g.user["user_id"])


def _selected(characters: list[dict]) -> dict | None:
    """The character purchases go to: the one picked, else the first."""
    if not characters:
        return None
    wanted = session.get("shop_character")
    for ch in characters:
        if ch["content_id"] == wanted:
            return ch
    return characters[0]


def _context(characters: list[dict] | None = None) -> dict:
    """What every shop page shows: currencies, the buyer's character and wallet."""
    s = _settings()
    cfg = S.settings(s.webapp_db)
    out = {"cfg": cfg, "coin_name": cfg["shop_coin_name"], "characters": [], "selected": None,
           "wallet": None, "gil": None, "online": False,
           "categories": S.categories(s.webapp_db, include_hidden=bool(g.user and g.user.get("is_admin")))}
    if g.user:
        characters = _characters() if characters is None else characters
        selected = _selected(characters)
        played = S.account_playtime(s.world_db, [c["content_id"] for c in characters])
        out.update({
            "characters": characters, "selected": selected,
            "wallet": S.wallet(s.webapp_db, g.user["user_id"], played, cfg),
            "played_minutes": played,
        })
        if selected:
            out["gil"] = S.character_gil(s.world_db, selected["content_id"])
            out["online"] = (chardata.is_online(s.world_db, selected["content_id"])
                             and _server_running())
    return out


def _is_admin() -> bool:
    return bool(getattr(g, "user", None) and g.user.get("is_admin"))


# ════════════════════════════════════════════════════════════════════════════
#  Browsing and buying
# ════════════════════════════════════════════════════════════════════════════
@shop.route("/")
def browse():
    s = _settings()
    category = request.args.get("category") or ""
    query = (request.args.get("q") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    ctx = _context()
    if not category and not query and ctx["categories"]:
        category = ctx["categories"][0][0]
    rows = S.listings(s.webapp_db, category or None, query, include_hidden=_is_admin())
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    index = _index()
    shown = [index.link_listing(item) for item in rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]]
    return render_template("shop/browse.html", listings=shown, count=len(rows), page=page,
                           pages=pages, category=category, query=query, index_ready=bool(index),
                           **ctx)


@shop.route("/character", methods=["POST"])
@auth.login_required
def pick_character():
    try:
        content_id = int(request.form.get("content_id", "0"))
    except ValueError:
        abort(400)
    if content_id not in {c["content_id"] for c in _characters()}:
        abort(403)
    session["shop_character"] = content_id
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/shop") else url_for("shop.browse"))


@shop.route("/item/<int:listing_id>")
def item(listing_id: int):
    s = _settings()
    listing = S.get(s.webapp_db, listing_id)
    if not listing or (not listing.visible and not _is_admin()):
        abort(404)
    _index().link_listing(listing)
    return render_template("shop/item.html", listing=listing, **_context())


@shop.route("/item/<int:listing_id>/buy", methods=["POST"])
@auth.login_required
def buy(listing_id: int):
    s = _settings()
    listing = S.get(s.webapp_db, listing_id)
    if not listing:
        abort(404)
    _index().link_listing(listing)
    characters = _characters()
    selected = _selected(characters)
    if not selected:
        flash("You need a character to deliver to. Make one in the game first.", "err")
        return redirect(url_for("shop.item", listing_id=listing_id))
    currency = request.form.get("currency", "")
    online = chardata.is_online(s.world_db, selected["content_id"]) and _server_running()
    played = S.account_playtime(s.world_db, [c["content_id"] for c in characters])
    try:
        result = S.buy(s.webapp_db, s.world_db, user_id=g.user["user_id"], character=selected,
                       listing=listing, currency=currency, online=online, played_minutes=played,
                       site=webdb.branding(s.webapp_db)["site_name"])
    except S.ShopError as exc:
        flash(str(exc), "err")
        return redirect(url_for("shop.item", listing_id=listing_id))
    cfg = S.settings(s.webapp_db)
    price = (f"{result['price']:,} gil" if currency == "gil"
             else f"{result['price']:,} {cfg['shop_coin_name']}")
    letters = "a letter" if result["letters"] == 1 else f"{result['letters']} letters"
    flash(f"Bought {listing.name} for {price}. It's on its way to {selected['name']} by moogle "
          f"mail ({letters}): check any mailbox in game.", "ok")
    webdb.notify(s.webapp_db, g.user["user_id"], "shop", f"{listing.name} is in your mailbox",
                 f"Delivered to {selected['name']} for {price}.", url_for("shop.history"))
    return redirect(url_for("shop.item", listing_id=listing_id))


@shop.route("/history")
@auth.login_required
def history():
    s = _settings()
    return render_template("shop/history.html",
                           purchases=S.purchases(s.webapp_db, g.user["user_id"], 200),
                           **_context())


# ════════════════════════════════════════════════════════════════════════════
#  Editing listings (administrators)
# ════════════════════════════════════════════════════════════════════════════
def _form_values(previous: list | None = None) -> dict:
    f = request.form

    def number(key, default=1):
        try:
            return max(0, int(f.get(key, default)))
        except ValueError:
            return default

    category = (f.get("new_category") or "").strip() or (f.get("category") or "").strip()
    return {
        "category": category or "Other",
        "name": (f.get("name") or "").strip()[:200] or "Untitled",
        "description": (f.get("description") or "").strip()[:4000],
        "contents": S.contents_from_text(f.get("contents") or "", previous),
        "price_gil": number("price_gil"),
        "price_coins": number("price_coins"),
        "sell_gil": 1 if f.get("sell_gil") == "on" else 0,
        "sell_coins": 1 if f.get("sell_coins") == "on" else 0,
        "visible": 1 if f.get("visible") == "on" else 0,
        "retail_price": (f.get("retail_price") or "").strip()[:100],
        "patch": (f.get("patch") or "").strip()[:20],
        "notes": (f.get("notes") or "").strip()[:2000],
    }


@shop.route("/item/<int:listing_id>/edit", methods=["GET", "POST"])
@auth.admin_required
def edit(listing_id: int):
    s = _settings()
    listing = S.get(s.webapp_db, listing_id)
    if not listing:
        abort(404)
    if request.method == "POST":
        S.save(s.webapp_db, listing_id, _form_values(listing.contents))
        flash("Listing saved.", "ok")
        return redirect(url_for("shop.edit", listing_id=listing_id))
    index = _index()
    index.link_listing(listing)
    return render_template("shop/edit.html", listing=listing,
                           contents_text=S.contents_to_text(listing.contents),
                           search=index.search(request.args.get("find", "")),
                           find=request.args.get("find", ""), index_ready=bool(index),
                           **_context())


@shop.route("/new", methods=["GET", "POST"])
@auth.admin_required
def new():
    s = _settings()
    if request.method == "POST":
        listing_id = S.save(s.webapp_db, None, _form_values())
        flash("Listing added.", "ok")
        return redirect(url_for("shop.edit", listing_id=listing_id))
    blank = S.Listing(0, request.args.get("category", ""), "", "", [], 1, 1, True, True, True,
                      "", "", "")
    return render_template("shop/edit.html", listing=blank, contents_text="", search=[], find="",
                           index_ready=bool(_index()), **_context())


@shop.route("/item/<int:listing_id>/delete", methods=["POST"])
@auth.admin_required
def delete(listing_id: int):
    s = _settings()
    listing = S.get(s.webapp_db, listing_id)
    if listing:
        S.delete(s.webapp_db, listing_id)
        flash(f"Deleted {listing.name}.", "ok")
        return redirect(url_for("shop.browse", category=listing.category))
    return redirect(url_for("shop.browse"))


# ════════════════════════════════════════════════════════════════════════════
#  Shop settings (administrators)
# ════════════════════════════════════════════════════════════════════════════
def _tool_status() -> dict:
    """Can kawari-questdump write the item list (its `items` command)?"""
    from .questlab import _active_build, tool_status
    status = dict(tool_status())
    build = _active_build()
    status["has_items_cmd"] = False
    if status.get("ok") and build is not None:
        try:
            status["has_items_cmd"] = b"kawari-questdump items" in (build / TOOL).read_bytes()
        except OSError:
            pass
    return status


@shop.route("/manage", methods=["GET", "POST"])
@auth.admin_required
def manage():
    s = _settings()
    if request.method == "POST":
        action = request.form.get("action", "")
        f = request.form
        if action == "settings":
            try:
                rate = max(0, int(f.get("shop_coins_per_hour", "25")))
            except ValueError:
                rate = 25
            S.save_settings(s.webapp_db, {
                "shop_coin_name": (f.get("shop_coin_name") or "Play Coins").strip()[:40],
                "shop_coins_per_hour": rate,
                "shop_count_past_playtime": "1" if f.get("shop_count_past_playtime") == "on" else "0",
                "shop_show_retail": "1" if f.get("shop_show_retail") == "on" else "0",
                "shop_gil_enabled": "1" if f.get("shop_gil_enabled") == "on" else "0",
                "shop_coins_enabled": "1" if f.get("shop_coins_enabled") == "on" else "0",
                "shop_letter": (f.get("shop_letter") or S.DEFAULTS["shop_letter"]).strip()[:500],
            })
            flash("Shop settings saved.", "ok")
        elif action == "prices":
            def opt(key):
                value = (f.get(key) or "").strip()
                return max(0, int(value)) if value.isdigit() else None
            count = S.set_prices(s.webapp_db, f.get("category") or None, opt("price_gil"),
                                 opt("price_coins"))
            flash(f"Updated {count} listing(s).", "ok")
        elif action == "coins":
            try:
                user_id = int(f.get("user_id", "0"))
                amount = int(f.get("amount", "0"))
            except ValueError:
                flash("Give a whole number of coins.", "err")
                return redirect(url_for("shop.manage"))
            reason = (f.get("reason") or "").strip()[:200] or f"Adjusted by {g.user['username']}"
            balance = S.adjust_coins(s.webapp_db, user_id, amount, reason)
            flash(f"Balance is now {balance:,}.", "ok")
        elif action == "reseed":
            if f.get("confirm") != "RESET":
                flash("Type RESET to replace every listing with the original catalogue.", "err")
            else:
                count = S.reseed(s.webapp_db)
                flash(f"The shop has its {count} original listings again (prices back to 1).", "ok")
        elif action == "index":
            from .admin_base import runner
            from .questlab import _run_step
            status = _tool_status()
            if not status.get("ok"):
                flash(status.get("reason") or "kawari-questdump can't run.", "err")
            elif not status["has_items_cmd"]:
                flash("The active build's kawari-questdump has no items command yet. Build the fork "
                      "again.", "err")
            else:
                try:
                    job = runner().start("Read the item list from the game",
                                         [("Write item-index.json", _run_step(["items"]))])
                except RuntimeError as exc:
                    flash(str(exc), "err")
                    return redirect(url_for("shop.manage"))
                return redirect(url_for("dashboard.job", job_id=job.id))
        return redirect(url_for("shop.manage"))

    index = _index()
    profiles = webdb.all_profiles(s.webapp_db)
    wallets = S.wallets(s.webapp_db)
    return render_template("shop/manage.html", report=S.link_report(s.webapp_db, index),
                           index=index, tool=_tool_status(), profiles=profiles, wallets=wallets,
                           recent=S.purchases(s.webapp_db, None, 30), **_context())
