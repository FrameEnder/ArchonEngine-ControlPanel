"""Quest Lab: Dashboard -> Quests. The quest-scripting loop in one place.

Run kawari-questdump, read what it found, check the fork's script against it,
hand a bundle to whoever writes the next quests, and put a test character at
any point in a quest. The logic lives in kawari_core.questlab; this is the web
half.

kawari-questdump runs in a throwaway container made from the ACTIVE server
image, with the server's own game install and config mounted read-only. So the
tool always matches the running build, the server does not have to be up, and
nothing new needs installing - but the build has to include it, which only a
build of the fork from source does (upstream's CI artifact does not).
"""

from __future__ import annotations

import io
import json
import shutil
import time
import zipfile
from pathlib import Path

from flask import abort, flash, redirect, render_template, request, send_file, url_for

from kawari_core import chardata, clientcheck, dbtools
from kawari_core import core as kcore
from kawari_core import questlab as Q
from kawari_core.versions import BuildStore

from . import auth
from .admin_base import dashboard, podman, runner, settings
from .admin_character import _commit, _guarded, _server_running
from .jobs import SkipStep
from .podman_api import container_spec

TOOL = "kawari-questdump"
COMMANDS = ("chain", "dump", "extract", "index", "scripts", "search")
# Where Script Check's dumps go, under the dumps folder.
CLIENT_SCRIPTS = "client-scripts"
DUMP_FILES = (".json", ".luab", ".txt")


# ════════════════════════════════════════════════════════════════════════════
#  Where things are
# ════════════════════════════════════════════════════════════════════════════
def _root() -> Path:
    return Q.dumps_root(settings().data_dir)


def _active_build() -> Path | None:
    store = BuildStore(settings().data_dir)
    if not store.ledger.active or not store.has_files(store.ledger.active):
        return None
    return store.build_path(store.ledger.active)


def tool_status() -> dict:
    """Can kawari-questdump run, and if not, what to do about it."""
    s = settings()
    build = _active_build()
    out = {"ok": False, "reason": "", "build": "", "has_index_cmd": False, "has_scripts_cmd": False,
           "has_search_cmd": False}
    if build is None:
        out["reason"] = "No build is active yet. Install one from Setup."
        return out
    store = BuildStore(s.data_dir)
    out["build"] = store.ledger.active_entry.short
    if not (build / TOOL).exists():
        out["reason"] = (f"The active build ({out['build']}) has no {TOOL}. It is part of the "
                         f"FrameEnder/Kawari fork and only a build from source includes it - "
                         f"upstream's CI archive does not. Set Updates → Source to "
                         f"“Build from source” and build.")
        return out
    if podman().mode != "socket":
        out["reason"] = "Running the tool needs the Podman API socket."
        return out
    if not s.shell.game_path:
        out["reason"] = "No game folder is configured (Setup → Game folder)."
        return out
    out["ok"] = True
    # The `index` command arrived after the tool itself; its usage text says
    # whether this binary has it, so check rather than fail mid-job.
    try:
        binary = (build / TOOL).read_bytes()
        out["has_index_cmd"] = b"kawari-questdump index" in binary
        # Script Check's dumps (`scripts`) arrived in cf9c620.
        out["has_scripts_cmd"] = b"kawari-questdump scripts" in binary
        # Finding scripts by what they mention (`search`) arrived with Trial by Water's battle script hunt.
        out["has_search_cmd"] = b"kawari-questdump search" in binary
    except OSError:
        pass
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Running the tool
# ════════════════════════════════════════════════════════════════════════════
def _tool_spec(args: list[str], out_sub: str = "") -> dict:
    s = settings()
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    mounts = [
        (s.shell.game_path, "/game", ["ro"]),
        (s.to_host(s.server_config_path), "/kawari/config.yaml", ["ro"]),
        (s.to_host(root), "/out", []),
    ]
    out_dir = f"/out/{out_sub}" if out_sub else "/out"
    spec = container_spec(f"kawari-questdump-{int(time.time() * 1000)}",
                          f"{s.shell.image}:latest", mounts, [],
                          kcore.selinux_enforcing())
    spec.pop("restart_policy", None)
    spec.update({
        "entrypoint": [f"/kawari/{TOOL}"],
        "command": [*args, "--game", "/game", "--out", out_dir],
        # config.yaml (for the game's language) is read from the working dir.
        "work_dir": "/kawari",
    })
    return spec


