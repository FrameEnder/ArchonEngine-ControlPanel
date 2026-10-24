"""Admin dashboard. Every route here is behind admin_required."""

from __future__ import annotations

import json
from pathlib import Path

from flask import (
    abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for,
)

from kawari_core import auracite, chardata, constants as C, dbtools
from kawari_core import core as kcore
from kawari_core import gamedata as G

from . import auth, retention, stats, webdb
from .admin_base import dashboard, podman as _podman, runner, settings as _settings
from .app import _save_upload


# ════════════════════════════════════════════════════════════════════════════
#  Pages
# ════════════════════════════════════════════════════════════════════════════
@dashboard.route("/")
@auth.admin_required
def overview():
    settings = _settings()
    podman = _podman()
    available, version = podman.available()
    report, client_ver, build_entry = _client_report(settings)
    return render_template(
        "dashboard/overview.html", section="overview",
        stats=stats.server_stats(settings, podman),
        info=stats.connection_info(settings),
        podman_ok=available, podman_version=version,
        db_mismatch=settings.db_path_mismatch,
        client=report, client_versions=client_ver, build_entry=build_entry,
        shell=settings.shell)


@dashboard.route("/config", methods=["GET", "POST"])
@auth.admin_required
def config():
    settings = _settings()
    cfg = settings.server_config()
    if request.method == "POST":
        for section, fields in C.CONFIG_SCHEMA.items():
            for key, _label, kind, default, _tip in fields:
                raw = request.form.get(f"{section}.{key}")
                if kind == "bool":
                    value = request.form.get(f"{section}.{key}") == "on"
                elif kind in ("int", "port"):
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        value = default
                elif kind == "intlist":
                    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
                    try:
                        value = [int(p) for p in parts]
                    except ValueError:
                        value = list(default)
                    if default and len(value) != len(default):
                        value = (value + [0] * len(default))[:len(default)]
                elif kind == "list":
                    value = [p.strip() for p in (raw or "").split(",") if p.strip()]
                else:
                    value = (raw or "").strip() or default
                if value == default and not (section == "filesystem"
                                             and key == "game_path"):
                    cfg.clear_value(section, key)
                else:
                    cfg.set_value(section, key, value)
        try:
            cfg.save(advertise_ip=settings.shell.ip, write_server_names=False)
            flash("config.yaml saved. Restart the container to apply.", "ok")
        except Exception as exc:                        # noqa: BLE001
            flash(f"Could not save: {exc}", "err")
        return redirect(url_for("dashboard.config"))

    values = {}
    for section, fields in C.CONFIG_SCHEMA.items():
        for key, _label, kind, default, _tip in fields:
            value = cfg.value(section, key, default)
            if kind in ("list", "intlist") and isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            values[f"{section}.{key}"] = value
    return render_template("dashboard/config.html", section="config",
                           schema=C.CONFIG_SCHEMA, help=C.CONFIG_SECTION_HELP,
                           values=values, path=settings.server_config_path,
                           error=cfg.error)


@dashboard.route("/database")
@auth.admin_required
def database():
    settings = _settings()
    world = dbtools.analyse(settings.world_db, "world")
    login = dbtools.analyse(settings.login_db, "login")
    chars, _err = dbtools.characters(settings.world_db)
    accounts, _e2 = dbtools.service_accounts(settings.login_db)
    return render_template(
        "dashboard/database.html", section="database",
        world=world, login=login,
        schema_label=dbtools.schema_label(),
        problems=dbtools.orphan_report(settings.login_db, settings.world_db),
        db_mismatch=settings.db_path_mismatch, db_dir=settings.db_dir,
        characters=[dict(c) for c in chars],
        accounts=[dict(a) for a in accounts],
        profiles=webdb.all_profiles(settings.webapp_db),
        backups=settings.backups.list(),
        backup_bytes=sum(b["size"] for b in settings.backups.list()),
        protected_backups=retention.protected_backups(settings),
        keep_backups=retention.keep_backups(settings),
        keep_snapshots=retention.keep_snapshots(settings),
        storage=retention.usage(settings),
        sections=auracite.SECTIONS)


