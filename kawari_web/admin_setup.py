"""Setup: everything needed to stand a server up on a headless machine.

This is the part that used to require the desktop app - installing the Kawari
build and Oodle, building the image, choosing how clients reach the server, and
creating the container.
"""

from __future__ import annotations

from pathlib import Path

from flask import flash, redirect, render_template, request, url_for

from kawari_core import constants as C
from kawari_core import core as kcore
from kawari_core import upstream
from kawari_core.versions import BuildStore, short_id

from . import auth, srcbuild, webdb
from .admin_base import (build_context_tar, container_mounts_and_ports, dashboard,
                         podman, runner, settings, write_build_files)
from .jobs import SkipStep

OODLE_NAMES = ("liboodle-network-shared.so", "oodle-network-shared.so")


@dashboard.route("/setup")
@auth.admin_required
def setup():
    s = settings()
    shell = s.shell
    store = BuildStore(s.data_dir)
    game = kcore.inspect_game_path(shell.game_path)
    # The game directory is a HOST path and is not mounted into this container,
    # so a "not found" here says nothing about whether it is valid on the host.
    game_visible = bool(shell.game_path) and Path(shell.game_path).exists()
    active = store.ledger.active_entry
    build_dir = s.build_dir / "kawari"
    return render_template(
        "dashboard/setup.html", section="setup",
        shell=shell, game=game, store=store, active=active,
        containerised=s.containerised(), game_visible=game_visible,
        db_mismatch=s.db_path_mismatch, db_dir=s.db_dir,
        host_data_dir=s.host_data_dir,
        installed=(build_dir / "kawari-run").exists() or (
            active is not None and store.has_files(active.sha)),
        oodle=_oodle_present(s, store),
        modes=[(m, C.MODE_LABELS[m], C.MODE_HELP[m])
               for m in (C.MODE_LOCAL, C.MODE_NETWORK, C.MODE_PROXY)],
        addresses=kcore.detect_ips(),
        ports=_port_preview(shell),
        hosts=_hosts_text(),
        podman_ok=podman().mode != "none",
        autoconfig=kcore.autoconfig_url(shell.ip, shell.web_port),
        src=upstream.source(), mode_labels=upstream.MODE_LABELS,
        expose_admin=webdb.get_setting(s.webapp_db, "expose_admin", "0") == "1",
        busy=runner().busy)


@dashboard.route("/setup/game-path", methods=["POST"])
@auth.admin_required
def set_game_path():
    s = settings()
    path = (request.form.get("game_path") or "").strip()
    info = kcore.inspect_game_path(path)
    shell = s.shell
    shell.set("GAME_PATH", path)
    shell.save()
    flash(info["note"], "ok" if info["valid"] else "warn")
    return redirect(url_for("dashboard.setup"))


@dashboard.route("/setup/repair-paths", methods=["POST"])
@auth.admin_required
def repair_paths():
    """Point settings.conf at this machine, after moving the data between hosts."""
    s = settings()
    corrected = s.repair_data_dir()
    flash(f"settings.conf now points DATA_DIR at {corrected}. If the game files also "
          f"moved, set the game path above as well, then recreate the container.", "ok")
    return redirect(url_for("dashboard.setup"))


@dashboard.route("/setup/network", methods=["POST"])
@auth.admin_required
def set_network():
    s = settings()
    mode = request.form.get("mode", C.MODE_LOCAL)
    ip = (request.form.get("address") or "").strip() or "127.0.0.1"
    if mode == C.MODE_LOCAL:
        ip = "127.0.0.1"
    shell = s.shell
    shell.set("CHOSEN_IP", ip)
    shell.set("PROXY_MODE", "true" if mode == C.MODE_PROXY else "false")
    shell.save()
    webdb.set_setting(s.webapp_db, "expose_admin",
                      "1" if request.form.get("expose_admin") == "on" else "0")

    cfg = s.server_config()
    cfg.set_value("filesystem", "game_path", "/game")
    for section in ("frontier", "lobby", "login", "patch", "web", "world"):
        cfg.clear_value(section, "server_name")
    try:
        cfg.save(advertise_ip=ip, write_server_names=(mode != C.MODE_LOCAL))
    except Exception as exc:                            # noqa: BLE001
        flash(f"Could not write config.yaml: {exc}", "err")
        return redirect(url_for("dashboard.setup"))
    flash("Network settings applied. Recreate the container to pick up the new port "
          "bindings.", "ok")
    return redirect(url_for("dashboard.setup"))


