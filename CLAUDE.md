# ArchonEngine ControlPanel (Kawari Web): the Windows branch

Bob's web front end for his FrameEnder/Kawari (ArchonEngine) server, on Windows. The README is the user guide.
`main` is the Linux ControlPanel; this branch, `Windows`, is the same ControlPanel adjusted to run on Windows, still in
Podman.

## Every change on main is made here too

* **Whatever lands on `main` comes to `Windows`**: merge `main` into `Windows` after each change there (or make the
  same change here when a merge can't carry it), in the same session as the change on `main`. The two branches stay
  feature for feature the same, at the same version (`kawari_web/__init__.py`, the README).
* A change made here that isn't about Windows belongs on `main` first, then merged here.
* When merging, keep this branch's Windows adjustments (below) and port what `main` changed in a Linux-only file to
  its Windows counterpart: a merge that brings back `run.sh`, `install-service.sh`, `diagnose.sh` or
  `kawari-web.container` is resolved by deleting them again and making their change in the `.ps1` script.
* Say in the fork's changelog (`docs/src/fork/changelog.md` in FrameEnder/ArchonEngine) when `Windows` was brought
  up to `main`, with both commits.

## What changes on Windows, and what doesn't

Podman for Windows runs containers in a Linux VM (`podman machine`, on WSL2). **Everything that runs in a container is
the same as on Linux**: the ControlPanel itself (`Containerfile`), the game server, the source build, kawari-questdump.
Leave their code alone. **Everything that runs outside a container is adjusted for Windows**:

| Linux (`main`) | Windows (this branch) |
|---|---|
| `run.sh` (bash, `id -u`, the user's socket in `/run/user`) | `run.ps1`: starts the Podman machine, finds its API socket with `podman info`, builds for uid 1000, keep-id only on a rootless machine |
| `install-service.sh`, `kawari-web.container` (systemd, Quadlet, linger) | `install-service.ps1`: a log-on scheduled task that starts the machine, `podman-restart` enabled in it, Windows Firewall rules (`-Firewall`) |
| `diagnose.sh` (`id`, `/etc/subuid`, `ss`, `firewall-cmd`, SELinux) | `diagnose.ps1`: the machine, the socket, the task, the firewall |
| host paths like `/home/bob/.kawari` | Windows paths (`C:\Users\Bob\.kawari`) become the machine's (`/mnt/c/Users/Bob/.kawari`): `kawari_web/winpaths.py`'s `machine_path` |
| firewalld | Windows Firewall (`New-NetFirewallRule`) |
| text in pages telling people to run Linux commands | the PowerShell equivalent (`templates/dashboard/overview.html`, `setup.html`, error messages in `podman_api.py`, `config.py`) |

Rules for code coming from `main`:

* **Host paths:** every bind mount the app asks Podman for goes through `podman_api.container_spec`, which turns each
  source into the machine's path. Don't build a mount elsewhere; a new host path read from settings.conf or a form
  goes through `machine_path` before it's compared or mounted (`config.py` does for `DATA_DIR`).
* **Shell commands, systemd, `/run/user`, `/etc`, SELinux, `id -u`:** only inside a container. On the Windows side
  they become PowerShell in the `.ps1` scripts.
* **Ports:** the firewall rules come from `kawari_core/constants.py`'s `SERVICES`; when `main` changes a port, change
  `install-service.ps1`'s rule.
* `os.getuid` and `socket.AF_UNIX` don't exist in Python on Windows: guard them (the app runs in the container, but
  should fail with a sentence, not a traceback, if started on Windows itself).
* PowerShell scripts: Windows PowerShell 5.1 and PowerShell 7 both; no bash-isms in text meant for Windows users.

## The rest, as on main

* Every change updates the fork's documentation in the same go: `docs/src/fork/kawari-web.md` (how the feature works;
  its "Windows branch" section for what's Windows-only) and `docs/src/fork/changelog.md` in FrameEnder/ArchonEngine,
  plus the README section for the feature.
* Bump the version in `kawari_web/__init__.py` and the README for each release (the same as `main`'s).
* No game text or game files in this repository: read them from the server's game install at runtime
  (`kawari-questdump` writes `quest-index.json` and `item-index.json`).
* The world server saves a logged-in character over the database, so edits to `world.db` only for logged-out
  characters (mail is the exception).