@dashboard.route("/branding", methods=["GET", "POST"])
@auth.admin_required
def branding():
    settings = _settings()
    if request.method == "POST":
        links = []
        labels = request.form.getlist("link_label")
        urls = request.form.getlist("link_url")
        for label, url in zip(labels, urls):
            label, url = label.strip(), url.strip()
            if label and url:
                links.append({"label": label, "url": url})
        values = {
            "site_name": (request.form.get("site_name") or "Kawari").strip()[:60],
            "tagline": (request.form.get("tagline") or "").strip()[:160],
            "favicon_emoji": (request.form.get("favicon_emoji") or "").strip()[:8],
            "accent": (request.form.get("accent") or "#7c6cff").strip()[:20],
            "theme": "light" if request.form.get("theme") == "light" else "dark",
            "footer": (request.form.get("footer") or "").strip()[:300],
            "links_json": json.dumps(links),
        }
        logo = _save_upload(settings, request.files.get("logo"))
        if logo:
            values["logo"] = logo
        if request.form.get("clear_logo") == "1":
            values["logo"] = ""
        webdb.set_branding(settings.webapp_db, values)
        flash("Branding updated.", "ok")
        return redirect(url_for("dashboard.branding"))
    return render_template("dashboard/branding.html", section="branding",
                           brand=webdb.branding(settings.webapp_db))


@dashboard.route("/logs")
@auth.admin_required
def logs():
    settings = _settings()
    podman = _podman()
    try:
        tail = max(50, min(2000, int(request.args.get("tail", "300"))))
    except ValueError:
        tail = 300
    text = (podman.logs(settings.shell.container, tail) if podman.mode != "none"
            else "No Podman access; logs are unavailable. See the Overview page.")
    hint = ""
    if "panicked at" in text:
        hint = ("A panic appears in the log. Kawari builds with panic = abort, so the "
                "process behind it is gone until the container restarts.")
    return render_template("dashboard/logs.html", section="logs", text=text,
                           tail=tail, hint=hint)


# ════════════════════════════════════════════════════════════════════════════
#  Actions
# ════════════════════════════════════════════════════════════════════════════
@dashboard.route("/server/<action>", methods=["POST"])
@auth.admin_required
def server_action(action: str):
    podman = _podman()
    container = _settings().shell.container
    if podman.mode == "none":
        flash("No Podman access - see the Overview page for how to enable it.", "err")
        return redirect(url_for("dashboard.overview"))
    if action not in ("start", "stop", "restart"):
        flash("Unknown action.", "err")
        return redirect(url_for("dashboard.overview"))

    state = podman.inspect(container)
    if action in ("start", "restart") and not state.get("exists"):
        # There is nothing to start. Create it instead of failing with
        # "no such container" - that is what the user meant by Start.
        settings = _settings()
        if not podman.image_exists(f"{settings.shell.image}:latest"):
            flash(f"No container and no image '{settings.shell.image}'. Install the "
                  f"server first on the Setup page.", "err")
            return redirect(url_for("dashboard.setup"))
        if not settings.shell.game_path:
            flash("No game path set - set it on the Setup page before creating the "
                  "container.", "err")
            return redirect(url_for("dashboard.setup"))
        from .admin_setup import _recreate_container
        from . import webdb as _webdb
        expose_admin = _webdb.get_setting(settings.webapp_db, "expose_admin", "0")
        try:
            job = runner().start("Create and start the server", [
                ("Create the container and start it",
                 lambda j: _recreate_container(j, expose_admin)),
            ])
        except RuntimeError as exc:
            flash(str(exc), "err")
            return redirect(url_for("dashboard.overview"))
        return redirect(url_for("dashboard.job", job_id=job.id, back="overview"))

    fn = {"start": podman.start, "stop": podman.stop, "restart": podman.restart}[action]
    result = fn(container)
    flash(f"{action.title()} requested." if result.ok
          else f"{action.title()} failed: {result.error}", "ok" if result.ok else "err")
    return redirect(url_for("dashboard.overview"))


@dashboard.route("/database/migrate", methods=["POST"])
@auth.admin_required
def migrate():
    settings = _settings()
    podman = _podman()
    if podman.mode != "none" and podman.inspect(settings.shell.container).get("running"):
        flash("Stop the server before migrating - editing the databases underneath a "
              "running server can corrupt them.", "err")
        return redirect(url_for("dashboard.database"))
    try:
        archive = settings.backups.create("premigrate")
    except OSError as exc:
        flash(f"Refusing to migrate without a backup: {exc}", "err")
        return redirect(url_for("dashboard.database"))

    applied, failed = [], []
    for kind, path in (("world", settings.world_db), ("login", settings.login_db)):
        report = dbtools.analyse(path, kind)
        if not report.present or report.ok:
            continue
        sql = dbtools.migration_sql(report)
        if not sql.strip():
            continue
        ok, msg = dbtools.apply_sql(path, sql)
        (applied if ok else failed).append(f"{path.name}" if ok else f"{path.name}: {msg}")
    if failed:
        flash("Migration failed: " + "; ".join(failed), "err")
    else:
        flash(f"Migrated {', '.join(applied) or 'nothing'} (backup {archive.name}).", "ok")
    retention.tidy_backups(settings)
    return redirect(url_for("dashboard.database"))


