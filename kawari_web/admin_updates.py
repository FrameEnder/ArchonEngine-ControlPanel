"""Updates: the build version control, ported whole.

Upstream only publishes the latest build, so the local history is the only one
that exists: every build stays on disk with its own image tag and schema map, and
switching between them - forward or back - is the same pipeline.
"""

from __future__ import annotations

import json
import re
import time

from flask import flash, redirect, render_template, request, url_for

from kawari_core import dbtools, schemagen, upstream
from kawari_core import versions as V

from . import auth, srcbuild, upstreamcfg, webdb
from .admin_base import (build_context_tar, dashboard, podman, runner, settings,
                         write_build_files)
from .admin_setup import _recreate_container
from .jobs import SkipStep


@dashboard.route("/updates")
@auth.admin_required
def updates():
    s = settings()
    store = V.BuildStore(s.data_dir)
    store.backfill_all(allow_network=False)
    check = webdb.get_setting(s.webapp_db, "last_check_json", "")
    try:
        last = json.loads(check) if check else {}
    except ValueError:
        last = {}
    probe = webdb.get_setting(s.webapp_db, "upstream_probe_json", "")
    try:
        probed = json.loads(probe) if probe else {}
    except ValueError:
        probed = {}
    return render_template(
        "dashboard/updates.html", section="updates",
        store=store, active=store.ledger.active_entry,
        schema_label=dbtools.schema_label(),
        tags=podman().image_tags(s.shell.image) if podman().mode != "none" else set(),
        last=last, busy=runner().busy,
        token_set=bool(webdb.get_setting(s.webapp_db, "github_token", "")),
        keep=int(webdb.get_setting(s.webapp_db, "keep_builds", "5") or 5),
        src=upstream.source(), default_src=upstream.DEFAULT,
        mode_labels=upstream.MODE_LABELS, mode_help=upstream.MODE_HELP,
        build_modes=upstream.BUILD_MODES, probed=probed,
        source_modes=upstream.SOURCE_MODES, forge_labels=upstream.FORGE_LABELS,
        picker_refusal=_ref_refusal(upstream.source(), "x"),
        cache_mib=int(webdb.get_setting(s.webapp_db, "source_cache_bytes", "0") or 0) / 1048576)


