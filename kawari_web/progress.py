"""Dashboard -> Progress: every quest from A Realm Reborn to Dawntrail against
the quest scripts in the active build. The logic is kawari_core.progress."""

from __future__ import annotations

import time

from flask import flash, redirect, render_template, request, url_for

from kawari_core import content_progress as CP
from kawari_core import progress as P

from . import auth
from .admin_base import dashboard, runner
from .questlab import TOOL, _active_build, _root, _run_step, tool_status

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


# ════════════════════════════════════════════════════════════════════════════
#  Misc Content: duties and zones
# ════════════════════════════════════════════════════════════════════════════
def _has_content_cmd() -> bool:
    build = _active_build()
    try:
        return build is not None and b"kawari-questdump content" in (build / TOOL).read_bytes()
    except OSError:
        return False


@dashboard.route("/progress/content")
@auth.admin_required
def progress_content():
    root = _root()
    build = _active_build()
    data = CP.load(root, build)

    expansion = _int_or_none(request.args.get("expansion"))
    status = request.args.get("status") or ""
    if status not in CP.STATUS_LABELS:
        status = ""
    query = (request.args.get("q") or "").strip().lower()

    def keep(item) -> bool:
        if expansion is not None and item.expansion != expansion:
            return False
        if query and query not in item.name.lower() and query != str(item.id):
            return False
        if not status:
            return True
        if isinstance(item, CP.Zone):
            # Zones only have spawns or not: "done" and "spawns" mean with, "missing" without.
            return bool(item.spawns) == (status in ("done", "spawns")) if status != "scripted" else False
        return item.status == status

    groups = []
    for group, name in data.groups():
        duties = [d for d in data.in_group(group) if keep(d)]
        groups.append({
            "key": "other" if group is None else group, "name": name,
            "tally": data.duty_tally(group, expansion), "rows": duties,
            "by_expansion": [(ex, data.duty_tally(group, ex)) for ex in data.expansions()],
        })
    zones = [z for z in data.zones if keep(z)]
    index_file = root / CP.INDEX_FILE
    return render_template(
        "dashboard/progress_content.html", section="progress",
        data=data, groups=groups, zones=zones, zone_tally=data.zone_tally(expansion),
        zone_by_expansion=[(ex, data.zone_tally(ex)) for ex in data.expansions()],
        filters={"expansion": expansion, "status": status, "q": request.args.get("q", "")},
        status_labels=CP.STATUS_LABELS, zone_status_labels=CP.ZONE_STATUS_LABELS,
        build=build.name if build else "",
        index_time=(time.strftime("%Y-%m-%d %H:%M", time.localtime(index_file.stat().st_mtime))
                    if index_file.exists() else ""),
        tool=tool_status(), has_content_cmd=_has_content_cmd(), busy=runner().busy)


@dashboard.route("/progress/content/index", methods=["POST"])
@auth.admin_required
def progress_content_index():
    status = tool_status()
    if not status["ok"]:
        flash(status["reason"], "err")
        return redirect(url_for("dashboard.progress_content"))
    if not _has_content_cmd():
        flash(f"The active build's {TOOL} predates the content command. Build the fork again.", "err")
        return redirect(url_for("dashboard.progress_content"))
    try:
        job = runner().start("Build the content index",
                             [("Write content-index.json", _run_step(["content"]))])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.progress_content"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="progress_content"))
