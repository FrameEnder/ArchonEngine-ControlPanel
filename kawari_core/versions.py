"""Build version control.

Upstream publishes only the LATEST build (xiv.zone/distrib/kawari). Older builds
exist as GitHub Actions artifacts, which need authentication to download. So the
only rollback history that can exist is one kept locally, and that is what this
module is:

    ~/.kawari/builds/<sha>/        every build ever installed, extracted, with Oodle
    ~/.kawari/versions.json        ledger: which builds, when, what patch, what backup
    ~/.kawari/schemas/<sha>.json   the database schema that build expects
    podman image  <image>:<short>  one image tag per build; :latest is the active one

Rolling back to a build whose image tag still exists is a retag plus container
recreate - no rebuild, no download.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import constants as C
from . import schemagen
from . import upstream
from .core import build_containerfile, build_entrypoint

# The repository every call below reads from is configurable - see
# kawari_core/upstream.py. Nothing here may hardcode it: a fork that changes a
# Diesel migration must be read from the fork, or the generated migration is wrong.
OODLE_NAMES = ("liboodle-network-shared.so", "oodle-network-shared.so")


# ════════════════════════════════════════════════════════════════════════════
#  Ledger
# ════════════════════════════════════════════════════════════════════════════
def short_id(build_id: str) -> str:
    """The display and image-tag form of a build id.

    A downloaded build is a commit, shown as its first nine characters. A build
    compiled from a working tree with uncommitted changes has no commit of its
    own, so it gets ``<head>-dev<timestamp>`` (or ``local-<timestamp>`` outside
    git) - and truncating THAT to nine characters would give every dev build of
    the same commit the same image tag, so each rebuild would silently retag
    the previous one. Such ids are short already and are used whole.
    """
    return build_id if "-" in build_id else build_id[:9]


def is_dev_id(build_id: str) -> bool:
    return "-" in build_id


@dataclass
class BuildEntry:
    sha: str
    installed_at: str = ""
    patch: str = ""             # human label, e.g. "7.56h1" (README at that commit)
    game_version: str = ""      # SUPPORTED_GAME_VERSION, e.g. "2026.09.15.0000.0000"
    boot_version: str = ""      # SUPPORTED_BOOT_VERSION
    expac_versions: list = field(default_factory=list)   # SUPPORTED_EXPAC_VERSIONS, ex1..
    source: str = ""            # "xiv.zone", "upload", "built FrameEnder/Kawari@wip", ...
    commit_date: str = ""
    commit_message: str = ""
    leave_backup: str = ""      # DB archive taken when switching AWAY from this build
    pinned: bool = False
    notes: str = ""

    @property
    def short(self) -> str:
        return short_id(self.sha)

    @property
    def is_dev(self) -> bool:
        """Compiled from uncommitted changes - not reproducible from any commit."""
        return is_dev_id(self.sha)

    @property
    def patch_text(self) -> str:
        """'7.56h1 (2026.09.15.0000.0000)', degrading gracefully."""
        if self.patch and self.game_version:
            return f"{self.patch} ({self.game_version})"
        return self.patch or self.game_version or "unknown"


@dataclass
class Ledger:
    active: str = ""
    builds: list = field(default_factory=list)   # list[BuildEntry], newest first
    skipped: list = field(default_factory=list)  # shas the user chose to skip

    def get(self, sha: str) -> BuildEntry | None:
        for b in self.builds:
            if b.sha == sha:
                return b
        # Prefix match for abbreviated commits only. A dev build's id starts
        # with its parent commit, so without the exact pass above (and the
        # dev exclusion here) "c1d2ac5b6" could resolve to a dev build of it.
        if len(sha) >= 7:
            for b in self.builds:
                if not b.is_dev and b.sha.startswith(sha):
                    return b
        return None

    @property
    def active_entry(self) -> BuildEntry | None:
        return self.get(self.active) if self.active else None


def _find_build_root(top: Path, depth: int = 0) -> Path | None:
    """The folder holding kawari-run inside an extracted archive.

    Upstream's archive has the binaries at the root. A GitHub Actions artifact is
    whatever the workflow uploaded, wrapped by GitHub in a zip of its own - so a
    fork's artifact is commonly one folder deep, or a zip inside a zip. Both are
    normal, so unwrap rather than refuse.
    """
    if (top / "kawari-run").exists():
        return top
    if depth >= 2:
        return None
    for sub in sorted(p for p in top.iterdir() if p.is_dir()):
        found = _find_build_root(sub, depth + 1)
        if found is not None:
            return found
    for inner in sorted(top.glob("*.zip")):
        target = top / f"_unwrapped_{inner.stem}"
        try:
            with zipfile.ZipFile(inner) as zf:
                zf.extractall(target)
        except (zipfile.BadZipFile, OSError):
            continue
        found = _find_build_root(target, depth + 1)
        if found is not None:
            return found
    return None


class BuildStore:
    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.builds_dir = self.config_dir / "builds"
        self.ledger_path = self.config_dir / "versions.json"
        self.context_dir = self.config_dir / "build"          # podman build context
        self.ledger = self.load()
        try:
            self.backfill_all(allow_network=False)   # offline: README on disk only
        except OSError:
            pass

    # ── persistence ─────────────────────────────────────────────────────
    def load(self) -> Ledger:
        if not self.ledger_path.exists():
            return Ledger()
        try:
            raw = json.loads(self.ledger_path.read_text())
        except (OSError, ValueError):
            return Ledger()
        builds = []
        for b in raw.get("builds", []):
            known = {k: v for k, v in b.items() if k in BuildEntry.__dataclass_fields__}
            builds.append(BuildEntry(**known))
        return Ledger(active=raw.get("active", ""), builds=builds,
                      skipped=raw.get("skipped", []))

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "active": self.ledger.active,
            "builds": [asdict(b) for b in self.ledger.builds],
            "skipped": self.ledger.skipped,
        }
        tmp = self.ledger_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.ledger_path)

    # ── support info ────────────────────────────────────────────────────
    def backfill(self, entry: BuildEntry, allow_network: bool = False) -> bool:
        """Fill in what a build supports. Local README first (always present in
        CI archives), upstream constants.yml for boot/expansion versions."""
        changed = False
        if not entry.game_version or not entry.patch:
            label, game = read_local_support(self.build_path(entry.sha))
            if label and label != entry.patch:
                entry.patch, changed = label, True
            if game and game != entry.game_version:
                entry.game_version, changed = game, True
        if allow_network and not entry.is_dev and (
                not entry.expac_versions or not entry.boot_version
                or not entry.game_version):
            sup = fetch_support(entry.sha)
            if sup["label"] and not entry.patch:
                entry.patch, changed = sup["label"], True
            if sup["game"] and sup["game"] != entry.game_version:
                entry.game_version, changed = sup["game"], True
            if sup["boot"] and sup["boot"] != entry.boot_version:
                entry.boot_version, changed = sup["boot"], True
            if sup["expac"] and sup["expac"] != entry.expac_versions:
                entry.expac_versions, changed = sup["expac"], True
        return changed

    def backfill_all(self, allow_network: bool = False) -> int:
        n = sum(1 for b in self.ledger.builds if self.backfill(b, allow_network))
        if n:
            self.save()
        return n

    # ── inspection ──────────────────────────────────────────────────────
    def build_path(self, sha: str) -> Path:
        return self.builds_dir / sha

    def has_files(self, sha: str) -> bool:
        return (self.build_path(sha) / "kawari-run").exists()

    def has_oodle(self, path: Path) -> bool:
        return (path / OODLE_NAMES[0]).exists()

    @staticmethod
    def read_version(folder: Path) -> str:
        v = Path(folder) / "version.txt"
        if not v.exists():
            return ""
        # CI does `echo ${{ github.sha }} >> version.txt`, so take the last line.
        lines = [line.strip() for line in v.read_text().splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def adopt_legacy(self) -> BuildEntry | None:
        """First run after upgrading the manager: register the build that is
        already in ~/.kawari/build/kawari so it gets a history entry."""
        # Only on a genuinely empty ledger. The build context is overwritten by
        # every stage_context(), so once history exists, whatever sits there is a
        # staged COPY of a known build - adopting it again would resurrect builds
        # the user deliberately deleted or pruned.
        if self.ledger.builds:
            return None
        legacy = self.context_dir / "kawari"
        sha = self.read_version(legacy)
        if not sha:
            return None
        dest = self.build_path(sha)
        if not dest.exists():
            shutil.copytree(legacy, dest, symlinks=True)
        # No network here: this runs during app startup. The README inside the
        # build gives the label and game version; the per-expansion constants
        # are filled the first time the Updates page checks upstream.
        entry = BuildEntry(sha=sha, installed_at=_now(), source="adopted")
        entry.patch, entry.game_version = read_local_support(dest)
        self.ledger.builds.insert(0, entry)
        if not self.ledger.active:
            self.ledger.active = sha
        self.save()
        return entry

    # ── import ──────────────────────────────────────────────────────────
    def import_zip(self, zip_path: Path, source: str,
                   oodle_from: Path | None = None) -> BuildEntry:
        with tempfile.TemporaryDirectory(prefix="kawari-import-") as tmp:
            tmp = Path(tmp)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp)
            root = _find_build_root(tmp)
            if root is None:
                raise RuntimeError("archive does not contain kawari-run")
            return self.import_dir(root, source, oodle_from)

    def import_dir(self, root: Path, source: str, oodle_from: Path | None = None,
                   meta: dict | None = None) -> BuildEntry:
        """Store a build folder (kawari-run, version.txt, ...) under its id.

        ``meta`` comes from a source build, which already knows its commit
        subject and supported versions from the tree it compiled. Given it, no
        network lookup is made: a dev build's id is not a commit GitHub has, and
        even a real commit may not be pushed yet.
        """
        root = Path(root)
        if not (root / "kawari-run").exists():
            raise RuntimeError(f"{root} does not contain kawari-run")
        sha = self.read_version(root)
        if not sha:
            raise RuntimeError("build has no version.txt - cannot version it")

        dest = self.build_path(sha)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, dest, symlinks=True)

        for exe in dest.glob("kawari-*"):
            exe.chmod(0o755)
        self._ensure_oodle(dest, oodle_from)

        entry = self.ledger.get(sha)
        if entry is None or entry.sha != sha:
            entry = BuildEntry(sha=sha)
            self.ledger.builds.insert(0, entry)
        entry.installed_at = _now()
        entry.source = source
        if meta is not None:
            entry.patch, entry.game_version = read_local_support(dest)
            entry.boot_version = meta.get("boot", "") or entry.boot_version
            entry.expac_versions = meta.get("expac") or entry.expac_versions
            entry.game_version = meta.get("game", "") or entry.game_version
            entry.commit_date = meta.get("date", "")
            entry.commit_message = meta.get("message", "")
            entry.notes = meta.get("notes", "")
        else:
            self.backfill(entry, allow_network=True)
            commit = fetch_commit(sha)
            if commit:
                entry.commit_date = commit.get("date", "")
                entry.commit_message = commit.get("message", "")
        self.save()
        return entry

    def _ensure_oodle(self, dest: Path, oodle_from: Path | None) -> None:
        """Oodle is not in the upstream archive. Carry it forward from the
        active build (or an explicit source) so every stored build is complete."""
        if self.has_oodle(dest):
            return
        candidates = []
        if oodle_from:
            candidates.append(Path(oodle_from))
        if self.ledger.active:
            candidates.append(self.build_path(self.ledger.active))
        candidates.append(self.context_dir / "kawari")
        for cand in candidates:
            src = cand / OODLE_NAMES[0] if cand.is_dir() else cand
            if src.exists():
                for name in OODLE_NAMES:
                    shutil.copy2(src, dest / name)
                return

    # ── activation ──────────────────────────────────────────────────────
    def stage_context(self, sha: str) -> Path:
        """Make ~/.kawari/build/ the podman build context for this sha."""
        src = self.build_path(sha)
        if not self.has_files(sha):
            raise RuntimeError(f"no stored files for {sha[:9]}")
        if not self.has_oodle(src):
            raise RuntimeError(
                f"build {sha[:9]} has no Oodle library - install Oodle from Setup first")
        target = self.context_dir / "kawari"
        if target.exists():
            shutil.rmtree(target)
        self.context_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target, symlinks=True)
        (self.context_dir / "Containerfile").write_text(build_containerfile())
        entry = self.context_dir / "entrypoint.sh"
        entry.write_text(build_entrypoint())
        entry.chmod(0o755)
        return self.context_dir

    def set_active(self, sha: str) -> None:
        self.ledger.active = sha
        self.save()

    def delete(self, sha: str) -> None:
        if sha == self.ledger.active:
            raise RuntimeError("refusing to delete the active build")
        path = self.build_path(sha)
        if path.exists():
            shutil.rmtree(path)
        self.ledger.builds = [b for b in self.ledger.builds if b.sha != sha]
        self.save()

    def prune(self, keep: int) -> list[str]:
        """Remove old unpinned builds beyond `keep`, never touching the active one."""
        removed = []
        candidates = [b for b in self.ledger.builds
                      if not b.pinned and b.sha != self.ledger.active]
        survivors = len(self.ledger.builds) - len(candidates)
        for b in candidates:
            if survivors < keep:
                survivors += 1
                continue
            self.delete(b.sha)
            removed.append(b.sha)
        return removed


# ════════════════════════════════════════════════════════════════════════════
#  What a build supports, and whether the client matches
# ════════════════════════════════════════════════════════════════════════════
_README_RE = re.compile(r"supports patch \*\*([^*]+)\*\*\s*\(([0-9.]+)\)")


def parse_readme_support(text: str) -> tuple[str, str]:
    """('7.56h1', '2026.09.15.0000.0000') from the README's support line."""
    m = _README_RE.search(text or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r"supports patch \*\*([^*]+)\*\*", text or "")
    return (m.group(1).strip() if m else ""), ""