@dashboard.route("/updates/check", methods=["POST"])
@auth.admin_required
def check_updates():
    s = settings()
    store = V.BuildStore(s.data_dir)
    active = store.ledger.active
    token = webdb.get_setting(s.webapp_db, "github_token", "")

    def phase_local(job):
        """raw.githubusercontent only - never rate limited, so this must not
        depend on the API phase succeeding."""
        if not active:
            raise SkipStep("no build installed yet")
        entry = store.ledger.active_entry
        changed = store.backfill(entry, allow_network=True)
        if changed:
            store.save()
        if not schemagen.cache_path(s.data_dir, active).exists():
            try:
                generated = schemagen.load_or_generate(s.data_dir, active)
                dbtools.set_schema(generated, f"build {V.short_id(active)}")
                job.say("Generated the schema map for the active build")
            except schemagen.SchemaGenError as exc:
                job.say(f"Could not generate the schema map: {exc}")
        return f"supports {entry.patch_text}"

    def phase_api(job):
        src = upstream.source()
        job.say(f"Source: {src.label}")
        try:
            head = V.fetch_head(token=token)
        except upstream.UpstreamError as exc:
            # Record the attempt so the page explains itself rather than looking
            # as though nothing happened - the local phase did succeed.
            webdb.set_setting(s.webapp_db, "last_check_json", json.dumps({
                "checked": time.strftime("%Y-%m-%d %H:%M"),
                "error": str(exc), "up_to_date": None,
                "head": {}, "support": {}, "behind": 0, "commits": [],
                "schema_changes": []}))
            raise SkipStep(f"{exc} Local build info was still refreshed.") from exc
        support = V.fetch_support(head["sha"])
        unrelated = ""
        # A dev build has no commit of its own; compare from the one it started at.
        base = active.split("-", 1)[0] if V.is_dev_id(active) else active
        if base.startswith("local"):
            base = ""
        if not base or (base == head["sha"] and not V.is_dev_id(active)):
            compare = {"ahead_by": 0, "commits": []}
        elif head["sha"].startswith(base):
            compare = {"ahead_by": 0, "commits": []}
        else:
            try:
                compare = V.fetch_compare(base, head["sha"], token)
            except V.Unrelated:
                # Changing source puts a build in the ledger whose commit this
                # repository may never have seen. That is not an error worth
                # failing the check over - the head commit and the schema diff
                # are still the useful answers.
                compare = {"ahead_by": 0, "commits": []}
                unrelated = (
                    f"The active build ({V.short_id(active)}) is not a commit in "
                    f"{src.slug}, so there is no changelog between them. It was "
                    f"installed from a different source.")
                job.say(unrelated)
        # The commit picker: the newest commits on the branch, so any of them
        # can be installed from the page.
        try:
            recent = V.fetch_recent(src.branch, 30, token)
        except upstream.UpstreamError as exc:
            recent = []
            job.say(f"Could not list recent commits: {exc}")
        try:
            new_schema = schemagen.generate(schemagen.fetch_sources(head["sha"]))
            schema_changes = schemagen.summarise_diff(dbtools.get_schema(), new_schema)
        except schemagen.SchemaGenError as exc:
            schema_changes = [f"could not preview: {exc}"]
        payload = {
            "checked": time.strftime("%Y-%m-%d %H:%M"),
            "head": head, "support": support,
            "behind": compare.get("ahead_by", 0),
            "commits": list(reversed(compare.get("commits", [])))[:60],
            "schema_changes": schema_changes,
            "up_to_date": active == head["sha"],
            "source": src.label,
            "repo_url": src.repo_url,
            "unrelated": unrelated,
            "recent": recent,
        }
        webdb.set_setting(s.webapp_db, "last_check_json", json.dumps(payload))
        job.context["summary"] = (
            "Already on the latest commit." if payload["up_to_date"]
            else f"{payload['behind']} commit(s) behind {head['sha'][:9]}.")
        return job.context["summary"]

    try:
        job = runner().start("Check for updates", [
            ("Refresh local build information", phase_local),
            ("Query upstream", phase_api),
        ])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.updates"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="updates"))


def _ref_refusal(src: upstream.Source, ref: str) -> str:
    """Why this source cannot install a chosen commit, or "" if it can."""
    if not re.match(r"^[A-Za-z0-9._/+-]{1,200}$", ref) or ref.startswith("-"):
        return f"{ref!r} is not a commit, tag or branch name."
    if src.build_mode == "url":
        return ("The build source is a fixed archive URL, which serves one build and "
                "cannot pick a commit. Set Updates → Source to “Build from source: clone "
                "the branch” to install any commit.")
    if src.build_mode == "local":
        return ("A local folder builds whatever is checked out in it. Check the commit "
                "out there, or switch the source to “clone the branch”.")
    return ""