@dashboard.route("/setup/verify-autoconfig")
@auth.admin_required
def verify_autoconfig():
    s = settings()
    shell = s.shell
    cfg = s.server_config()
    fetch_host = s.reachable_host(shell.web_port)
    results = kcore.check_autoconfig(
        shell.ip, shell.web_port, int(cfg.value("lobby", "port", 21059)),
        str(cfg.value("world", "server_name", "127.0.0.1")),
        fetch_host=fetch_host)
    if not fetch_host:
        results.insert(0, ("warn",
            "Could not reach the server from this container on any of: "
            + ", ".join(s.probe_candidates())
            + ". That does not mean clients cannot reach it - they connect from "
              "outside. Set KAWARI_PROBE_HOST if the server is somewhere else."))
    return render_template("dashboard/verify.html", section="setup", results=results,
                           url=kcore.autoconfig_url(shell.ip, shell.web_port))


@dashboard.route("/setup/oodle", methods=["POST"])
@auth.admin_required
def upload_oodle():
    s = settings()
    upload = request.files.get("oodle")
    if not upload or not upload.filename:
        flash("Choose the Oodle .so or the release zip.", "err")
        return redirect(url_for("dashboard.setup"))
    data = upload.read()
    store = BuildStore(s.data_dir)
    targets = [s.build_dir / "kawari"]
    targets += [store.build_path(b.sha) for b in store.ledger.builds
                if store.has_files(b.sha)]
    try:
        blob = _extract_oodle(data, upload.filename)
    except ValueError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.setup"))
    count = 0
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        # Both names: Linux links against "oodle-network-shared", so the loader
        # needs the lib-prefixed file; the docs' bare name is Windows guidance.
        for name in OODLE_NAMES:
            (target / name).write_bytes(blob)
        count += 1
    flash(f"Oodle installed into {count} location(s).", "ok")
    return redirect(url_for("dashboard.setup"))


@dashboard.route("/setup/install", methods=["POST"])
@auth.admin_required
def install_build():
    """Download the latest build, store it, build the image, create the container."""
    s = settings()
    source = request.form.get("source", "download")
    upload = request.files.get("archive")
    payload = upload.read() if (upload and upload.filename) else b""
    expose_admin = webdb.get_setting(s.webapp_db, "expose_admin", "0")

    def fetch(job):
        s.build_dir.mkdir(parents=True, exist_ok=True)
        target = s.build_dir / "kawari-download.zip"
        if source == "upload":
            if not payload:
                raise RuntimeError("no archive uploaded")
            target.write_bytes(payload)
            job.say(f"Using uploaded archive ({len(payload):,} bytes)")
        else:
            found = upstream.resolve_build()
            job.say(f"Source: {upstream.source().label}")
            if found["note"]:
                job.say(found["note"])
            job.say(f"Downloading {found['name'] or found['url']}")
            data = upstream.download(found["url"], timeout=600)
            target.write_bytes(data)
            job.say(f"{len(data):,} bytes")
        job.context["zip"] = target
        return f"{target.stat().st_size // 1048576} MiB"

    def store_build(job):
        store = BuildStore(s.data_dir)
        label = "upload" if source == "upload" else upstream.source().build_origin
        entry = store.import_zip(job.context["zip"], label)
        store.set_active(entry.sha)
        job.context["sha"] = entry.sha
        return f"{entry.short} (supports {entry.patch_text})"

    def schema(job):
        from kawari_core import dbtools, schemagen
        try:
            generated = schemagen.load_or_generate(s.data_dir, job.context["sha"])
        except schemagen.SchemaGenError as exc:
            raise SkipStep(f"using the bundled map: {exc}") from exc
        dbtools.set_schema(generated, f"build {short_id(job.context['sha'])}")
        return "generated"

    def image(job):
        store = BuildStore(s.data_dir)
        store.stage_context(job.context["sha"])
        write_build_files(s.build_dir)
        tag = f"{s.shell.image}:{short_id(job.context['sha'])}"
        job.say(f"Building {tag}")
        result = podman().build(build_context_tar(s.build_dir), tag, job)
        if not result.ok:
            raise RuntimeError(result.error)
        podman().tag_image(tag, f"{s.shell.image}:latest")
        return tag

    def container(job):
        return _recreate_container(job, expose_admin)

    def activate(job):
        BuildStore(s.data_dir).set_active(job.context["sha"])
        return short_id(job.context["sha"])

    # "Download" means "get it from the configured source", and for a source
    # that compiles, getting it is compiling it.
    if source != "upload" and upstream.source().compiles:
        acquire = [("Compile from source", srcbuild.build_step(podman())),
                   ("Mark it active", activate)]
    else:
        acquire = [("Fetch the build", fetch),
                   ("Store and version it", store_build)]
    try:
        job = runner().start("Install Kawari", [
            *acquire,
            ("Generate the schema map", schema),
            ("Build the container image", image),
            ("Create and start the container", container),
        ])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.setup"))
    return redirect(url_for("dashboard.job", job_id=job.id))