@dashboard.route("/database/backup", methods=["POST"])
@auth.admin_required
def backup():
    settings = _settings()
    try:
        archive = settings.backups.create(request.form.get("label", "manual"))
    except OSError as exc:
        flash(f"Backup failed: {exc}", "err")
        return redirect(url_for("dashboard.database"))
    pruned = retention.tidy_backups(settings)
    flash(f"Created {archive.name}." + (f" Removed {len(pruned)} old backup(s) beyond the limit."
                                         if pruned else ""), "ok")
    return redirect(url_for("dashboard.database"))


def _backup_file(name: str):
    """Resolve a backup by name, refusing anything outside the backups directory."""
    settings = _settings()
    for entry in settings.backups.list():
        if entry["name"] == name:
            resolved = entry["path"].resolve()
            if resolved.parent == settings.backups.dir.resolve():
                return resolved
    return None


@dashboard.route("/database/backup/<name>/restore", methods=["POST"])
@auth.admin_required
def restore_backup(name: str):
    settings = _settings()
    podman = _podman()
    archive = _backup_file(name)
    if archive is None:
        flash("That backup no longer exists.", "err")
        return redirect(url_for("dashboard.database"))
    if podman.mode != "none" and podman.inspect(settings.shell.container).get("running"):
        flash("Stop the server first - restoring the databases underneath a running "
              "server will corrupt them.", "err")
        return redirect(url_for("dashboard.database"))
    # Never overwrite the current state without keeping it.
    try:
        safety = settings.backups.create("pre-restore")
    except OSError as exc:
        flash(f"Refusing to restore without a safety backup first: {exc}", "err")
        return redirect(url_for("dashboard.database"))
    try:
        restored = settings.backups.restore(archive)
    except (OSError, ValueError) as exc:
        flash(f"Restore failed: {exc}", "err")
        return redirect(url_for("dashboard.database"))
    retention.tidy_backups(settings, protect={name})
    flash(f"Restored {', '.join(restored) or 'nothing'} from {name}. The previous state "
          f"was saved as {safety.name}.", "ok")
    return redirect(url_for("dashboard.database"))


@dashboard.route("/database/backup/<name>/delete", methods=["POST"])
@auth.admin_required
def delete_backup(name: str):
    archive = _backup_file(name)
    if archive is None:
        flash("That backup no longer exists.", "err")
        return redirect(url_for("dashboard.database"))
    try:
        archive.unlink()
    except OSError as exc:
        flash(f"Could not delete: {exc}", "err")
        return redirect(url_for("dashboard.database"))
    flash(f"Deleted {name}.", "ok")
    return redirect(url_for("dashboard.database"))


@dashboard.route("/database/backup/<name>/download")
@auth.admin_required
def download_backup(name: str):
    from flask import send_file
    archive = _backup_file(name)
    if archive is None:
        abort(404)
    return send_file(archive, mimetype="application/gzip", as_attachment=True,
                     download_name=name)


@dashboard.route("/database/backups/delete", methods=["POST"])
@auth.admin_required
def delete_backups():
    """Delete the backups ticked in the list."""
    settings = _settings()
    names = request.form.getlist("names")
    if not names:
        flash("Tick the backups to delete first.", "err")
        return redirect(url_for("dashboard.database"))
    removed = settings.backups.delete(names)
    flash(f"Deleted {len(removed)} backup(s).", "ok")
    return redirect(url_for("dashboard.database"))


@dashboard.route("/database/retention", methods=["POST"])
@auth.admin_required
def save_retention():
    """The limits on backups and snapshots, applied straight away."""
    settings = _settings()
    retention.save_limits(settings, request.form.get("keep_backups"),
                          request.form.get("keep_snapshots"))
    backups = retention.tidy_backups(settings)
    snapshots = retention.tidy_snapshots(settings)
    flash(f"Saved: keeping {retention.keep_backups(settings) or 'every'} backup(s) and "
          f"{retention.keep_snapshots(settings) or 'every'} snapshot(s) per character. "
          f"Removed {len(backups)} backup(s) and {snapshots} snapshot(s).", "ok")
    return redirect(url_for("dashboard.database") + "#retention")