@dashboard.route("/updates/apply", methods=["POST"])
@auth.admin_required
def apply_update():
    s = settings()
    migrate = request.form.get("migrate") == "on"
    restart = request.form.get("restart") == "on"
    snapshot = request.form.get("snapshot") == "on"
    expose_admin = webdb.get_setting(s.webapp_db, "expose_admin", "0")

    src = upstream.source()
    # Where the binary comes from - see Source.build_origin. A fork set to the
    # xiv.zone URL is labelled xiv.zone, because that is whose code it is.
    origin = src.build_origin
    # A commit (full or short), tag or branch to install instead of the newest.
    ref = (request.form.get("ref") or "").strip()
    if ref:
        refusal = _ref_refusal(src, ref)
        if refusal:
            flash(refusal, "err")
            return redirect(url_for("dashboard.updates") + "#commits")

    def download(job):
        s.build_dir.mkdir(parents=True, exist_ok=True)
        target = s.build_dir / f"kawari-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        found = upstream.resolve_build(ref=ref)
        job.say(f"Source: {src.label}")
        if found["note"]:
            job.say(found["note"])
        job.say(f"Downloading {found['name'] or found['url']}")
        target.write_bytes(upstream.download(found["url"], timeout=600))
        job.context["zip"] = target
        return f"{target.stat().st_size // 1048576} MiB"

    def store_it(job):
        store = V.BuildStore(s.data_dir)
        entry = store.import_zip(job.context["zip"], origin)
        job.context["sha"] = entry.sha
        if entry.sha == store.ledger.active:
            job.context["finish"] = (f"Nothing to install - {entry.short} is already "
                                     f"the active build.")
        return f"{entry.short} (supports {entry.patch_text})"

    def same_as_active(job):
        store = V.BuildStore(s.data_dir)
        if job.context.get("sha") == store.ledger.active:
            job.context["finish"] = (f"Nothing to install - that compiled to "
                                     f"{store.ledger.active_entry.short}, the active build.")
            return "same as the active build"
        return "new build"

    if src.compiles:
        fetch_steps = [
            ("Compile from source", srcbuild.build_step(podman(), ref)),
            ("Compare with the active build", same_as_active),
        ]
        title = f"Build and install {ref}" if ref else "Build and install from source"
    else:
        fetch_steps = [
            (f"Download the build for {ref}" if ref else "Download the latest build", download),
            ("Store and version it", store_it),
        ]
        title = f"Install {ref}" if ref else "Update Kawari"
    steps = [
        *fetch_steps,
        ("Stop the server", _stop_step),
        ("Back up the databases", _backup_step),
    ]
    if snapshot:
        steps.append(("Snapshot every character",
                      _snapshot_step(f"pre-install-{ref[:12]}" if ref else "pre-update")))
    steps += [
        ("Generate the schema map", _schema_step),
        ("Build or retag the image", _image_step),
        ("Mark the build active", _activate_step),
        ("Reconcile the database schema", _migrate_step(migrate)),
    ]
    if restart:
        steps.append(("Recreate the container",
                      lambda job: _recreate_container(job, expose_admin)))
    try:
        job = runner().start(title, steps)
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.updates"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="updates"))


@dashboard.route("/updates/switch/<sha>", methods=["POST"])
@auth.admin_required
def switch_build(sha: str):
    s = settings()
    store = V.BuildStore(s.data_dir)
    target = store.ledger.get(sha)
    if not target or not store.has_files(target.sha):
        flash("That build's files are no longer on disk.", "err")
        return redirect(url_for("dashboard.updates"))
    restore = (target.leave_backup if request.form.get("restore_db") == "on" else "")
    if restore and not (s.backups.dir / restore).exists():
        restore = ""
    expose_admin = webdb.get_setting(s.webapp_db, "expose_admin", "0")

    def select(job):
        job.context["sha"] = target.sha
        job.context["restore_backup"] = restore
        return f"{target.short} (supports {target.patch_text})"

    def restore_db(job):
        name = job.context.get("restore_backup")
        if not name:
            raise SkipStep("keeping the current databases")
        restored = s.backups.restore(s.backups.dir / name)
        return f"restored {', '.join(restored)} from {name}"

    try:
        job = runner().start(f"Switch to {target.short}", [
            ("Select the build", select),
            ("Stop the server", _stop_step),
            ("Back up the databases", _backup_step),
            ("Snapshot every character", _snapshot_step(f"pre-switch-{target.short}")),
            ("Generate the schema map", _schema_step),
            ("Build or retag the image", _image_step),
            ("Mark the build active", _activate_step),
            ("Restore that build's databases", restore_db),
            ("Reconcile the database schema", _migrate_step(False)),
            ("Recreate the container",
             lambda job: _recreate_container(job, expose_admin)),
        ])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.updates"))
    return redirect(url_for("dashboard.job", job_id=job.id, back="updates"))


