"""Dashboard -> Progress: every quest from A Realm Reborn to Dawntrail against
the quest scripts in the active build. The logic is kawari_core.progress."""

from __future__ import annotations

import time

from flask import render_template, request

from kawari_core import progress as P

from . import auth
from .admin_base import dashboard, runner
from .questlab import _active_build, _root, tool_status

# How many rows the list shows at once; filters narrow it down.
PAGE_SIZE = 300


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


@dashboard.route("/progress")
@auth.admin_required
def progress():
    root = _root()
    build = _active_build()
    data = P.load(root, build)

    expansion = _int_or_none(request.args.get("expansion"))
    if expansion not in P.EXPANSIONS:
        expansion = None
    kind = request.args.get("type") or ""
    if kind not in P.TYPE_LABELS:
        kind = ""
    status = request.args.get("status") or ""
    if status not in P.STATUS_LABELS:
        status = ""
    section = request.args.get("section") or ""
    query = request.args.get("q", "")
    page = max(1, _int_or_none(request.args.get("page")) or 1)

    rows = data.filter(expansion, kind or None, status or None, section or None, query)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)

    summary = [
        {
            "id": ex,
            "name": data.expansion_names.get(ex, P.FALLBACK_EXPANSION_NAMES[ex]),
            "msq": data.tally(ex, P.MSQ),
            "side": data.tally(ex, P.SIDE),
        }
        for ex in P.EXPANSIONS
    ]
    index_file = root / "quest-index.json"
    return render_template(
        "dashboard/progress.html", section="progress",
        data=data, summary=summary,
        total_msq=data.tally(kind=P.MSQ), total_side=data.tally(kind=P.SIDE),
        rows=rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], row_count=len(rows),
        page=page, pages=pages,
        filters={"expansion": expansion, "type": kind, "status": status,
                 "section": section, "q": query},
        type_labels=P.TYPE_LABELS, status_labels=P.STATUS_LABELS,
        build=build.name if build else "",
        index_time=(time.strftime("%Y-%m-%d %H:%M", time.localtime(index_file.stat().st_mtime))
                    if index_file.exists() else ""),
        tool=tool_status(), busy=runner().busy)
