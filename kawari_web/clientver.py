"""The game client's own version, for Overview's Client compatibility card.

The versions are in the game install's .ver files. When ArchonEngine ControlPanel runs in its
container it can't see them: only the data folder is mounted, and the game
usually lives elsewhere. So when they can't be read directly, they're read the
way kawari-questdump reads game files: a short-lived container from the
server's image, with the install mounted read-only, prints them. That takes a
second or two, so the answer is kept for ten minutes (a minute if it failed).
"""

from __future__ import annotations

import shlex
import threading
import time
from pathlib import Path

from kawari_core import core as kcore
from kawari_core.versions import read_client_versions

from .podman_api import container_spec

OK_FOR = 600          # seconds a successful read is kept
FAILED_FOR = 60       # and a failed one

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


class _Collect:
    """Stands in for a job: run_once hands every output line to say()."""

    def __init__(self):
        self.lines: list[str] = []

    def say(self, line: str) -> None:
        self.lines.append(line)


def forget() -> None:
    with _lock:
        _cache.clear()


def _empty(source: str, problem: str = "") -> dict:
    return {"game": "", "boot": "", "expac": {}, "source": source, "problem": problem,
            "checked": time.time()}


def _from_container(settings, podman) -> dict:
    game = Path(settings.shell.game_path)
    image = f"{settings.shell.image}:latest"
    if podman.mode != "socket":
        return _empty("container", "ArchonEngine ControlPanel can't see the game folder, and reading it "
                                   "another way needs the Podman API socket.")
    if not podman.image_exists(image):
        return _empty("container", "ArchonEngine ControlPanel can't see the game folder, and there's no "
                                   "server image yet to read it with (install Kawari on Setup).")
    folder = shlex.quote(game.name)
    # Print "path<TAB>contents" for each .ver file that exists.
    script = (f'cd /client || exit 3; for f in {folder}/ffxivgame.ver boot/ffxivboot.ver '
              f'{folder}/sqpack/ex*/ex*.ver; do '
              f'[ -f "$f" ] && printf "%s\\t%s\\n" "$f" "$(head -c 64 "$f")"; done; exit 0')
    spec = container_spec(f"kawari-web-clientver-{int(time.time() * 1000)}", image,
                          [(str(game.parent), "/client", ["ro"])], [],
                          kcore.selinux_enforcing())
    spec.pop("restart_policy", None)
    spec.update({"entrypoint": ["/bin/sh", "-c", script], "command": []})
    out = _Collect()
    result = podman.run_once(spec, out, timeout=60)
    if not result.ok:
        return _empty("container", f"Couldn't read the game folder through Podman ({result.error}).")
    found = _empty("container")
    for line in out.lines:
        path, _, value = line.partition("\t")
        value = value.strip()
        if not value:
            continue
        name = Path(path).name
        if name == "ffxivgame.ver":
            found["game"] = value
        elif name == "ffxivboot.ver":
            found["boot"] = value
        elif name.startswith("ex") and name[2:-4].isdigit():
            found["expac"][int(name[2:-4])] = value
    if not found["game"]:
        found["problem"] = (f"There's no ffxivgame.ver in {game} - check the game path "
                            f"on Setup (it's the folder that contains sqpack).")
    return found


def client_versions(settings, podman, refresh: bool = False) -> dict:
    """{"game", "boot", "expac": {n: version}, "source", "problem", "checked"}."""
    game_path = settings.shell.game_path or ""
    if not game_path:
        return _empty("none", "No game path is set (Setup → Game files).")
    direct = read_client_versions(game_path)
    if direct.get("game"):
        return {**direct, "source": "direct", "problem": "", "checked": time.time()}
    now = time.time()
    with _lock:
        cached = _cache.get(game_path)
        if cached and not refresh:
            stamp, value = cached
            if now - stamp < (OK_FOR if value.get("game") else FAILED_FOR):
                return value
    value = _from_container(settings, podman)
    with _lock:
        _cache[game_path] = (now, value)
    return value