# ════════════════════════════════════════════════════════════════════════════
#  Source: which repository this installation tracks
# ════════════════════════════════════════════════════════════════════════════
def _source_from_form(current: upstream.Source) -> upstream.Source:
    """Read a source out of the Source form, keeping the stored token unless a
    new one was typed. Raises ValueError for an unusable repository URL."""
    from dataclasses import replace

    def text(name: str) -> str:
        return (request.form.get(name) or "").strip()

    parsed = upstream.parse_repo_url(text("repo_url") or current.repo_url)
    forge = text("forge")
    if forge not in upstream.FORGES:
        forge = parsed["forge"]            # "auto": github.com is GitHub, else Gitea/Forgejo
    token = text("github_token")
    if request.form.get("clear_token") == "1":
        token = ""
    elif not token:
        token = current.token
    mode = text("build_mode") or current.build_mode
    if mode not in upstream.BUILD_MODES:
        mode = "url"
    return replace(
        current,
        forge=forge,
        host=parsed["host"],
        owner=parsed["owner"],
        repo=parsed["repo"],
        # An explicit branch wins; else one named in the pasted URL; else master.
        branch=text("branch") or parsed["branch"] or upstream.DEFAULT_BRANCH,
        api_base=text("api_base"),
        raw_base=text("raw_base"),
        build_mode=mode,
        build_url=text("build_url") or upstream.DEFAULT.build_url,
        artifact=text("artifact"),
        token=token,
        local_path=text("local_path"),
        # A checkbox: absent means off. Stored as "0" rather than "", because
        # "" would fall back to the default, which is on.
        fast_build="1" if request.form.get("fast_build") == "1" else "0",
    )


@dashboard.route("/updates/source/codeberg", methods=["POST"])
@auth.admin_required
def move_to_codeberg():
    """Upstream moved from GitHub to Codeberg. Re-point a source that still
    tracks the old GitHub repository, keeping how it builds."""
    from dataclasses import replace
    s = settings()
    current = upstreamcfg.read(s.webapp_db)
    moved = replace(current, forge=upstream.DEFAULT_FORGE, host=upstream.DEFAULT_HOST,
                    owner=upstream.DEFAULT_OWNER, repo=upstream.DEFAULT_REPO,
                    api_base="", raw_base="")
    upstreamcfg.save(s.webapp_db, moved)
    webdb.set_setting(s.webapp_db, "upstream_probe_json", "")
    flash("Now tracking codeberg.org/redstrate/Kawari. A GitHub token is not valid on "
          "Codeberg - it is kept, but a public repository needs none. Test the source "
          "to confirm.", "ok")
    return redirect(url_for("dashboard.updates") + "#source")


@dashboard.route("/updates/source", methods=["POST"])
@auth.admin_required
def save_source():
    """Point this installation at a different repository - a fork, usually.

    Saved even when the probe finds problems: a source you cannot reach yet is
    still the source you meant, and refusing to store it would mean fixing a
    typo from scratch. The problems are flashed instead.
    """
    s = settings()
    try:
        candidate = _source_from_form(upstream.source())
    except ValueError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.updates") + "#source")
    problems = candidate.problems()
    upstreamcfg.save(s.webapp_db, candidate)
    webdb.set_setting(s.webapp_db, "upstream_probe_json", "")
    if problems:
        for problem in problems:
            flash(problem, "err")
        flash("Saved anyway - fix the above, then test it.", "info")
    elif candidate.is_default:
        flash("Tracking upstream (codeberg.org/redstrate/Kawari). Test it to confirm.", "ok")
    else:
        flash(f"Now tracking {candidate.label}. Test it before updating, and "
              f"remember that a build from a fork is versioned by that fork's "
              f"commit.", "ok")
    return redirect(url_for("dashboard.updates") + "#source")