def read_local_support(build_dir: Path) -> tuple[str, str]:
    """CI ships README.md inside every build archive, so each stored build knows
    what it supports without any network access."""
    readme = Path(build_dir) / "README.md"
    if not readme.exists():
        return "", ""
    try:
        return parse_readme_support(readme.read_text(errors="replace"))
    except OSError:
        return "", ""


def parse_support_constants(text: str) -> dict:
    """SUPPORTED_* out of resources/data/constants.yml (flat keys + one list).
    These are compiled into the binaries, and resources/data/ is excluded from
    the artifact, so the only readable copy is upstream source at the commit."""
    out = {"boot": "", "game": "", "expac": []}
    in_list = False
    for line in text.splitlines():
        if line.startswith("SUPPORTED_EXPAC_VERSIONS:"):
            in_list = True
            continue
        if in_list:
            m = re.match(r"\s*-\s*([0-9.]+)\s*$", line)
            if m:
                out["expac"].append(m.group(1))
                continue
            in_list = False
        m = re.match(r"SUPPORTED_(BOOT|GAME)_VERSION:\s*([0-9.]+)", line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def fetch_support(sha: str, timeout: int = 15) -> dict:
    """label + all SUPPORTED_* constants for a commit, from upstream source."""
    out = {"label": "", "game": "", "boot": "", "expac": []}
    try:
        out["label"], out["game"] = parse_readme_support(
            upstream.fetch_text(sha, "README.md", timeout=timeout))
    except Exception:                                   # noqa: BLE001
        pass
    try:
        consts = parse_support_constants(
            upstream.fetch_text(sha, "resources/data/constants.yml", timeout=timeout))
        out["boot"], out["expac"] = consts["boot"], consts["expac"]
        out["game"] = consts["game"] or out["game"]     # the constant is authoritative
    except Exception:                                   # noqa: BLE001
        pass
    return out


def _vkey(v: str) -> tuple:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return ()


def read_client_versions(game_path: str) -> dict:
    """The versions the client will report, read from its own .ver files.

    game_path is the 'game' folder (the one containing sqpack/). Boot sits
    beside it. Each expansion has sqpack/exN/exN.ver.
    """
    out = {"game": "", "boot": "", "expac": {}}
    if not game_path:
        return out
    root = Path(game_path)

    def read(p: Path) -> str:
        try:
            return p.read_text(errors="replace").strip()
        except OSError:
            return ""
    out["game"] = read(root / "ffxivgame.ver")
    out["boot"] = read(root.parent / "boot" / "ffxivboot.ver")
    sqpack = root / "sqpack"
    if sqpack.is_dir():
        for d in sorted(sqpack.iterdir()):
            m = re.fullmatch(r"ex(\d+)", d.name)
            if m and d.is_dir():
                v = read(d / f"{d.name}.ver")
                if v:
                    out["expac"][int(m.group(1))] = v
    return out


def compare_client(entry: "BuildEntry | None", client: dict,
                   enforce: bool = True) -> list[tuple[str, str]]:
    """Mirror servers/patch/src/main.rs exactly. Returns (level, message).

    * client game or any expansion NEWER than supported -> the patch server
      returns HTTP 500 and the client cannot log in
    * equal -> accepted
    * older -> accepted with a warning; the server tries to serve patches
    All of this only runs while tweaks.enforce_validity_checks is on.
    """
    if entry is None or not entry.game_version:
        return [("idle", "The active build's supported versions are not known yet - "
                         "run Check on the Updates page.")]
    if not client.get("game"):
        return [("warn", "Could not read ffxivgame.ver from the game path.")]

    out = []
    blocking = "err" if enforce else "warn"
    consequence = (" The patch server will refuse the login - update Kawari." if enforce else
                   " Allowed only because tweaks.enforce_validity_checks is off.")
    cg, sg = client["game"], entry.game_version
    if _vkey(cg) > _vkey(sg):
        out.append((blocking, f"Client game {cg} is NEWER than this build supports ({sg})."
                              + consequence))
    elif _vkey(cg) < _vkey(sg):
        out.append(("warn", f"Client game {cg} is older than the build's {sg}. The patch "
                            f"server will try to serve patches from patches/game; update "
                            f"the client through Astra."))
    else:
        out.append(("ok", f"Client game {cg} matches exactly."))

    for n, cv in sorted(client.get("expac", {}).items()):
        idx = n - 1                                   # ex1 -> index 0, as upstream does
        if idx >= len(entry.expac_versions):
            continue
        sv = entry.expac_versions[idx]
        if _vkey(cv) > _vkey(sv):
            out.append((blocking, f"ex{n} {cv} is NEWER than supported ({sv})." + consequence))
    if entry.boot_version and client.get("boot") and client["boot"] != entry.boot_version:
        out.append(("idle", f"Boot {client['boot']} vs supported {entry.boot_version} "
                            f"(informational - the launcher handles boot)."))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Upstream
# ════════════════════════════════════════════════════════════════════════════
# Re-exported so callers need not know which module raises them.
RateLimited = upstream.RateLimited
Unrelated = upstream.Unrelated


def fetch_patch(sha: str, timeout: int = 15) -> str:
    """The game patch a commit targets, read from README at that commit."""
    import re
    try:
        text = upstream.fetch_text(sha, "README.md", timeout=timeout)
    except Exception:                                   # noqa: BLE001
        return ""
    m = re.search(r"supports patch \*\*([^*]+)\*\*", text)
    return m.group(1).strip() if m else ""


def fetch_commit(sha: str, token: str = "") -> dict:
    """Subject, author and date of a commit; {} when the forge does not know it."""
    try:
        return upstream.resolve(sha, token=token)
    except Exception:                                   # noqa: BLE001
        return {}


def fetch_head(branch: str = "", token: str = "") -> dict:
    """The newest commit on the configured source's branch."""
    return upstream.head(branch, token=token)


def fetch_compare(base: str, head: str, token: str = "") -> dict:
    """Commits between the installed build and upstream head, oldest first.
    Raises Unrelated when the two share no history in this repository."""
    return upstream.compare(base, head, token=token)


def fetch_recent(ref: str = "", limit: int = 30, token: str = "") -> list:
    """The newest commits on a branch, newest first - the commit picker."""
    return upstream.recent_commits(ref, limit, token=token)


# ════════════════════════════════════════════════════════════════════════════
#  Update / rollback pipeline (runs off the UI thread)
# ════════════════════════════════════════════════════════════════════════════
class SkipStep(Exception):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def image_tags(podman: str, image: str) -> set[str]:
    try:
        out = subprocess.run(
            [podman, "images", "--format", "{{.Tag}}", image],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}