def _run_step(args: list[str], out_sub: str = "", soft: bool = False):
    def step(job):
        job.say(f"$ {TOOL} {' '.join(args)}")
        result = podman().run_once(_tool_spec(args, out_sub), job, timeout=1800)
        if not result.ok:
            if soft:
                raise SkipStep(f"{TOOL} {args[0]} failed ({result.error})")
            raise RuntimeError(f"{TOOL} failed ({result.error}) - see the log above")
        return "done"
    return step


@dashboard.route("/quests/run", methods=["POST"])
@auth.admin_required
def quests_run():
    status = tool_status()
    if not status["ok"]:
        flash(status["reason"], "err")
        return redirect(url_for("dashboard.quests"))
    command = request.form.get("command", "")
    if command not in COMMANDS:
        abort(400)
    listing = request.form.get("listing") == "on"
    refresh_index = request.form.get("index") == "on"

    steps = []
    if command == "chain":
        start = (request.form.get("start") or "").strip()
        try:
            count = max(1, min(200, int(request.form.get("count") or 10)))
        except ValueError:
            count = 10
        if not start:
            flash("Give the quest the chain starts after.", "err")
            return redirect(url_for("dashboard.quests"))
        args = ["chain", start, str(count)] + (["--listing"] if listing else [])
        title = f"Dump {count} quests after {start}"
        steps.append(("Dump the chain", _run_step(args)))
    elif command == "dump":
        # One quest per line, or comma-separated - names contain spaces.
        raw = request.form.get("quests") or ""
        queries = [q.strip() for line in raw.splitlines() for q in line.split(",") if q.strip()]
        if not queries:
            flash("Give at least one quest: an ID, a script name or a quest name.", "err")
            return redirect(url_for("dashboard.quests"))
        args = ["dump", *queries] + (["--listing"] if listing else [])
        title = f"Dump {len(queries)} quest(s)"
        steps.append(("Dump the quests", _run_step(args)))
    elif command == "extract":
        paths = [p.strip() for p in (request.form.get("paths") or "").splitlines() if p.strip()]
        if not paths:
            flash("Give at least one game path, e.g. game_script/opening/OpeningGridania.luab", "err")
            return redirect(url_for("dashboard.quests"))
        args = ["extract", *paths] + (["--listing"] if listing else [])
        title = f"Extract {len(paths)} file(s)"
        steps.append(("Extract the files", _run_step(args, out_sub="extracted")))
    elif command == "search":
        if not status["has_search_cmd"]:
            flash(f"The active build's {TOOL} predates the search command. Build the fork again.", "err")
            return redirect(url_for("dashboard.quests"))
        texts = [t.strip() for line in (request.form.get("texts") or "").splitlines()
                 for t in line.split(",") if t.strip()]
        if not texts:
            flash("Give at least one thing to look for, e.g. ClsCnj003", "err")
            return redirect(url_for("dashboard.quests"))
        args = ["search", *texts] + (["--listing"] if listing else [])
        title = f"Find scripts mentioning {', '.join(texts)}"
        steps.append(("Search the game's scripts", _run_step(args, out_sub="extracted")))
    elif command == "scripts":
        if not status["has_scripts_cmd"]:
            flash(f"The active build's {TOOL} predates Script Check (its scripts command came in "
                  f"cf9c620). Build the fork again.", "err")
            return redirect(url_for("dashboard.script_check"))
        # Only what the server has a script for; the image keeps the build's resources in /kawari.
        shutil.rmtree(_root() / CLIENT_SCRIPTS, ignore_errors=True)
        for kind, soft in (("custom", False), ("opening", True)):
            args = ["scripts", kind, "--server", "/kawari/resources/scripts"]
            steps.append((f"Dump the client's {kind} scripts", _run_step(args, out_sub=CLIENT_SCRIPTS, soft=soft)))
        try:
            job = runner().start("Script Check: dump the client's scripts", steps)
        except RuntimeError as exc:
            flash(str(exc), "err")
            return redirect(url_for("dashboard.script_check"))
        return redirect(url_for("dashboard.job", job_id=job.id, back="script_check"))
    else:
        title = "Build the quest index"
        refresh_index = True

    if refresh_index:
        if not status["has_index_cmd"]:
            if command == "index":
                flash(f"The active build's {TOOL} predates the index command. Build the fork "
                      f"again (it was added in 4556bf1).", "err")
                return redirect(url_for("dashboard.quests"))
        else:
            steps.append(("Build the quest index", _run_step(["index"], soft=command != "index")))

    try:
        job = runner().start(title, steps)
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.quests"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="quests"))


