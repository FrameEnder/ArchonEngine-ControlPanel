"""Non-UI plumbing: preferences, the shell script's settings.conf, podman, config.yaml."""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import constants as C

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ════════════════════════════════════════════════════════════════════════════
#  settings.conf -- shared with kawari-setup.sh
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ShellSettings:
    """Reads and writes ~/.kawari/settings.conf in the exact format the bash
    script uses (KEY=value, one per line, %q-quoted).

    Keeping this bidirectional means the TUI and the GUI never disagree about
    which container or data directory is in play.
    """

    config_dir: Path
    values: dict = field(default_factory=dict)

    KEYS = ["CONTAINER_NAME", "IMAGE_NAME", "KAWARI_PORT", "CHOSEN_IP",
            "PROXY_MODE", "GAME_PATH", "DATA_DIR"]

    DEFAULTS = {
        "CONTAINER_NAME": "kawari-server",
        "IMAGE_NAME": "kawari-ffxiv",
        "KAWARI_PORT": "21062",
        "CHOSEN_IP": "127.0.0.1",
        "PROXY_MODE": "false",
        "GAME_PATH": "",
        "DATA_DIR": "",
    }

    @property
    def path(self) -> Path:
        return self.config_dir / "settings.conf"

    def load(self) -> "ShellSettings":
        self.values = dict(self.DEFAULTS)
        self.values["DATA_DIR"] = str(self.config_dir / "data")
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                key = key.strip()
                if key not in self.KEYS:
                    continue
                try:
                    parts = shlex.split(raw)
                    self.values[key] = parts[0] if parts else ""
                except ValueError:
                    self.values[key] = raw.strip().strip("'\"")
        return self

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Kawari settings (shared by kawari-setup.sh and Kawari Manager)"]
        for k in self.KEYS:
            lines.append(f"{k}={shlex.quote(str(self.values.get(k, '')))}")
        self.path.write_text("\n".join(lines) + "\n")

    # typed helpers
    def get(self, key, default=""):        return self.values.get(key, default)
    def set(self, key, value):             self.values[key] = str(value)
    @property
    def container(self):                   return self.get("CONTAINER_NAME", "kawari-server")
    @property
    def image(self):                       return self.get("IMAGE_NAME", "kawari-ffxiv")
    @property
    def web_port(self):
        try:
            return int(self.get("KAWARI_PORT", "21062"))
        except ValueError:
            return 21062
    @property
    def ip(self):                          return self.get("CHOSEN_IP", "127.0.0.1") or "127.0.0.1"
    @property
    def proxy(self):                       return self.get("PROXY_MODE", "false") == "true"
    @property
    def game_path(self):                   return self.get("GAME_PATH", "")
    @property
    def data_dir(self) -> Path:
        d = self.get("DATA_DIR", "")
        return Path(d) if d else self.config_dir / "data"

    @property
    def mode(self) -> str:
        if self.proxy:
            return C.MODE_PROXY
        return C.MODE_LOCAL if self.ip == "127.0.0.1" else C.MODE_NETWORK


# ════════════════════════════════════════════════════════════════════════════
#  Podman
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ContainerState:
    exists: bool = False
    running: bool = False
    status: str = "absent"
    started_at: str = ""
    image_exists: bool = False
    image_created: str = ""
    error: str = ""

    @property
    def uptime(self) -> str:
        if not self.running or not self.started_at:
            return "-"
        try:
            raw = self.started_at.split(".")[0].replace("Z", "")
            started = datetime.fromisoformat(raw)
            secs = int((datetime.utcnow() - started).total_seconds())
        except (ValueError, TypeError):
            return "-"
        if secs < 0:
            return "-"
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            return f"{d}d {h}h {m}m"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"


def podman_run_args(s: ShellSettings, cfg_file: Path, image: str,
                    db_container_path: str = "/kawari/data",
                    selinux_disable: bool | None = None,
                    expose_admin: bool = True) -> list[str]:
    """The `podman run` arguments for the Kawari container.

    Identical to what the desktop app builds: SELinux labelling disabled rather
    than relabelling the whole game directory, and the admin port kept on
    loopback unless explicitly published.
    """
    if selinux_disable is None:
        selinux_disable = selinux_enforcing()

    args = ["run", "-d", "--name", s.container, "--restart", "unless-stopped"]
    if selinux_disable:
        args += ["--security-opt", "label=disable"]
    args += [
        "-v", f"{s.game_path}:/game:ro",
        "-v", f"{cfg_file}:/kawari/config.yaml:ro",
        "-v", f"{s.data_dir}:{db_container_path}",
    ]
    everywhere = s.proxy or s.ip != "127.0.0.1"
    for port in C.ALL_PORTS:
        if port == 21057 and not expose_admin:
            host = "127.0.0.1"
        elif everywhere or port in C.GAME_PORTS:
            host = "0.0.0.0"
        else:
            host = "127.0.0.1"
        args += ["-p", f"{host}:{port}:{port}"]
    args.append(image)
    return args