@dashboard.route("/setup/recreate", methods=["POST"])
@auth.admin_required
def recreate_container():
    s = settings()
    expose_admin = webdb.get_setting(s.webapp_db, "expose_admin", "0")
    try:
        job = runner().start("Recreate the container", [
            ("Recreate and start", lambda j: _recreate_container(j, expose_admin)),
        ])
    except RuntimeError as exc:
        flash(str(exc), "err")
        return redirect(url_for("dashboard.setup"))
    return redirect(url_for("dashboard.job", job_id=job.id))


# ── helpers ─────────────────────────────────────────────────────────────────
def _recreate_container(job, expose_admin: str) -> str:
    from .podman_api import container_spec
    s = settings()
    p = podman()
    if p.mode != "socket":
        raise RuntimeError("Creating the container needs the Podman API socket.")
    shell = s.shell
    if not shell.game_path:
        raise RuntimeError("No game path set - set it on the Setup page first.")

    cfg_path = s.server_config_path
    if not cfg_path.exists():
        conf = s.server_config()
        conf.set_value("filesystem", "game_path", "/game")
        conf.save(advertise_ip=shell.ip,
                  write_server_names=shell.mode != C.MODE_LOCAL)
        job.say("Wrote a starting config.yaml")

    mounts, ports = container_mounts_and_ports({"expose_admin": expose_admin})
    job.say(f"Mounts: {', '.join(f'{src} -> {dst}' for src, dst, _o in mounts)}")
    p.remove(shell.container)
    spec = container_spec(shell.container, f"{shell.image}:latest", mounts, ports,
                          kcore.selinux_enforcing())
    result = p.create_container(spec)
    if not result.ok:
        raise RuntimeError(f"create failed: {result.error}")
    started = p.start(shell.container)
    if not started.ok:
        raise RuntimeError(f"start failed: {started.error}")
    return f"{shell.container} running"


def _oodle_present(s, store) -> bool:
    if (s.build_dir / "kawari" / OODLE_NAMES[0]).exists():
        return True
    active = store.ledger.active_entry
    return bool(active and store.has_oodle(store.build_path(active.sha)))


def _extract_oodle(data: bytes, filename: str) -> bytes:
    if filename.lower().endswith(".so"):
        return data
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            match = next((n for n in names if "oodle-network-shared" in n
                          and n.endswith(".so")), None)
            match = match or next((n for n in names if n.endswith(".so")), None)
            if not match:
                raise ValueError("no .so inside that archive")
            return zf.read(match)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a readable zip: {exc}") from exc


def _port_preview(shell) -> list:
    everywhere = shell.proxy or shell.ip != "127.0.0.1"
    rows = []
    for key, label, port, proto, _e in C.SERVICES:
        bind = "0.0.0.0" if (everywhere or proto == "game") else "127.0.0.1"
        rows.append({"label": label, "port": port, "bind": bind, "protocol": proto,
                     "who": "any host that can route here" if bind == "0.0.0.0"
                            else "this machine only"})
    return rows


def _hosts_text() -> str:
    return "\n".join(["# Kawari"] + [f"127.0.0.1 {name}" for name, _why in C.HOSTS_NAMES])
