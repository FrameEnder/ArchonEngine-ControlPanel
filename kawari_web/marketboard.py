"""Dashboard -> Marketboard: what everything on the in-game marketboard costs,
laid out like the board (tabs, their search categories, each one's items). Set a
tab's rates, change them for one category, fix one item's price, and the
listings' settings. The logic is kawari_core.marketboard; the server reads the
same tables, so changes apply the next time someone looks at the board."""

from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from kawari_core import marketboard as M

from . import auth
from .admin_base import dashboard, runner, settings
from .questlab import _active_build, _run_step, tool_status

PAGE_SIZE = 100
TOOL = "kawari-questdump"


def _board() -> M.Board:
    from kawari_core import questlab as Q
    return M.Board(Q.dumps_root(settings().data_dir))


def _tool() -> dict:
    """Can kawari-questdump write the item list with the marketboard's fields?"""
    status = dict(tool_status())
    build = _active_build()
    status["has_categories"] = False
    if status.get("ok") and build is not None:
        try:
            status["has_categories"] = b"marketboard-categories.json" in (build / TOOL).read_bytes()
        except OSError:
            pass
    return status


def _prices() -> tuple[M.Prices | None, str]:
    try:
        return M.load(settings().world_db), ""
    except M.MarketboardError as exc:
        return None, str(exc)


def _rows(board: M.Board, prices: M.Prices, items: list) -> list[dict]:
    rows = []
    for item in items:
        cat = board.categories[int(item["search_category"])]
        rates = prices.rates(cat.tab, cat.id)
        level = item.get("level_equip") if rates["level_source"] == "equip" else item.get("level_item")
        rows.append({"item": item, "category": cat, "level": level,
                     "rarity": M.RARITIES[M.RARITY_INDEX.get(item.get("rarity", 1), 0)],
                     "sold": prices.sold.get(int(item["id"]), 0),
                     **prices.item_prices(item, cat)})
    return rows


def _back(**kwargs):
    target = request.form.get("back") or ""
    if target.startswith("/dashboard/marketboard"):
        return redirect(target)
    return redirect(url_for("dashboard.marketboard", **kwargs))


@dashboard.route("/marketboard", methods=["GET", "POST"])
@auth.admin_required
def marketboard():
    s = settings()
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "index":
                status = _tool()
                if not status.get("ok"):
                    flash(status.get("reason") or "kawari-questdump can't run.", "err")
                elif not status["has_categories"]:
                    flash("The active build's kawari-questdump predates the marketboard: build the fork "
                          "again.", "err")
                else:
                    job = runner().start("Read the item list from the game",
                                         [("Write item-index.json", _run_step(["items"]))])
                    return redirect(url_for("dashboard.job", job_id=job.id))
            elif action == "settings":
                M.save_settings(s.world_db, request.form)
                flash("Marketboard settings saved.", "ok")
            elif action == "tab":
                tab = int(request.form.get("tab", "0"))
                if tab not in M.TABS:
                    abort(400)
                M.save_rates(s.world_db, f"tab:{tab}", request.form, keep_blank=False)
                flash(f"{M.TABS[tab]} rates saved.", "ok")
            elif action == "tab_reset":
                tab = int(request.form.get("tab", "0"))
                if tab not in M.TABS:
                    abort(400)
                M.save_rates(s.world_db, f"tab:{tab}", {}, keep_blank=False)
                flash(f"{M.TABS[tab]} rates are the built-in ones again.", "ok")
            elif action == "category":
                cat = int(request.form.get("category", "0"))
                M.save_rates(s.world_db, f"category:{cat}", request.form, keep_blank=True)
                flash("Category rates saved (empty fields use the tab's).", "ok")
            elif action == "category_reset":
                cat = int(request.form.get("category", "0"))
                M.clear_rates(s.world_db, f"category:{cat}")
                flash("The category uses its tab's rates again.", "ok")
            elif action == "items":
                changes = {}
                for key in request.form:
                    if not key.startswith("nq_"):
                        continue
                    item_id = int(key[3:])
                    changes[item_id] = (M._number(request.form.get(f"nq_{item_id}"), 1),
                                        M._number(request.form.get(f"hq_{item_id}"), 1))
                own = M.save_item_prices(s.world_db, changes)
                flash(f"Saved: {own} of these {len(changes)} items have their own prices.", "ok")
        except M.MarketboardError as exc:
            flash(str(exc), "err")
        except (ValueError, KeyError):
            flash("That form didn't make sense.", "err")
        return _back()

    board = _board()
    prices, problem = _prices()
    query = request.args.get("q", "").strip()
    results = _rows(board, prices, board.search(query)) if board and prices and query else []
    return render_template("dashboard/marketboard.html", section="marketboard", board=board,
                           prices=prices, problem=problem, tool=_tool(), M=M, query=query,
                           results=results,
                           sales=M.recent_sales(s.world_db, 20) if prices else [])


@dashboard.route("/marketboard/category/<int:category_id>")
@auth.admin_required
def marketboard_category(category_id: int):
    board = _board()
    if not board:
        return redirect(url_for("dashboard.marketboard"))
    cat = board.categories.get(category_id)
    if cat is None:
        abort(404)
    prices, problem = _prices()
    if prices is None:
        flash(problem, "err")
        return redirect(url_for("dashboard.marketboard"))
    page = max(1, request.args.get("page", 1, type=int))
    pages = max(1, (len(cat.items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    items = cat.items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return render_template("dashboard/marketboard_category.html", section="marketboard", board=board,
                           cat=cat, prices=prices, M=M, rows=_rows(board, prices, items), page=page,
                           pages=pages, tab_rates=prices.tabs.get(cat.tab),
                           changes=prices.categories.get(cat.id, {}),
                           rates=prices.rates(cat.tab, cat.id))