# ════════════════════════════════════════════════════════════════════════════
#  Overview
# ════════════════════════════════════════════════════════════════════════════
def _analyse_all(dumps, build):
    globals_ = Q.global_names(build)
    rows = []
    for d in dumps:
        script = Q.find_script(build, d.script)
        lua = script.read_text(errors="replace") if script else None
        rows.append((d, Q.analyse(d.quest, d.listing(), lua, globals_)))
    return rows


@dashboard.route("/quests")
@auth.admin_required
def quests():
    root = _root()
    build = _active_build()
    dumps = Q.scan_dumps(root)
    index = Q.load_index(root)
    rows = _analyse_all(dumps, build)
    counts = {key: sum(1 for _d, a in rows if a.status == key) for key in Q.STATUS_LABELS}
    query = request.args.get("q", "")
    dumped_ids = {d.quest_id for d in dumps}
    extracted = sorted((root / "extracted").glob("*")) if (root / "extracted").is_dir() else []
    index_file = root / "quest-index.json"
    return render_template(
        "dashboard/quests.html", section="quests",
        status=tool_status(), rows=rows, counts=counts, labels=Q.STATUS_LABELS,
        index_count=len(index),
        index_time=(time.strftime("%Y-%m-%d %H:%M", time.localtime(index_file.stat().st_mtime))
                    if index_file.exists() else ""),
        query=query, results=Q.search(index, dumps, query) if query else [],
        dumped_ids=dumped_ids, extracted=extracted, busy=runner().busy)


# ════════════════════════════════════════════════════════════════════════════
#  One quest
# ════════════════════════════════════════════════════════════════════════════
@dashboard.route("/quests/<int:quest_id>")
@auth.admin_required
def quest(quest_id: int):
    s = settings()
    root = _root()
    build = _active_build()
    dumps = Q.scan_dumps(root)
    index = Q.load_index(root)
    graph = Q.graph(index, dumps)
    dump = next((d for d in dumps if d.quest_id == quest_id), None)
    entry = graph.get(quest_id)
    if dump is None and entry is None:
        flash(f"Quest {quest_id} is neither dumped nor in the quest index.", "err")
        return redirect(url_for("dashboard.quests"))

    script_path = Q.find_script(build, dump.script if dump else entry.get("script", ""))
    lua = script_path.read_text(errors="replace") if script_path else None
    analysis = (Q.analyse(dump.quest, dump.listing(), lua, Q.global_names(build))
                if dump else None)
    order, unknown = Q.prerequisites(graph, quest_id)
    chars, _err = dbtools.characters(s.world_db)
    return render_template(
        "dashboard/quest.html", section="quests",
        quest_id=quest_id, dump=dump, entry=entry or {}, q=dump.quest if dump else {},
        analysis=analysis, labels=Q.STATUS_LABELS,
        lua=lua, script_rel=Q.script_rel_path(dump.script if dump else entry.get("script", "")),
        listing=dump.listing() if dump else "",
        previous=[graph.get(int(p), {"id": int(p)}) for p in
                  ((dump.quest.get("previous_quests") if dump else None) or
                   (entry or {}).get("previous", []))],
        join_any=int(((dump.quest if dump else entry) or {}).get("previous_join") or 0) == Q.JOIN_ANY,
        following=Q.successors(graph, quest_id)[:12],
        prereq_count=len(order), prereq_unknown=unknown, have_index=bool(index),
        gm=Q.gm_commands(dump.quest if dump else {"quest_id": quest_id}),
        characters=[dict(c) for c in chars], states=Q.STATES,
        status=tool_status(), busy=runner().busy,
        server_running=_server_running(), test=_current_test(), capture_on=_capture_on())