@dashboard.route("/updates/source/test", methods=["POST"])
@auth.admin_required
def test_source():
    """Save what was typed, then try every way the source is used.

    Saving first is the point: testing a source other than the one on screen
    would be a trap. Synchronous rather than a job - four small requests, and
    the answer belongs beside the form that produced it.
    """
    s = settings()
    if request.form.get("repo_url") is not None:        # came from the form
        try:
            upstreamcfg.save(s.webapp_db, _source_from_form(upstream.source()))
        except ValueError as exc:
            flash(str(exc), "err")
            return redirect(url_for("dashboard.updates") + "#source")
    try:
        result = upstream.probe()
    except Exception as exc:                            # noqa: BLE001
        result = {"ok": False, "checks": [{"name": "Source", "ok": False,
                                           "detail": str(exc)}]}
    result["checked"] = time.strftime("%Y-%m-%d %H:%M")
    result["label"] = upstream.source().label
    webdb.set_setting(s.webapp_db, "upstream_probe_json", json.dumps(result))
    flash("The source works." if result.get("ok")
          else "That source has problems - see the Source panel.",
          "ok" if result.get("ok") else "err")
    return redirect(url_for("dashboard.updates") + "#source")


@dashboard.route("/updates/source/reset", methods=["POST"])
@auth.admin_required
def reset_source():
    s = settings()
    upstreamcfg.clear(s.webapp_db, keep_token=request.form.get("drop_token") != "1")
    webdb.set_setting(s.webapp_db, "upstream_probe_json", "")
    flash("Back to tracking upstream (codeberg.org/redstrate/Kawari). Builds "
          "already installed are untouched.", "ok")
    return redirect(url_for("dashboard.updates") + "#source")


@dashboard.route("/updates/settings", methods=["POST"])
@auth.admin_required
def update_settings():
    s = settings()
    webdb.set_setting(s.webapp_db, "keep_builds",
                      str(max(1, min(50, int(request.form.get("keep", "5") or 5)))))
    flash("Update settings saved.", "ok")
    return redirect(url_for("dashboard.updates"))


@dashboard.route("/updates/prune", methods=["POST"])
@auth.admin_required
def prune_builds():
    s = settings()
    store = V.BuildStore(s.data_dir)
    keep = int(webdb.get_setting(s.webapp_db, "keep_builds", "5") or 5)
    removed = store.prune(keep)
    untagged = _remove_images(s, removed)
    flash((f"Pruned {len(removed)} build(s)" + (f" and {untagged} image tag(s)." if untagged
           else ".")) if removed else "Nothing to prune.", "ok")
    return redirect(url_for("dashboard.updates"))


def _remove_images(s, shas) -> int:
    """Remove the image tag of each deleted build. A rebuild loop on a working
    tree makes a new tag every time, so leaving them would fill the disk."""
    p = podman()
    if p.mode != "socket":
        return 0
    removed = 0
    for sha in shas:
        if p.remove_image(f"{s.shell.image}:{V.short_id(sha)}").ok:
            removed += 1
    return removed


@dashboard.route("/updates/source/cache/clear", methods=["POST"])
@auth.admin_required
def clear_build_cache():
    if runner().busy:
        flash("A job is running - clear the cache once it has finished.", "err")
        return redirect(url_for("dashboard.updates") + "#source")
    s = settings()
    size = srcbuild.cache_size(s)
    srcbuild.clear_cache(s)
    webdb.set_setting(s.webapp_db, "source_cache_bytes", "0")
    flash(f"Cleared the source build cache ({size / 1048576:,.0f} MiB). The next build "
          f"downloads crates and compiles everything again.", "ok")
    return redirect(url_for("dashboard.updates") + "#source")


@dashboard.route("/updates/delete/<sha>", methods=["POST"])
@auth.admin_required
def delete_build(sha: str):
    s = settings()
    store = V.BuildStore(s.data_dir)
    entry = store.ledger.get(sha)
    if not entry:
        flash("No such build.", "err")
        return redirect(url_for("dashboard.updates"))
    try:
        store.delete(entry.sha)
    except RuntimeError as exc:                 # refuses to delete the active build
        flash(str(exc), "err")
        return redirect(url_for("dashboard.updates"))
    if _remove_images(s, [entry.sha]):
        flash(f"Deleted {entry.short}: its stored files and its image tag.", "ok")
    else:
        flash(f"Deleted the stored files for {entry.short}. Its image tag could not be "
              f"removed here; if it still exists: podman rmi {s.shell.image}:{entry.short}",
              "ok")
    return redirect(url_for("dashboard.updates"))


