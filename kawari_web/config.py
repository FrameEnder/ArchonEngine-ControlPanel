"""Runtime configuration.

Everything is driven by environment variables so the same image works whether
it runs on the host or in a container:

    KAWARI_DATA_DIR   the Kawari config directory (default ~/.kawari), holding
                      settings.conf, server/config.yaml, data/*.db, backups/
    KAWARI_WEB_DIR    where the web app keeps its own state (default
                      <data dir>/web): webapp.db and uploads
    KAWARI_PROBE_HOST override for where the Kawari services are reachable FROM
                      THIS PROCESS. In a container the advertised LAN address is
                      not reachable (separate network namespace), so the default
                      tries host.containers.internal first.
    PODMAN_SOCKET     path to the rootless Podman API socket, e.g.
                      /run/user/1000/podman/podman.sock
    KAWARI_HOST_DATA_DIR
                      the SAME directory as seen by the HOST. Podman runs on the
                      host, so bind-mount paths in a container spec must be host
                      paths - inside this container /data is meaningless to it.
    KAWARI_WEB_HOST   bind address (default 0.0.0.0)
    KAWARI_WEB_PORT   port (default 7301; 8080 is too often taken)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from kawari_core import core as kcore

from .winpaths import machine_path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass
class Settings:
    data_dir: Path
    web_dir: Path
    podman_socket: str
    host_data_dir: Path
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _env_path("KAWARI_DATA_DIR", Path.home() / ".kawari")
        return cls(
            data_dir=data_dir,
            web_dir=_env_path("KAWARI_WEB_DIR", data_dir / "web"),
            podman_socket=os.environ.get("PODMAN_SOCKET", ""),
            # As Podman's machine sees it (Windows branch): run.ps1 passes /mnt/c/..., a C:\... given by hand works too.
            host_data_dir=Path(machine_path(_env_path("KAWARI_HOST_DATA_DIR", data_dir))),
            host=os.environ.get("KAWARI_WEB_HOST", "0.0.0.0"),
            port=int(os.environ.get("KAWARI_WEB_PORT", "7301")),
        )

    # ── Kawari paths ────────────────────────────────────────────────────
    @property
    def shell(self) -> kcore.ShellSettings:
        """settings.conf, shared with kawari-setup.sh and the desktop app."""
        return kcore.ShellSettings(self.data_dir).load()

    def db_candidates(self) -> list:
        """Where the databases might be, best first.

        settings.conf records DATA_DIR as an absolute HOST path, written on
        whichever machine set the server up. Move the data to another host - a
        different username, /home vs /var/home - and that path resolves to
        nothing. So the configured value is tried first, then the conventional
        layout inside the mount, rather than trusting it blindly.
        """
        from pathlib import Path as _P
        out = []
        configured = machine_path((self.shell.get("DATA_DIR", "") or "").strip())
        if configured:
            out.append(self.from_host(configured))     # host path -> our view
            out.append(_P(configured))                 # already local?
        out.append(_P(self.data_dir) / "data")         # the standard layout
        seen, unique = set(), []
        for path in out:
            if str(path) not in seen:
                seen.add(str(path))
                unique.append(path)
        return unique

    @property
    def db_dir(self) -> Path:
        """The first candidate that actually holds a database, else the
        conventional location so that creating one still works."""
        for candidate in self.db_candidates():
            if (candidate / "login.db").exists() or (candidate / "world.db").exists():
                return candidate
        return Path(self.data_dir) / "data"

    @property
    def db_dir_configured(self) -> Path:
        """What settings.conf points at, translated into our view."""
        configured = machine_path((self.shell.get("DATA_DIR", "") or "").strip())
        return self.from_host(configured) if configured else Path(self.data_dir) / "data"

    @property
    def db_path_mismatch(self) -> str:
        """Empty when settings.conf is right; otherwise an explanation."""
        configured = machine_path((self.shell.get("DATA_DIR", "") or "").strip())
        if not configured:
            return ""
        translated = self.db_dir_configured
        if translated.exists():
            return ""
        return (f"settings.conf points DATA_DIR at {configured}, which does not exist "
                f"here. Using {self.db_dir} instead. This path is also used to build "
                f"the game container's bind mount, so repair it before creating the "
                f"container.")

    def repair_data_dir(self) -> str:
        """Point settings.conf at this machine's actual data directory."""
        shell = self.shell
        correct = str(Path(self.host_data_dir) / "data")
        shell.set("DATA_DIR", correct)
        shell.save()
        return correct

    @property
    def world_db(self) -> Path:
        return self.db_dir / "world.db"

    @property
    def login_db(self) -> Path:
        return self.db_dir / "login.db"

    @property
    def server_config_path(self) -> Path:
        return self.data_dir / "server" / "config.yaml"

    def server_config(self) -> kcore.ServerConfig:
        return kcore.ServerConfig(self.server_config_path).load()

    @property
    def backups(self) -> kcore.Backups:
        return kcore.Backups(self.data_dir, self.db_dir)

    # ── web app paths ───────────────────────────────────────────────────
    @property
    def webapp_db(self) -> Path:
        return self.web_dir / "webapp.db"

    @property
    def uploads(self) -> Path:
        return self.web_dir / "uploads"

    def from_host(self, path) -> Path:
        """Translate a HOST path into this process's view of it.

        The inverse of to_host(). A path outside the data directory (the game
        files, say) is not visible here at all and is returned unchanged.
        """
        from pathlib import Path as _P
        path = _P(path)
        if _P(self.host_data_dir) == _P(self.data_dir):
            return path                      # not containerised: nothing to do
        try:
            relative = path.relative_to(self.host_data_dir)
        except ValueError:
            return path
        return _P(self.data_dir) / relative

    def probe_candidates(self) -> list:
        """Addresses to try when checking whether Kawari is up, best first.

        The advertised address is what CLIENTS use; from inside this container it
        usually is not reachable at all, because rootless Podman gives the
        container its own network namespace and the host's published ports are
        not on the container's view of that IP. Podman exposes the host as
        host.containers.internal, which is why it comes first.
        """
        import os
        out = []
        explicit = os.environ.get("KAWARI_PROBE_HOST", "").strip()
        if explicit:
            out.append(explicit)
        if self.containerised():
            out.append("host.containers.internal")
        advertised = (self.shell.ip or "").strip()
        if advertised and advertised not in ("0.0.0.0",):
            out.append(advertised)
        out.append("127.0.0.1")
        seen, unique = set(), []
        for host in out:
            if host not in seen:
                seen.add(host)
                unique.append(host)
        return unique

    def reachable_host(self, port: int, timeout: float = 0.4) -> str:
        """The first candidate that actually accepts a connection on `port`."""
        from kawari_core.core import probe_port
        for host in self.probe_candidates():
            if probe_port(host, port, timeout):
                return host
        return ""

    def containerised(self) -> bool:
        from pathlib import Path as _P
        return _P(self.host_data_dir) != _P(self.data_dir)

    def to_host(self, path) -> str:
        """Translate a path inside this container to the equivalent host path.

        Paths that already live outside the data directory (the game files, the
        data dir from settings.conf) are host paths already and pass through.
        """
        from pathlib import Path as _P
        path = _P(path)
        try:
            relative = path.relative_to(self.data_dir)
        except ValueError:
            return str(path)
        return str(_P(self.host_data_dir) / relative)

    @property
    def build_dir(self) -> Path:
        return self.data_dir / "build"

    def ensure_dirs(self) -> None:
        try:
            self.web_dir.mkdir(parents=True, exist_ok=True)
            self.uploads.mkdir(parents=True, exist_ok=True)
            probe = self.web_dir / ".writable"
            probe.write_text("")
            probe.unlink()
        except PermissionError as exc:
            import os
            raise PermissionError(
                f"Cannot write to {self.web_dir} (running as uid {getattr(os, 'getuid', lambda: '?')()}).\n\n"
                f"In rootless Podman, container uid 0 maps to your host user and other "
                f"uids map to subuids, so a mounted directory you own looks root-owned "
                f"inside the container.\n"
                f"Run the container with --userns=keep-id (run.ps1 does this on a rootless "
                f"Podman machine), or check that {self.data_dir} is writable on the host."
            ) from exc