def _current_test():
    from .questtest import current_test
    return current_test()


def _capture_on() -> bool:
    from .diagnostics import capture_status
    try:
        return capture_status()["enabled"]
    except Exception:                                   # noqa: BLE001
        return False


@dashboard.route("/quests/<int:quest_id>/file/<name>")
@auth.admin_required
def quest_file(quest_id: int, name: str):
    dump = Q.find_dump(_root(), quest_id)
    if dump is None:
        abort(404)
    target = dump.folder / name
    # Only the files the tool writes, and only inside the quest's folder.
    if (Path(name).name != name or Path(name).suffix not in DUMP_FILES
            or not target.is_file()):
        abort(404)
    return send_file(target, as_attachment=request.args.get("download") == "1",
                     mimetype="text/plain" if target.suffix != ".luab"
                     else "application/octet-stream", download_name=name)


def prepare_character(quest_id: int, form) -> tuple[bool, str, object]:
    """Put a character at a point in a quest, from a form's content_id, state, sequence, prereqs and level.
    Returns (whether it worked, what to tell the admin, the character.) Shared by "Set up a character" and
    "Start a test"."""
    try:
        content_id = int(form.get("content_id", "0"))
    except ValueError:
        content_id = 0
    if not content_id:
        return False, "Choose a character.", None
    ch, error = _guarded(content_id)
    if error:
        return False, error + " Use the //gm commands on this page instead - they work while logged in.", None
    state = form.get("state", "available")
    try:
        sequence = int(form.get("sequence") or 1)
    except ValueError:
        sequence = 1
    root = _root()
    graph = Q.graph(Q.load_index(root), Q.scan_dumps(root))
    try:
        done = Q.prepare(ch, graph, quest_id, state, sequence,
                         with_prerequisites=form.get("prereqs") == "on",
                         level_up=form.get("level") == "on")
    except Exception as exc:                            # noqa: BLE001
        return False, f"Could not apply: {exc}", ch
    if not ch.dirty:
        return True, f"{ch.name}: nothing to change - {'; '.join(done)}.", ch
    written = _commit(ch, content_id, f"before-quest-{quest_id}")
    return True, (f"{ch.name}: {'; '.join(done)}. Saved {written}; a snapshot was taken first, "
                  f"so the character page can restore it."), ch


@dashboard.route("/quests/<int:quest_id>/prepare", methods=["POST"])
@auth.admin_required
def quest_prepare(quest_id: int):
    """Put a test character at a point in this quest."""
    ok, message, _ch = prepare_character(quest_id, request.form)
    flash(message, "info" if ok and "nothing to change" in message else "ok" if ok else "err")
    return redirect(url_for("dashboard.quest", quest_id=quest_id) + "#prepare")