@dashboard.route("/updates/pin/<sha>", methods=["POST"])
@auth.admin_required
def pin_build(sha: str):
    s = settings()
    store = V.BuildStore(s.data_dir)
    entry = store.ledger.get(sha)
    if entry:
        entry.pinned = not entry.pinned
        store.save()
    return redirect(url_for("dashboard.updates"))


# ── shared steps ────────────────────────────────────────────────────────────
def _stop_step(job):
    s = settings()
    p = podman()
    if p.mode == "none":
        raise SkipStep("no Podman access")
    state = p.inspect(s.shell.container)
    if not state.get("running"):
        raise SkipStep("not running")
    result = p.stop(s.shell.container)
    if not result.ok:
        raise RuntimeError(result.error)
    return "stopped"


def _backup_step(job):
    s = settings()
    store = V.BuildStore(s.data_dir)
    leaving = store.ledger.active_entry
    label = f"leave-{leaving.short}" if leaving else "pre-install"
    archive = s.backups.create(label)
    if leaving:
        leaving.leave_backup = archive.name
        store.save()
    return archive.name


def _snapshot_step(label: str):
    def step(job):
        from kawari_core import chardata
        s = settings()
        rows, err = dbtools.characters(s.world_db)
        if err or not rows:
            raise SkipStep(err or "no characters")
        store = V.BuildStore(s.data_dir)
        for row in rows:
            chardata.snapshot(s.data_dir, s.world_db, row["content_id"], label,
                              store.ledger.active)
        return f"{len(rows)} character(s)"
    return step


def _schema_step(job):
    s = settings()
    sha = job.context["sha"]
    old = dbtools.get_schema()
    try:
        generated = schemagen.load_or_generate(s.data_dir, sha)
    except schemagen.SchemaGenError as exc:
        raise SkipStep(f"falling back to the bundled map: {exc}") from exc
    job.context["schema"] = generated
    changes = schemagen.summarise_diff(old, generated)
    for line in changes:
        job.say(f"  schema: {line}")
    return f"{len(changes)} change(s)" if changes else "no schema changes"


def _image_step(job):
    s = settings()
    sha = job.context["sha"]
    short = V.short_id(sha)
    image = s.shell.image
    p = podman()
    if p.mode != "socket":
        raise RuntimeError("Building or retagging the image needs the Podman socket.")
    if short in p.image_tags(image):
        result = p.tag_image(f"{image}:{short}", f"{image}:latest")
        if not result.ok:
            raise RuntimeError(result.error)
        return f"retagged the existing {image}:{short}"
    store = V.BuildStore(s.data_dir)
    store.stage_context(sha)
    write_build_files(s.build_dir)
    result = p.build(build_context_tar(s.build_dir), f"{image}:{short}", job)
    if not result.ok:
        raise RuntimeError(result.error)
    p.tag_image(f"{image}:{short}", f"{image}:latest")
    return f"built {image}:{short}"


def _activate_step(job):
    s = settings()
    sha = job.context["sha"]
    store = V.BuildStore(s.data_dir)
    store.set_active(sha)
    schema = job.context.get("schema")
    dbtools.set_schema(schema, f"build {V.short_id(sha)}" if schema else "")
    return V.short_id(sha)


def _migrate_step(auto: bool):
    def step(job):
        s = settings()
        reports = [dbtools.analyse(s.world_db, "world"),
                   dbtools.analyse(s.login_db, "login")]
        drift = [r for r in reports if r.present and not r.ok]
        if not drift:
            return "schemas already match"
        if not auto:
            job.context["needs_migration"] = True
            raise SkipStep(f"{sum(r.problem_count for r in drift)} problem(s) - review "
                           f"on the Database page (auto-migrate was off)")
        for report in drift:
            ok, message = dbtools.apply_sql(report.path, dbtools.migration_sql(report))
            if not ok:
                raise RuntimeError(f"{report.path.name}: {message}")
        return "migrated"
    return step
