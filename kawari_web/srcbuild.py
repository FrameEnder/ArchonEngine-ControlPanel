"""Compile Kawari from source, for running a fork or a working tree in progress.

Downloading a published build is quick but it is always somebody else's binary:
xiv.zone serves upstream's, a CI artifact serves whatever was last pushed and
went green. Developing the server needs the opposite - compile exactly the code
in front of you, uncommitted edits included, and run it.

How it works:

* A **builder image** (``localhost/kawari-builder``) is built once: the official
  Rust image plus what Kawari's native dependencies need - cmake and libclang
  for recastnavigation's bindgen, rsync for syncing a tree.
* A throwaway **builder container** runs per build, through the same Podman API
  socket the rest of the dashboard uses. Everything it keeps lives under
  ``<data>/source/``: the cargo registry, the git checkout, and the target dir.
  Keeping the target dir is what makes the second build take minutes rather
  than the first build's tens of minutes.
* The result is packaged **exactly as CI packages it** (same file list, same
  ``version.txt``), so from there the ordinary update pipeline takes over: back
  up, generate the schema, build the image, activate, migrate, recreate.

The schema map is generated from the tree that was compiled, never from
GitHub - a working tree that changed a Diesel migration has not pushed it, and
the migration is the one thing the database step must get right.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from kawari_core import core as kcore
from kawari_core import schemagen, upstream
from kawari_core import versions as V

from .podman_api import container_spec

BUILDER_IMAGE = "localhost/kawari-builder"
# Bump when BUILDER_CONTAINERFILE changes, so an old builder is replaced.
BUILDER_TAG = "2"
BUILDER_REF = f"{BUILDER_IMAGE}:{BUILDER_TAG}"

BUILDER_CONTAINERFILE = """\
# Kawari Web source builder - generated, rebuilt when its tag changes.
FROM docker.io/library/rust:1-bookworm
# recastnavigation-sys runs bindgen (libclang) and cmake; rsync syncs a working
# tree into the build area without touching the original.
RUN apt-get update \\
 && apt-get install -y --no-install-recommends cmake clang libclang-dev rsync \\
 && rm -rf /var/lib/apt/lists/*
# The checkout is owned by the host user and mounted in; git refuses to read a
# repository owned by someone else unless told it is safe.
RUN git config --system --add safe.directory '*'
"""

# Runs inside the builder. Everything variable arrives through KW_* variables,
# so nothing user-supplied is ever spliced into the script text.
BUILD_SCRIPT = r"""
set -euo pipefail
say() { printf '==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

AUTH=()
if [ -n "${KW_AUTH_HEADER:-}" ]; then
  # GitHub wants Basic with the token as password; Gitea/Forgejo take
  # "token <t>". Either way it is one header line, built by Kawari Web.
  AUTH=(-c "http.extraHeader=${KW_AUTH_HEADER}")
fi

HEAD=""; DIRTY=0; BRANCH=""; SUBJECT=""; DATE=""
if [ "$KW_MODE" = source ]; then
  TREE="/work/git/${KW_TREE_KEY}"
  if [ ! -d "$TREE/.git" ]; then
    say "First build of ${KW_REPO}: creating a checkout"
    git init -q "$TREE"
    git -C "$TREE" remote add origin "$KW_REPO"
  fi
  cd "$TREE"
  git remote set-url origin "$KW_REPO"
  # KW_REF installs something other than the branch's head: a commit (full
  # or short), a tag, or another branch.
  TARGET="${KW_REF:-$KW_BRANCH}"
  say "Fetching ${TARGET}"
  # A branch, a tag or a full commit can be fetched by name. A short commit
  # cannot, so on failure fetch every branch and tag and resolve it here.
  if git "${AUTH[@]}" fetch --prune --depth=200 origin "$TARGET" 2>/dev/null; then
    git checkout -q --force --detach FETCH_HEAD
  else
    say "Fetching every branch and tag to find ${TARGET}"
    UNSHALLOW=""
    if [ -f .git/shallow ]; then UNSHALLOW="--unshallow"; fi
    git "${AUTH[@]}" fetch --prune --tags $UNSHALLOW origin '+refs/heads/*:refs/remotes/origin/*' \
      || fail "could not fetch from ${KW_REPO} - check the address, and the token for a private repository"
    COMMIT=$(git rev-parse --verify --quiet "${TARGET}^{commit}" \
             || git rev-parse --verify --quiet "origin/${TARGET}^{commit}") \
      || fail "${KW_REPO} has no commit, tag or branch called ${TARGET}"
    git checkout -q --force --detach "$COMMIT"
  fi
  # Remove stray untracked files but NOT ignored ones (no -x): core/build.rs
  # generates gitignored sources into the tree, and cargo will not regenerate
  # them while it considers the build script's output current.
  git clean -q -ffd
  BRANCH="${KW_REF:-$KW_BRANCH}"
else
  [ -f /src/Cargo.toml ] || fail "${KW_LOCAL_PATH} has no Cargo.toml - is that the Kawari checkout?"
  [ -d /src/servers/world ] || fail "${KW_LOCAL_PATH} has a Cargo.toml but no servers/world - not a Kawari checkout"
  if git -C /src rev-parse HEAD >/dev/null 2>&1; then
    HEAD=$(git -C /src rev-parse HEAD)
    BRANCH=$(git -C /src rev-parse --abbrev-ref HEAD)
    # --no-optional-locks: /src is read-only, and plain `git status` would try
    # to refresh the index on disk. Ignored files (config.yaml, *.db) do not
    # count; untracked source files do, because the build will include them.
    if [ -n "$(git -C /src --no-optional-locks status --porcelain)" ]; then DIRTY=1; fi
    SUBJECT=$(git -C /src log -1 --format=%s)
    DATE=$(git -C /src log -1 --format=%cs)
  fi
  TREE=/work/local
  mkdir -p "$TREE"
  say "Syncing ${KW_LOCAL_PATH} (read-only) into the build area"
  # Honour .gitignore: an ignored file is neither copied in nor deleted from
  # the build area. That matters both ways - your config.yaml, databases and
  # target/ stay out, and the files core/build.rs GENERATES into the tree
  # (core/src/opcodes.rs, doc_*.md, ...) survive the next sync. Deleting those
  # breaks the rebuild: cargo still thinks build.rs's output is current.
  rsync -a --delete --filter=':- .gitignore' \
    --exclude /.git/ --exclude /target/ --exclude /oodle/ \
    /src/ "$TREE"/
  cd "$TREE"
fi
if [ "$KW_MODE" = source ]; then
  HEAD=$(git rev-parse HEAD)
  SUBJECT=$(git log -1 --format=%s)
  DATE=$(git log -1 --format=%cs)
fi

STAMP=$(date -u +%y%m%d%H%M%S)
if [ -n "$HEAD" ] && [ "$DIRTY" = 0 ]; then
  ID="$HEAD"
elif [ -n "$HEAD" ]; then
  ID="${HEAD:0:9}-dev${STAMP}"
else
  ID="local-${STAMP}"
fi
say "Building ${ID}${BRANCH:+ (from ${BRANCH})}$([ "$DIRTY" = 1 ] && echo ' with uncommitted changes')"
if [ -n "$SUBJECT" ]; then say "Last commit: ${SUBJECT}"; fi

# core/build.rs adds <repo>/oodle to the link search path for --features oodle.
mkdir -p oodle
cp /oodle/liboodle-network-shared.so oodle/

export CARGO_TARGET_DIR="/cache/target/${KW_TREE_KEY}"
export CARGO_TERM_COLOR=never CARGO_TERM_PROGRESS_WHEN=never
if [ "$KW_FAST" = 1 ]; then
  # The workspace's release profile is lto=true, codegen-units=1, opt-level=z:
  # the smallest binary, at the slowest possible compile. For a dev server
  # trade that for an incremental build.
  export CARGO_PROFILE_RELEASE_LTO=false
  export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=256
  export CARGO_PROFILE_RELEASE_INCREMENTAL=true
  export CARGO_PROFILE_RELEASE_OPT_LEVEL=1
  say "Profile: fast incremental (no LTO)"
else
  say "Profile: full release, as CI builds it (slow)"
fi
say "$(rustc --version)"
cargo build --release --features oodle

# Package exactly what CI's upload-artifact step packages.
R="$CARGO_TARGET_DIR/release"
OUT=/out/build
rm -rf /out/build /out/sources /out/build.env
mkdir -p "$OUT"
for f in "$R"/kawari-*; do
  case "$f" in *.d) continue ;; esac
  if [ -f "$f" ] && [ -x "$f" ]; then cp "$f" "$OUT/"; fi
done
[ -f "$OUT/kawari-run" ] || fail "the build finished but produced no kawari-run"
cp LICENSE README.md "$OUT/" 2>/dev/null || true
mkdir -p "$OUT/resources"
rsync -a --exclude /data/ resources/ "$OUT/resources/"
echo "$ID" > "$OUT/version.txt"

# The files the schema map and the supported versions are derived from, taken
# from THIS tree - a dev tree's migration has not been pushed anywhere.
for f in ${KW_SCHEMA_FILES}; do
  if [ -f "$f" ]; then mkdir -p "/out/sources/$(dirname "$f")"; cp "$f" "/out/sources/$f"; fi
done

# One KEY=value per line; values never contain newlines (git %s is one line).
{
  echo "id=${ID}"; echo "head=${HEAD}"; echo "dirty=${DIRTY}"
  echo "branch=${BRANCH}"; echo "subject=${SUBJECT}"; echo "date=${DATE}"
} > /out/build.env
say "Built ${ID}: $(ls "$OUT" | grep -c '^kawari-') binaries"
"""


# ════════════════════════════════════════════════════════════════════════════
#  Paths
# ════════════════════════════════════════════════════════════════════════════
def source_root(s) -> Path:
    return Path(s.data_dir) / "source"


def cache_size(s) -> int:
    """Bytes kept between builds (registry, checkouts, target dirs)."""
    total = 0
    root = source_root(s)
    if not root.exists():
        return 0
    for p in root.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def clear_cache(s) -> None:
    root = source_root(s)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _tree_key(src: upstream.Source) -> str:
    if src.build_mode == "local":
        return "local"
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{src.owner}-{src.repo}")


# ════════════════════════════════════════════════════════════════════════════
#  Steps
# ════════════════════════════════════════════════════════════════════════════
def ensure_builder(podman, job) -> str:
    """Build the builder image if this tag does not exist yet."""
    if podman.image_exists(BUILDER_REF):
        return f"{BUILDER_REF} present"
    job.say(f"Building {BUILDER_REF} - once only: pulls the Rust image and "
            f"installs cmake, clang and rsync")
    import io
    import tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        data = BUILDER_CONTAINERFILE.encode()
        info = tarfile.TarInfo("Containerfile")
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    result = podman.build(buffer.getvalue(), BUILDER_REF, job)
    if not result.ok:
        raise RuntimeError(f"could not build the builder image: {result.error}")
    return f"built {BUILDER_REF}"


def find_oodle(s) -> Path:
    """Oodle is needed at LINK time with --features oodle, not only at runtime."""
    store = V.BuildStore(s.data_dir)
    candidates = []
    if store.ledger.active:
        candidates.append(store.build_path(store.ledger.active))
    candidates += [b for b in (store.build_path(e.sha) for e in store.ledger.builds)]
    candidates.append(Path(s.build_dir) / "kawari")
    for folder in candidates:
        lib = folder / V.OODLE_NAMES[0]
        if lib.exists():
            return lib
    raise RuntimeError("No Oodle library found. Install Oodle from Setup first - "
                       "Kawari links against it when compiled with --features oodle.")


def builder_spec(s, src: upstream.Source, oodle: Path, name: str, ref: str = "") -> dict:
    root = source_root(s)
    for sub in ("cache/registry", "cache/git", "cache/target", "work", "out"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    mounts = [
        (s.to_host(root / "cache" / "registry"), "/usr/local/cargo/registry", []),
        (s.to_host(root / "cache" / "git"), "/usr/local/cargo/git", []),
        (s.to_host(root / "cache" / "target"), "/cache/target", []),
        (s.to_host(root / "work"), "/work", []),
        (s.to_host(root / "out"), "/out", []),
        (s.to_host(oodle), "/oodle/liboodle-network-shared.so", ["ro"]),
    ]
    if src.build_mode == "local":
        # A HOST path, handed straight to Podman. This app's container never
        # sees it, which is why the builder checks it rather than this code.
        mounts.append((src.local_path.rstrip("/"), "/src", ["ro"]))
    env = {
        "KW_MODE": src.build_mode,
        "KW_TREE_KEY": _tree_key(src),
        "KW_REPO": src.clone_url,
        "KW_BRANCH": src.branch,
        "KW_LOCAL_PATH": src.local_path,
        "KW_FAST": "1" if src.fast_build == "1" else "0",
        "KW_SCHEMA_FILES": " ".join(schemagen.SOURCES.values()) + " README.md",
    }
    if src.build_mode == "source":
        env["KW_REF"] = ref
        header = src.git_auth_header()
        if header:
            env["KW_AUTH_HEADER"] = header
    spec = container_spec(name, BUILDER_REF, mounts, [], kcore.selinux_enforcing())
    spec.pop("restart_policy", None)
    spec.update({"command": ["bash", "-c", BUILD_SCRIPT], "env": env,
                 "work_dir": "/work"})
    return spec


def read_result(s) -> tuple[Path, dict]:
    out = source_root(s) / "out"
    build = out / "build"
    info: dict = {}
    try:
        for line in (out / "build.env").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key] = value
    except OSError:
        pass
    info["dirty"] = info.get("dirty") == "1"
    if not (build / "kawari-run").exists():
        raise RuntimeError("the builder finished without leaving a build behind")
    return build, info


def schema_from_tree(s) -> dict | None:
    """Derive the schema map from the files the builder copied out."""
    sources_dir = source_root(s) / "out" / "sources"
    files = {}
    for key, rel in schemagen.SOURCES.items():
        path = sources_dir / rel
        if not path.exists():
            return None
        files[key] = path.read_text()
    return schemagen.generate(files)


def support_from_tree(s) -> dict:
    sources_dir = source_root(s) / "out" / "sources"
    out = {"game": "", "boot": "", "expac": []}
    constants = sources_dir / "resources" / "data" / "constants.yml"
    if constants.exists():
        parsed = V.parse_support_constants(constants.read_text())
        out.update({"game": parsed.get("game", ""), "boot": parsed.get("boot", ""),
                    "expac": parsed.get("expac", [])})
    return out


def build_step(podman, ref: str = ""):
    """The job step: compile, import, pre-generate the schema. Sets context["sha"]."""
    def step(job):
        from .admin_base import settings
        s = settings()
        src = upstream.source()
        if not src.compiles:
            raise RuntimeError("the configured source does not build from source")
        problems = src.problems()
        if problems:
            raise RuntimeError(" ".join(problems))
        if ref and src.build_mode == "local":
            raise RuntimeError("A local folder builds whatever is checked out in it - "
                               "check out the commit there, or switch to 'clone the branch'.")
        if ref:
            job.say(f"Installing {ref} rather than the head of {src.branch}")
        job.say(f"Source: {src.label}" if src.build_mode == "source"
                else f"Source: {src.local_path} on this server")
        oodle = find_oodle(s)
        ensure_builder(podman, job)
        name = f"kawari-builder-{int(time.time())}"
        started = time.time()
        result = podman.run_once(builder_spec(s, src, oodle, name, ref), job, timeout=4 * 3600)
        if not result.ok:
            raise RuntimeError(f"the build failed ({result.error}) - the log above "
                               f"has cargo's output")
        folder, info = read_result(s)
        support = support_from_tree(s)
        meta = {
            "message": info.get("subject", ""),
            "date": info.get("date", ""),
            "game": support["game"], "boot": support["boot"], "expac": support["expac"],
            "notes": (f"built from {'uncommitted changes on ' if info.get('dirty') else ''}"
                      f"{info.get('branch') or 'a working tree'}"
                      + (f" at {info['head'][:9]}" if info.get("head") else "")),
        }
        store = V.BuildStore(s.data_dir)
        entry = store.import_dir(folder, src.build_origin, oodle_from=oodle, meta=meta)
        job.context["sha"] = entry.sha

        # Pre-seed the schema cache for this id from the compiled tree, so the
        # schema step never asks GitHub about a commit it may not have.
        try:
            schema = schema_from_tree(s)
        except schemagen.SchemaGenError as exc:
            schema = None
            job.say(f"Could not derive the schema from the tree: {exc}")
        if schema is not None:
            path = schemagen.cache_path(s.data_dir, entry.sha)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema, indent=1))
            job.say("Schema map derived from the compiled tree")
        # Measured here, once per build, rather than on every page view: the
        # target dir is gigabytes across a great many files.
        from . import webdb
        webdb.set_setting(s.webapp_db, "source_cache_bytes", str(cache_size(s)))
        minutes = (time.time() - started) / 60
        return f"{entry.short} in {minutes:.1f} min (supports {entry.patch_text})"
    return step