@dashboard.route("/database/backups/prune", methods=["POST"])
@auth.admin_required
def prune_backups():
    """Keep the newest N, delete the rest."""
    settings = _settings()
    try:
        keep = max(0, min(200, int(request.form.get("keep", "10"))))
    except ValueError:
        keep = 10
    entries = settings.backups.list()          # newest first
    removed = 0
    for entry in entries[keep:]:
        try:
            entry["path"].unlink()
            removed += 1
        except OSError:
            pass
    flash(f"Deleted {removed} backup(s), kept the newest {keep}." if removed
          else f"Nothing to delete - {len(entries)} backup(s), keeping {keep}.", "ok")
    return redirect(url_for("dashboard.database"))


@dashboard.route("/announcements")
@auth.admin_required
def announcements():
    settings = _settings()
    return render_template("dashboard/announcements.html", section="announcements",
                           sent=webdb.announcements(settings.webapp_db),
                           accounts=len(webdb.all_profiles(settings.webapp_db)))


@dashboard.route("/announce", methods=["POST"])
@auth.admin_required
def announce():
    """Send a notification to every account, and keep a record of it."""
    settings = _settings()
    back = (url_for("dashboard.overview") if request.form.get("back") == "overview"
            else url_for("dashboard.announcements"))
    title = (request.form.get("title") or "").strip()[:120]
    body = (request.form.get("body") or "").strip()[:500]
    if not title:
        flash("An announcement needs a title.", "err")
        return redirect(back)
    accounts = len(webdb.all_profiles(settings.webapp_db))
    sent = webdb.notify_all(settings.webapp_db, "announcement", title, body,
                            url_for("social.notifications"))
    webdb.record_announcement(settings.webapp_db, title, body, g.user["user_id"], sent, accounts)
    flash(f"Announcement sent to {sent} account(s). Anyone who turned announcements "
          f"off did not receive it.", "ok")
    return redirect(back)


@dashboard.route("/accounts/create", methods=["POST"])
@auth.admin_required
def create_account():
    settings = _settings()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    make_admin = request.form.get("is_admin") == "on"
    info = kcore.inspect_game_path(settings.shell.game_path)
    max_ex = info["max_ex"] if info["repos"] else 5
    try:
        user_id, sa_id = dbtools.create_account(settings.login_db, username, password,
                                                max_ex)
    except Exception as exc:                            # noqa: BLE001
        flash(f"Could not create the account: {exc}", "err")
        return redirect(url_for("dashboard.database"))
    webdb.ensure_profile(settings.webapp_db, user_id, username, is_admin=make_admin)
    flash(f"Created '{username}' (user {user_id}, service account {sa_id}). It works in "
          f"the game client and here.", "ok")
    return redirect(url_for("dashboard.database"))


@dashboard.route("/accounts/<int:user_id>/admin", methods=["POST"])
@auth.admin_required
def set_admin(user_id: int):
    settings = _settings()
    make_admin = request.form.get("is_admin") == "1"
    if not make_admin and user_id == g.user["user_id"]:
        flash("You cannot remove your own administrator access.", "err")
        return redirect(url_for("dashboard.database"))
    if not make_admin and webdb.admin_count(settings.webapp_db) <= 1:
        flash("There must always be at least one administrator.", "err")
        return redirect(url_for("dashboard.database"))
    webdb.update_profile(settings.webapp_db, user_id, is_admin=1 if make_admin else 0)
    flash("Administrator access updated.", "ok")
    return redirect(url_for("dashboard.database"))