def selinux_enforcing() -> bool:
    try:
        return subprocess.run(["selinuxenabled"], timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def detect_ips() -> list[tuple[str, str]]:
    """(address, description) for every plausible advertise address."""
    found = [("127.0.0.1", "Loopback - local only")]
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        if "src" in out:
            found.append((out[out.index("src") + 1], "Primary LAN address"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        out = subprocess.run(["tailscale", "ip", "-4"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        for line in out.splitlines():
            if line.strip():
                found.append((line.strip(), "Tailscale"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        for ip in out:
            if ip.count(".") == 3 and not any(ip == f[0] for f in found):
                found.append((ip, "Additional interface"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    seen, uniq = set(), []
    for ip, desc in found:
        if ip not in seen:
            seen.add(ip)
            uniq.append((ip, desc))
    return uniq


def probe_port(host: str, port: int, timeout: float = 0.35) -> bool:
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


# ════════════════════════════════════════════════════════════════════════════
#  config.yaml
# ════════════════════════════════════════════════════════════════════════════
class ServerConfig:
    """Loads, edits and saves Kawari's config.yaml.

    Only keys that differ from the source defaults are written, which keeps the
    file small and lets upstream default changes flow through. The one exception
    is filesystem.game_path, which is always written because it is required.
    """

    AUTO_START = "# >>> auto server_name >>>"
    AUTO_END = "# <<< auto server_name <<<"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        self.raw: str = ""
        self.error: str = ""

    def load(self) -> "ServerConfig":
        self.error = ""
        if not self.path.exists():
            self.data, self.raw = {}, ""
            return self
        self.raw = self.path.read_text()
        if yaml is None:
            self.error = "PyYAML is not installed - only the raw editor is available."
            return self
        try:
            parsed = yaml.safe_load(self.raw)
            self.data = parsed if isinstance(parsed, dict) else {}
        except yaml.YAMLError as exc:
            self.error = f"YAML parse error: {exc}"
            self.data = {}
        return self

    def value(self, section: str, key: str, default):
        return (self.data.get(section) or {}).get(key, default)

    def set_value(self, section: str, key: str, value):
        self.data.setdefault(section, {})[key] = value

    def clear_value(self, section: str, key: str):
        if section in self.data:
            self.data[section].pop(key, None)
            if not self.data[section]:
                self.data.pop(section)

    def pruned(self) -> dict:
        """Drop anything equal to the upstream default (except game_path)."""
        out = {}
        for section, fields in C.CONFIG_SCHEMA.items():
            block = {}
            for key, _label, _kind, default, _tip in fields:
                if section not in self.data or key not in self.data[section]:
                    continue
                val = self.data[section][key]
                if val == default and not (section == "filesystem" and key == "game_path"):
                    continue
                block[key] = val
            if block:
                out[section] = block
        for section, block in self.data.items():   # keep unknown/future keys
            if section not in C.CONFIG_SCHEMA:
                out[section] = block
            else:
                known = {k for k, *_ in C.CONFIG_SCHEMA[section]}
                for k, v in (block or {}).items():
                    if k not in known:
                        out.setdefault(section, {})[k] = v
        return out

    def save(self, advertise_ip: str = "127.0.0.1", write_server_names: bool = False) -> None:
        if yaml is None:
            raise RuntimeError("PyYAML is required to save config.yaml")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self.pruned()

        if write_server_names and advertise_ip != "127.0.0.1":
            web = self.value("web", "port", 21062)
            for section, val in (
                ("frontier", f"http://{advertise_ip}:{self.value('frontier', 'port', 21058)}"),
                ("lobby",    advertise_ip),
                ("login",    f"http://{advertise_ip}:{self.value('login', 'port', 21060)}"),
                ("patch",    f"http://{advertise_ip}:{self.value('patch', 'port', 21061)}"),
                ("web",      f"http://{advertise_ip}:{web}"),
                ("world",    advertise_ip),
            ):
                data.setdefault(section, {})["server_name"] = val

        header = (
            f"# Kawari config.yaml - written by {C.APP_NAME} {C.APP_VERSION}\n"
            f"# Only non-default keys are written;\n"
            f"# anything omitted uses the built-in default from core/src/config.rs.\n"
        )
        body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, indent=4)
        self.path.write_text(header + body)
        self.raw = header + body

    def save_raw(self, text: str) -> None:
        if yaml is not None:
            yaml.safe_load(text)  # raises on malformed input
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)
        self.raw = text
        self.load()


# ════════════════════════════════════════════════════════════════════════════
#  Backups  (tar.gz, same layout and naming as kawari-setup.sh)
# ════════════════════════════════════════════════════════════════════════════
class Backups:
    def __init__(self, config_dir: Path, data_dir: Path):
        self.dir = Path(config_dir) / "backups"
        self.data_dir = Path(data_dir)

    def list(self) -> list[dict]:
        if not self.dir.exists():
            return []
        items = []
        for f in sorted(self.dir.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            items.append({
                "path": f,
                "name": f.name,
                "size": stat.st_size,
                "when": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        return items

    def create(self, label: str = "") -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = f"-{re.sub(r'[^A-Za-z0-9_-]', '', label)}" if label else ""
        out = self.dir / f"kawari-db-{stamp}{suffix}.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            for db in ("world.db", "login.db"):
                src = self.data_dir / db
                if src.exists():
                    tar.add(src, arcname=db)
        return out

    def total_bytes(self) -> int:
        return sum(item["size"] for item in self.list())

    def delete(self, names) -> list[str]:
        """Delete these archives (by name). Names that aren't a backup here are ignored."""
        wanted = set(names)
        removed = []
        for item in self.list():
            if item["name"] in wanted and item["path"].resolve().parent == self.dir.resolve():
                try:
                    item["path"].unlink()
                    removed.append(item["name"])
                except OSError:
                    pass
        return removed

    def prune(self, keep: int, protect=()) -> list[str]:
        """Keep the newest `keep` archives (and any named in `protect`), delete the rest."""
        protect = set(protect)
        extra = [i["name"] for i in self.list() if i["name"] not in protect][keep:]
        return self.delete(extra)

    def restore(self, archive: Path) -> list[str]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        restored = []
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = os.path.basename(member.name)
                if name in ("world.db", "login.db") and member.isfile():
                    src = tar.extractfile(member)
                    if src is None:
                        continue
                    (self.data_dir / name).write_bytes(src.read())
                    restored.append(name)
        return restored


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ════════════════════════════════════════════════════════════════════════════
#  Client auto-configuration (Astra)
# ════════════════════════════════════════════════════════════════════════════
LOOPBACK_MARKERS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")


def autoconfig_url(host: str, web_port: int) -> str:
    """The URL to paste into Astra's 'Autoconfig Server URL'.

    Astra takes this, replaces the path with /.well-known/xiv and fills every
    server field from the response, so only the WEB server address is needed.
    """
    return f"http://{host}:{web_port}"


def check_autoconfig(host: str, web_port: int, expect_lobby_port: int,
                     world_server_name: str, timeout: int = 8,
                     fetch_host: str = "") -> list[tuple[str, str]]:
    """Fetch /.well-known/xiv as Astra would and check every field.

    Returns (level, message) with level in ok|warn|err|info. The point is to
    catch the failure that looks like a client problem: a server_name still
    pointing at loopback, which a remote machine cannot reach.
    """
    import json
    import urllib.request

    # Fetch from wherever this process can reach the server, but check the values
    # against the ADVERTISED host - that is what clients are handed.
    url = autoconfig_url(fetch_host or host, web_port) + "/.well-known/xiv"
    out: list[tuple[str, str]] = []
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:                            # noqa: BLE001
        return [("err", f"Could not fetch {url}: {exc}. Is the server running, and is "
                        f"port {web_port} published and allowed through the firewall?")]
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        return [("err", f"{url} did not return valid JSON: {exc}")]

    def host_of(value: str) -> str:
        v = value.split("//", 1)[-1]
        return v.split("/", 1)[0].split(":", 1)[0]

    # Fields Astra reads, and whether each must carry a scheme.
    fields = [("frontier_server", True, "Frontier Server"),
              ("login_server", True, "Login Server"),
              ("boot_patch_server", True, "Boot Patch Server"),
              ("game_patch_server", True, "Game Patch Server"),
              ("lobby_server", False, "Lobby Server")]
    problems = 0
    for key, needs_scheme, label in fields:
        value = str(cfg.get(key, "") or "")
        if not value:
            out.append(("err", f"{label} ({key}) is empty - Astra will have nothing "
                               f"to connect to."))
            problems += 1
            continue
        if needs_scheme and not value.startswith(("http://", "https://")):
            out.append(("err", f"{label} is '{value}' - it needs a scheme "
                               f"(http://...). Fix {key.rsplit('_', 1)[0]}.server_name."))
            problems += 1
            continue
        if not needs_scheme and "//" in value:
            out.append(("err", f"{label} is '{value}' - it must be a bare host with no "
                               f"scheme, because the client dials it directly."))
            problems += 1
            continue
        h = host_of(value)
        if any(m in h for m in LOOPBACK_MARKERS) or h.endswith(".localhost"):
            out.append(("err", f"{label} points at '{h}', which on another machine "
                               f"means THAT machine. Switch to LAN/Tailscale mode and "
                               f"apply, so it becomes {host}."))
            problems += 1
        elif h != host:
            out.append(("warn", f"{label} points at '{h}' but you are advertising "
                                f"{host}. Fine behind a proxy, wrong otherwise."))
        else:
            out.append(("ok", f"{label}: {value}"))

    port = cfg.get("lobby_port")
    if not isinstance(port, int) or port <= 0:
        out.append(("err", f"lobby_port is {port!r}; Astra reads it as a number and "
                           f"will end up with 0."))
        problems += 1
    elif port != expect_lobby_port:
        out.append(("warn", f"lobby_port is {port}, but this manager expects "
                            f"{expect_lobby_port}."))
    else:
        out.append(("ok", f"Lobby Port: {port}"))

    # NOT part of auto-config: the world address is sent in the EnterWorld packet
    # from world.server_name. Wrong here = login works, entering the world hangs.
    wh = host_of(str(world_server_name or ""))
    if not wh:
        out.append(("err", "world.server_name is empty - the client will be told to "
                           "connect nowhere after character select."))
    elif any(m in wh for m in LOOPBACK_MARKERS) or wh.endswith(".localhost"):
        out.append(("err", f"world.server_name is '{wh}'. This is NOT in the auto-config "
                           f"file - the lobby sends it after character select - so "
                           f"remote clients will reach the lobby and then fail to enter "
                           f"the world. It must be {host}."))
    elif wh != host:
        out.append(("warn", f"world.server_name is '{wh}' but you advertise {host}."))
    else:
        out.append(("ok", f"World server (sent at character select): {wh}"))

    for key, label in (("lodestone_server", "Lodestone Server"),
                       ("savedata_bank_server", "Save Data Bank Server"),
                       ("datacenter_travel_server", "Data Center Travel Server")):
        if str(cfg.get(key, "") or ""):
            out.append(("info", f"{label} is set to '{cfg[key]}'; Kawari normally "
                                f"leaves it empty."))
    return out


def inspect_game_path(path: str) -> dict:
    """Validate a game folder and derive max_ex the way the login server does."""
    result = {"valid": False, "sqpack": False, "exe": False, "repos": [], "max_ex": 0, "note": ""}
    if not path:
        result["note"] = "No game path set."
        return result
    p = Path(path)
    if not p.is_dir():
        result["note"] = "Path does not exist or is not a directory."
        return result
    sqpack = p / "sqpack"
    result["sqpack"] = sqpack.is_dir()
    result["exe"] = (p / "ffxiv_dx11.exe").exists()
    if result["sqpack"]:
        repos = sorted(d.name for d in sqpack.iterdir() if d.is_dir())
        result["repos"] = repos
        result["max_ex"] = max(0, len(repos) - 1)
    if not result["sqpack"]:
        result["note"] = ("No sqpack/ here. Point at the 'game' folder inside your FFXIV "
                          "install, not the install root.")
    elif not result["exe"]:
        result["note"] = "sqpack/ found but ffxiv_dx11.exe is missing - probably the wrong level."
    else:
        result["valid"] = True
        result["note"] = f"{len(result['repos'])} repositories -> max_ex {result['max_ex']}"
    return result


def build_containerfile(db_container_path: str = "/kawari/data") -> str:
    """The same image the shell script builds, kept in one place.

    Upstream deleted its own Containerfile on 2026-08-12, so there is nothing
    to track; this is ours. Ubuntu 24.04 matches the glibc of the CI artifact.
    """
    return f"""FROM docker.io/library/ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates libssl3 curl && \\
        rm -rf /var/lib/apt/lists/*

WORKDIR /kawari
COPY kawari/ /kawari/

RUN chmod +x /kawari/kawari-* || true
RUN mkdir -p {db_container_path}
VOLUME ["{db_container_path}"]
ENV KAWARI_DATA={db_container_path}
ENV LD_LIBRARY_PATH=/kawari
ENV RUST_LOG=info

EXPOSE {' '.join(str(p) for p in C.ALL_PORTS)}

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
"""


def build_entrypoint() -> str:
    """world.db and login.db are opened as bare relative paths with no config
    key (SqliteConnection::establish("world.db")), so the only way to persist
    them is to symlink them out of the working directory onto the volume."""
    return """#!/bin/sh
set -e
DATA="${KAWARI_DATA:-/kawari/data}"
mkdir -p "$DATA"
cd /kawari
for db in world.db login.db; do
    [ -e "$DATA/$db" ] || : > "$DATA/$db"
    ln -sf "$DATA/$db" "/kawari/$db"
done
exec /kawari/kawari-run
"""