# ════════════════════════════════════════════════════════════════════════════
#  Bundle and housekeeping
# ════════════════════════════════════════════════════════════════════════════
def _manifest(rows, build) -> str:
    lines = ["# Quest bundle", "",
             f"Made by Kawari Web on {time.strftime('%Y-%m-%d %H:%M')}"
             + (f" from build {BuildStore(settings().data_dir).ledger.active_entry.short}"
                if build else "") + ".", "",
             "* `quest-dumps/<script>/` is kawari-questdump's output: quest.json, the client's",
             "  compiled script (.luab) and its listing (.txt).",
             "* `fork-scripts/` is what the active build runs today, at its real path under",
             "  `resources/scripts/`, plus Global.lua for the shared helpers.",
             "", "The .luab files and listings are Square Enix's data. Do not commit them.", "",
             "| Quest | Script | Lv | Status | Client scenes not played | Unknown names |",
             "|---|---|---|---|---|---|"]
    for d, a in rows:
        lines.append(
            f"| {d.quest_id} {d.name} | {d.script} | {d.level} | {Q.STATUS_LABELS[a.status]} | "
            f"{', '.join(map(str, a.unplayed)) if a.scripted else '-'} | "
            f"{', '.join(a.unknown_names) or '-'} |")
    notes = [(d, a) for d, a in rows if a.notes]
    if notes:
        lines += ["", "## Notes in the scripts", ""]
        for d, a in notes:
            lines.append(f"**{d.script}** ({d.name})")
            lines += [f"* {n}" for n in a.notes]
            lines.append("")
    return "\n".join(lines) + "\n"


@dashboard.route("/quests/bundle", methods=["POST"])
@auth.admin_required
def quests_bundle():
    """A zip of dumps plus the scripts the server runs now - everything someone
    writing the next quests needs, in one attachment."""
    root = _root()
    build = _active_build()
    wanted = set(request.form.getlist("script"))
    dumps = [d for d in Q.scan_dumps(root) if not wanted or d.script in wanted]
    if not dumps:
        flash("Nothing to bundle - dump some quests first.", "err")
        return redirect(url_for("dashboard.quests"))
    rows = _analyse_all(dumps, build)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MANIFEST.md", _manifest(rows, build))
        for d, _a in rows:
            for f in d.files():
                zf.write(f, f"quest-dumps/{d.script}/{f.name}")
            script = Q.find_script(build, d.script)
            if script:
                zf.write(script, f"fork-scripts/{Q.script_rel_path(d.script)}")
        if build:
            for rel in ("Global.lua",):
                p = build / "resources" / "scripts" / rel
                if p.is_file():
                    zf.write(p, f"fork-scripts/{rel}")
        index = root / "quest-index.json"
        if index.is_file() and request.form.get("with_index") == "on":
            zf.write(index, "quest-index.json")
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"kawari-quests-{time.strftime('%Y%m%d-%H%M')}.zip")


@dashboard.route("/quests/delete", methods=["POST"])
@auth.admin_required
def quests_delete():
    root = _root()
    wanted = set(request.form.getlist("script"))
    removed = 0
    for d in Q.scan_dumps(root):
        if wanted and d.script not in wanted:
            continue
        shutil.rmtree(d.folder, ignore_errors=True)
        removed += 1
    if not wanted and request.form.get("everything") == "1":
        for extra in ("extracted",):
            shutil.rmtree(root / extra, ignore_errors=True)
        for name in ("index.txt",):
            (root / name).unlink(missing_ok=True)
    flash(f"Deleted {removed} dumped quest(s).", "ok")
    return redirect(url_for("dashboard.quests"))


@dashboard.route("/quests/extracted.zip")
@auth.admin_required
def quest_extracted_zip():
    """Every extracted (or searched-out) file in one zip, to attach as it is."""
    folder = _root() / "extracted"
    if not folder.is_dir():
        abort(404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.glob("*")):
            if path.is_file():
                archive.write(path, path.name)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, mimetype="application/zip",
                     download_name="extracted-scripts.zip")


