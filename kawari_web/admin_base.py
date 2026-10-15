"""Shared blueprint plus the helpers every admin module needs."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from flask import Blueprint, current_app

from kawari_core import constants as C
from kawari_core import core as kcore

dashboard = Blueprint("dashboard", __name__)


@dashboard.before_request
def _apply_upstream():
    """Keep the configured source in force for every admin action.

    create_app() sets it once, but a source saved in one request has to apply to
    the next one - including the background job a request starts, which runs in
    this process and reads the same module state.
    """
    from . import upstreamcfg
    upstreamcfg.apply(settings().webapp_db)


def settings():
    return current_app.config["SETTINGS"]


def podman():
    return current_app.config["PODMAN"]


def runner():
    return current_app.config["JOBS"]


def build_context_tar(build_dir: Path) -> bytes:
    """Tar the podman build context in memory.

    Streaming the context means Podman never resolves a path, so this works from
    inside a container where /data is not what the host calls it.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(build_dir.rglob("*")):
            if path.is_dir():
                continue
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(build_dir)))
            # Executables keep their bit; podman needs kawari-* runnable.
            if path.name.startswith("kawari-") or path.suffix == ".sh":
                info.mode = 0o755
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
    return buffer.getvalue()


def container_mounts_and_ports(cfg):
    """Host-side mounts and port bindings for the Kawari container."""
    s = settings()
    shell = s.shell
    mounts = [
        (shell.game_path, "/game", ["ro"]),
        (s.to_host(s.server_config_path), "/kawari/config.yaml", ["ro"]),
        (s.to_host(shell.data_dir), "/kawari/data", []),
    ]
    everywhere = shell.proxy or shell.ip != "127.0.0.1"
    expose_admin = str(cfg.get("expose_admin", "")) == "1"
    ports = []
    for port in C.ALL_PORTS:
        if port == 21057 and not expose_admin:
            host_ip = "127.0.0.1"
        elif everywhere or port in C.GAME_PORTS:
            host_ip = "0.0.0.0"
        else:
            host_ip = "127.0.0.1"
        ports.append((host_ip, port))
    return mounts, ports


def write_build_files(build_dir: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "Containerfile").write_text(kcore.build_containerfile())
    entry = build_dir / "entrypoint.sh"
    entry.write_text(kcore.build_entrypoint())
    entry.chmod(0o755)
