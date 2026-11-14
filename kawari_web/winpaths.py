"""Windows host paths as Podman's machine sees them (the Windows branch).

On Windows, Podman runs containers in a Linux VM (`podman machine`, on WSL2). ArchonEngine ControlPanel runs in a
container there too (`run.ps1`), and the containers it makes for the server, the source build and kawari-questdump
are made through the Podman API socket, whose bind mounts are paths in that VM, not Windows paths. The `podman` CLI
translates `C:\\...` into the VM's `/mnt/c/...` by itself; the API doesn't. So every host path the app turns into a
mount goes through `machine_path`: the game folder and the data folder from settings.conf (typed in Setup or written
on Windows as `C:\\...`), and KAWARI_HOST_DATA_DIR if it was given in Windows form.

A path that isn't a Windows drive path (already `/mnt/c/...`, or a path inside the VM) is returned unchanged, so a
settings.conf written either way works.
"""

from __future__ import annotations

import re

# C:\Users\Bob\.kawari, C:/Users/Bob/.kawari, c:\...  (not a UNC path: \\server\share has no /mnt/ form)
DRIVE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")


def machine_path(path) -> str:
    """`C:\\Program Files (x86)\\SquareEnix` -> `/mnt/c/Program Files (x86)/SquareEnix`; anything else as it is."""
    text = str(path).strip().strip('"')
    match = DRIVE.match(text)
    if not match:
        return text
    drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/").strip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def is_windows_path(path) -> bool:
    return bool(DRIVE.match(str(path).strip().strip('"')))