@dashboard.route("/quests/extracted/<name>")
@auth.admin_required
def quest_extracted(name: str):
    target = _root() / "extracted" / name
    if Path(name).name != name or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=target.suffix == ".luab",
                     mimetype="text/plain" if target.suffix == ".txt"
                     else "application/octet-stream", download_name=name)


# ════════════════════════════════════════════════════════════════════════════
#  Script Check: the server's NPC and opening scripts against the client's
# ════════════════════════════════════════════════════════════════════════════
def _script_check():
    """(checks, missing, when the dumps were made) for the active build, or None before anything was dumped."""
    build = _active_build()
    dumps = _root() / CLIENT_SCRIPTS
    if build is None or not dumps.is_dir():
        return None
    scripts = build / "resources" / "scripts"
    checks, missing = clientcheck.collect(dumps, scripts)
    indexes = [p for p in dumps.glob("*/index.json")]
    made = max((p.stat().st_mtime for p in indexes), default=0)
    return checks, missing, made


@dashboard.route("/quests/script-check")
@auth.admin_required
def script_check():
    found = _script_check()
    checks, missing, made = found if found else ([], [], 0)
    flagged = [c for c in checks if c.issues]
    others = [c for c in checks if not c.issues and (c.notes or c.unplayed)]
    clean = [c for c in checks if not c.issues and not c.notes and not c.unplayed]
    build = _active_build()
    scripts = build / "resources" / "scripts" if build else None
    def rel(c):
        try:
            return str(c.server_script.relative_to(scripts))
        except (TypeError, ValueError):
            return str(c.server_script)
    return render_template(
        "dashboard/scriptcheck.html", section="quests", status=tool_status(), busy=runner().busy,
        dumped=found is not None and bool(made), made=time.strftime("%Y-%m-%d %H:%M", time.localtime(made)) if made else "",
        flagged=flagged, others=others, clean=clean, missing=missing, total=len(checks), rel=rel)


@dashboard.route("/quests/script-check/report.md")
@auth.admin_required
def script_check_report():
    found = _script_check()
    if not found:
        abort(404)
    checks, missing, _made = found
    build = _active_build()
    text = clientcheck.report(checks, missing, show_all=True, scripts=build / "resources" / "scripts")
    return send_file(io.BytesIO(text.encode()), mimetype="text/markdown", as_attachment=True,
                     download_name=f"script-check-{time.strftime('%Y%m%d-%H%M')}.md")


@dashboard.route("/quests/script-check/bundle")
@auth.admin_required
def script_check_bundle():
    """The report, the client's scripts and the server's, in one attachment for whoever fixes what it found."""
    found = _script_check()
    if not found:
        abort(404)
    checks, missing, _made = found
    build = _active_build()
    scripts = build / "resources" / "scripts"
    dumps = _root() / CLIENT_SCRIPTS
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("client-check.md", clientcheck.report(checks, missing, show_all=True, scripts=scripts))
        zf.writestr("README.md", "# Script Check bundle\n\n"
                    f"Made by Kawari Web on {time.strftime('%Y-%m-%d %H:%M')} from build "
                    f"{BuildStore(settings().data_dir).ledger.active_entry.short}.\n\n"
                    "* `client-check.md`: the report.\n"
                    "* `client-scripts/`: kawari-questdump scripts' output (the client's scripts, their listings, "
                    "their sheet rows, index.json): Square Enix's files, do not commit them.\n"
                    "* `fork-scripts/`: the scripts the server runs today, under `resources/scripts/`.\n")
        for f in sorted(dumps.rglob("*")):
            if f.is_file():
                zf.write(f, f"client-scripts/{f.relative_to(dumps)}")
        for c in checks:
            if c.server_script.is_file():
                zf.write(c.server_script, f"fork-scripts/{c.server_script.relative_to(scripts)}")
        for shared in ("Global.lua", "Init.lua"):
            if (scripts / shared).is_file():
                zf.write(scripts / shared, f"fork-scripts/{shared}")
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"kawari-script-check-{time.strftime('%Y%m%d-%H%M')}.zip")