@dashboard.route("/characters/<int:content_id>/export")
@auth.admin_required
def export_character(content_id: int):
    from flask import send_file
    import io
    import zipfile
    settings = _settings()
    try:
        from . import __version__
        ch = chardata.load(settings.world_db, content_id)
        data = auracite.export_character(
            ch, kept_extended=auracite.stored_extended(settings.world_db, content_id),
            exporter=f"Kawari Web {__version__}")
    except Exception as exc:                            # noqa: BLE001
        flash(f"Export failed: {exc}", "err")
        return redirect(url_for("dashboard.database"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("character.json", json.dumps(data, indent=1))
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=auracite.suggest_filename(ch.name))


@dashboard.route("/characters/import", methods=["POST"])
@auth.admin_required
def import_character():
    settings = _settings()
    upload = request.files.get("backup")
    if not upload or not upload.filename:
        flash("Choose an Auracite backup first.", "err")
        return redirect(url_for("dashboard.database"))
    tmp = settings.web_dir / "tmp-import.zip"
    upload.save(tmp)
    try:
        dump = auracite.Dump.load(tmp)
    except auracite.AuraciteError as exc:
        flash(str(exc), "err")
        tmp.unlink(missing_ok=True)
        return redirect(url_for("dashboard.database"))

    chosen = set(request.form.getlist("sections"))
    target_raw = request.form.get("target", "")
    try:
        target = int(target_raw)
    except ValueError:
        target = chardata.find_by_name(settings.world_db, dump.name) or 0

    try:
        if not target:
            flash("No character matches that backup. Create one in the desktop app, or "
                  "pick an existing character to update.", "err")
            return redirect(url_for("dashboard.database"))
        if chardata.is_online(settings.world_db, target) and _server_running(settings):
            flash("That character is logged in: the world server would save over the import "
                  "when they log out. Log them out first.", "err")
            return redirect(url_for("dashboard.database"))
        chardata.snapshot(settings.data_dir, settings.world_db, target,
                          "before-auracite-web")
        retention.tidy_snapshots(settings, target)
        ch = chardata.load(settings.world_db, target)
        changes, _notes = auracite.build_changes(ch, dump, chosen)
        auracite.apply_changes(ch, changes)
        chardata.save(settings.world_db, ch)
        kept = auracite.store_extended(settings.world_db, target, dump)
        n = sum(len(c) for c in changes.values())
        flash(f"Imported {n} column(s) into '{ch.name}'. A snapshot was taken first."
              + (" The backup's extended data was kept for export." if kept else ""),
              "ok")
    except Exception as exc:                            # noqa: BLE001
        flash(f"Import failed: {exc}", "err")
    finally:
        tmp.unlink(missing_ok=True)
    return redirect(url_for("dashboard.database"))


def _server_running(settings) -> bool:
    p = _podman()
    return p.mode != "none" and p.inspect(settings.shell.container).get("running", False)


@dashboard.route("/characters/inspect", methods=["POST"])
@auth.admin_required
def inspect_backup():
    """What an Auracite backup holds, without importing anything."""
    settings = _settings()
    upload = request.files.get("backup")
    if not upload or not upload.filename:
        flash("Choose an Auracite backup first.", "err")
        return redirect(url_for("dashboard.database"))
    tmp = settings.web_dir / "tmp-inspect.zip"
    upload.save(tmp)
    try:
        dump = auracite.Dump.load(tmp)
    except auracite.AuraciteError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.database"))
    finally:
        tmp.unlink(missing_ok=True)
    return render_template("dashboard/auracite_inspect.html", section="database",
                           filename=upload.filename, summary=dump.summary(),
                           extended=dump.extended_report(),
                           has_extended=dump.extended is not None)


def _client_report(settings, refresh: bool = False) -> tuple[list, dict, object]:
    """Client-vs-server version check, as on the desktop Dashboard.

    Returns the messages, the client's versions (with where they were read from)
    and the active build's entry (its supported versions, as Updates shows them)."""
    from kawari_core.versions import BuildStore, compare_client

    from .clientver import client_versions
    try:
        store = BuildStore(settings.data_dir)
        entry = store.ledger.active_entry
    except OSError:
        entry = None
    cfg = settings.server_config()
    enforce = bool(cfg.value("tweaks", "enforce_validity_checks", True))
    client = client_versions(settings, _podman(), refresh)
    report = compare_client(entry, client, enforce)
    if client.get("problem") and not client.get("game"):
        # compare_client only knows "couldn't read it"; say why.
        report = [r for r in report if "ffxivgame.ver" not in r[1]] + [("warn", client["problem"])]
    return report, client, entry


@dashboard.route("/client-check", methods=["POST"])
@auth.admin_required
def client_check():
    """Read the client's version again now, rather than the cached one."""
    from .clientver import forget
    forget()
    return redirect(url_for("dashboard.overview") + "#client")


# ════════════════════════════════════════════════════════════════════════════
#  Background jobs
# ════════════════════════════════════════════════════════════════════════════
@dashboard.route("/job/<job_id>")
@auth.admin_required
def job(job_id: str):
    found = runner().get(job_id)
    if not found:
        flash("That job is no longer available.", "info")
        return redirect(url_for("dashboard.overview"))
    back = request.args.get("back", "setup")
    # Where Back goes: a dashboard page by name, or the shop's management page.
    endpoints = {"shop": "shop.manage"}
    endpoint = endpoints.get(back, f"dashboard.{back}")
    if endpoint not in current_app.view_functions:
        back, endpoint = "setup", "dashboard.setup"
    return render_template("dashboard/job.html", section=back, job=found.as_dict(),
                           back=back, back_url=url_for(endpoint))


@dashboard.route("/job/<job_id>.json")
@auth.admin_required
def job_json(job_id: str):
    found = runner().get(job_id)
    if not found:
        abort(404)
    return jsonify(found.as_dict())
