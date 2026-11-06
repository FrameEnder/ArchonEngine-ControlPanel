"""How much old data ArchonEngine ControlPanel keeps: database backups, character snapshots and builds.

Every update and build switch takes a backup of both databases and a snapshot of every character, and every edit,
sync or import snapshots the character first. Without a limit they pile up on every update, so each is capped:
after anything makes a new backup or snapshot, the oldest beyond the limit are deleted. A limit of 0 keeps
everything. Backups a kept build would restore when switched back to (its `leave_backup`) are never pruned, and
neither is the newest one.
"""

from __future__ import annotations

from kawari_core import chardata
from kawari_core import versions as V

from . import webdb

DEFAULT_KEEP_BACKUPS = 20
DEFAULT_KEEP_SNAPSHOTS = 30     # per character
MAX_KEEP = 500


def _int(s, key: str, default: int) -> int:
    try:
        return max(0, min(MAX_KEEP, int(webdb.get_setting(s.webapp_db, key, str(default)) or default)))
    except ValueError:
        return default


def keep_backups(s) -> int:
    return _int(s, "keep_backups", DEFAULT_KEEP_BACKUPS)


def keep_snapshots(s) -> int:
    return _int(s, "keep_snapshots", DEFAULT_KEEP_SNAPSHOTS)


def save_limits(s, backups: str, snapshots: str) -> None:
    for key, value, default in (("keep_backups", backups, DEFAULT_KEEP_BACKUPS),
                                ("keep_snapshots", snapshots, DEFAULT_KEEP_SNAPSHOTS)):
        try:
            number = max(0, min(MAX_KEEP, int(value)))
        except (TypeError, ValueError):
            number = default
        webdb.set_setting(s.webapp_db, key, str(number))


def protected_backups(s) -> set[str]:
    """Backups that a build still on disk would restore when switched back to."""
    try:
        store = V.BuildStore(s.data_dir)
        return {b.leave_backup for b in store.ledger.builds
                if b.leave_backup and store.has_files(b.sha)}
    except Exception:                                   # noqa: BLE001 - never block a backup on this
        return set()


def tidy_backups(s, keep: int | None = None, protect=()) -> list[str]:
    keep = keep_backups(s) if keep is None else keep
    if keep <= 0:
        return []
    return s.backups.prune(max(1, keep), protected_backups(s) | set(protect))


def tidy_snapshots(s, content_id: int | None = None, keep: int | None = None) -> int:
    """Prune one character's snapshots, or every character's. Returns how many were deleted."""
    keep = keep_snapshots(s) if keep is None else keep
    if keep <= 0:
        return 0
    ids = [content_id] if content_id is not None else list(chardata.snapshot_usage(s.data_dir))
    return sum(len(chardata.prune_snapshots(s.data_dir, cid, max(1, keep))) for cid in ids)


def usage(s) -> dict:
    snaps = chardata.snapshot_usage(s.data_dir)
    return {
        "backups": len(s.backups.list()),
        "backup_bytes": s.backups.total_bytes(),
        "snapshots": sum(n for n, _ in snaps.values()),
        "snapshot_bytes": sum(b for _, b in snaps.values()),
        "snapshot_characters": len(snaps),
    }
