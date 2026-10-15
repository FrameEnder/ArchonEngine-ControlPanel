"""Where Kawari itself comes from.

Everything that reaches out to the Kawari project goes through here: the commit
history, the per-commit source files the schema map is derived from, and the
build archive that becomes the container image. Nothing else in the codebase may
hardcode a repository, a forge or ``xiv.zone``.

Three things are redirected together, because they have to be:

* the **API** (commits, compare) - the changelog and "how far behind"
* the **source files** at a given commit - ``schemagen`` derives the database
  schema from the Diesel migrations, so a fork that changes a migration MUST be
  read from the fork or the generated migration will be wrong
* the **build archive**, or the checkout a source build compiles

**Forges.** Two API families cover everything worth supporting:

* ``github`` - github.com (and GitHub Enterprise, through the API override).
* ``gitea`` - Gitea, and Forgejo, which is a fork of Gitea serving the same
  ``/api/v1``. Codeberg runs Forgejo, and upstream Kawari moved there
  (codeberg.org/redstrate/Kawari), so this is now the default. A self-hosted
  Gitea - on a Tailscale address, over plain http - is the same code path.

The two differ in small ways that matter, all handled here and checked against
a real Gitea 1.24: Gitea's compare lists commits newest first where GitHub
lists them oldest first, and reports ``total_commits`` where GitHub reports
``ahead_by``; Gitea authenticates with ``token <t>`` and serves raw files
through the API (so a private repo needs no fallback); a private release asset
is downloaded from the web host, not an API host.

**Specific commits.** Every build path takes an optional ref - a full or short
commit, a tag, or a branch - for installing something other than the branch's
head. A source build fetches and compiles it; a CI artifact is looked up by its
commit; a release by its tag. A plain archive URL cannot pick a commit, and
says so.

The configured source is process-wide state (``configure()`` / ``source()``)
because it is a property of the installation, not of a call.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace

from . import constants as C

FORGES = ("github", "gitea")
FORGE_LABELS = {
    "github": "GitHub",
    "gitea": "Gitea / Forgejo (Codeberg)",
}
GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

# Upstream moved from GitHub to Codeberg. Changing these changes the default
# for every installation that has not chosen a source of its own.
DEFAULT_FORGE = "gitea"
DEFAULT_HOST = "https://codeberg.org"
DEFAULT_OWNER = "redstrate"
DEFAULT_REPO = "Kawari"
DEFAULT_BRANCH = "master"
# Where upstream used to live. A saved source still pointing here is offered
# the move (see Source.is_old_upstream).
OLD_UPSTREAM = ("github", GITHUB_WEB, "redstrate", "Kawari")

BUILD_MODES = ("url", "actions", "release", "source", "local")
# Modes that compile rather than download. They need the Podman socket and a
# builder image, and they produce their own version id.
SOURCE_MODES = ("source", "local")
MODE_LABELS = {
    "url": "Download: direct archive URL",
    "actions": "Download: latest successful CI artifact",
    "release": "Download: latest release asset",
    "source": "Build from source: clone the branch",
    "local": "Build from source: a folder on this server",
}
MODE_HELP = {
    "url": "Downloads a .zip from a fixed address. This is how upstream "
           "publishes: xiv.zone serves the newest build of redstrate/Kawari - "
           "so for a fork this downloads upstream's code, not yours, and it "
           "can never install a specific older commit.",
    "actions": "Downloads the artifact of the newest successful CI run on the "
               "branch (or of a chosen commit). No compiler needed here, but every "
               "change has to be pushed and go green first. Needs a token. On "
               "Gitea this needs 1.23 or newer; Forgejo has no artifact API yet.",
    "release": "Downloads an asset from the newest release (or a release chosen "
               "by tag). Use this if the repository publishes releases.",
    "source": "Clones (then fetches) the branch and compiles it here with cargo, "
              "in a builder container. Builds exactly what is pushed, or any "
              "commit or tag you choose. The first build downloads the toolchain "
              "and every crate; later ones reuse both and only recompile what "
              "changed.",
    "local": "Compiles a checkout that already sits on this machine - the one "
             "you are editing. Uncommitted changes are included, and such a "
             "build is versioned <commit>-dev<time> so it never masquerades as "
             "the commit it started from. The folder is mounted read-only; "
             "nothing is written into it.",
}

USER_AGENT = f"kawari-web/{C.APP_VERSION}"
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class UpstreamError(RuntimeError):
    """The configured source could not be used."""


class RateLimited(UpstreamError):
    pass


class Unrelated(UpstreamError):
    """Two commits with no history in common in this repository - typically
    the active build came from a different repository than the one now
    configured."""


# ════════════════════════════════════════════════════════════════════════════
#  Repository URLs
# ════════════════════════════════════════════════════════════════════════════
def guess_forge(host: str) -> str:
    """github for github.com; everything else is assumed Gitea/Forgejo, which
    is what a self-hosted forge almost always is. The form can override it."""
    name = (urllib.parse.urlsplit(host).hostname or "").lower()
    return "github" if name in ("github.com", "www.github.com") else "gitea"


def parse_repo_url(url: str) -> dict:
    """Split a repository's web address into host, owner and repo.

    Accepts what people paste: with or without ``.git``, with a trailing
    slash, or a deeper page of the repository (``/src/branch/master``,
    ``/tree/main``, ``/commit/<sha>``). The branch is picked up from those
    where it is present.
    """
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("A repository URL starts with http:// or https://, e.g. "
                         "https://codeberg.org/redstrate/Kawari")
    parts = urllib.parse.urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise ValueError(f"{url} has no owner/repository in it")
    # A Gitea instance served from a sub-path (https://host/git/owner/repo)
    # is rare; the owner/repo pair is taken as the first two segments, and
    # the API override covers the rest.
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    branch = ""
    rest = segments[2:]
    if len(rest) >= 3 and rest[0] == "src" and rest[1] == "branch":
        branch = rest[2]
    elif len(rest) >= 2 and rest[0] == "tree":
        branch = rest[1]
    host = f"{parts.scheme}://{parts.netloc}"
    return {"host": host, "owner": owner, "repo": repo, "branch": branch,
            "forge": guess_forge(host)}


# ════════════════════════════════════════════════════════════════════════════
#  The source
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Source:
    forge: str = DEFAULT_FORGE
    # The forge's web address: https://github.com, https://codeberg.org,
    # http://100.79.235.78:3769 ...
    host: str = DEFAULT_HOST
    owner: str = DEFAULT_OWNER
    repo: str = DEFAULT_REPO
    branch: str = DEFAULT_BRANCH
    # Overrides, normally empty: derived from forge + host. Only needed for
    # GitHub Enterprise or a forge behind an unusual reverse proxy.
    api_base: str = ""
    raw_base: str = ""
    build_mode: str = "url"
    build_url: str = C.BUILD_URL
    # Substring an artifact or release asset name must contain. Empty means
    # "the first one that looks like a Linux x86-64 zip, else the first".
    artifact: str = ""
    token: str = ""
    # local mode: the checkout's path ON THE HOST (the builder container mounts
    # it; this app's own container never needs to see it).
    local_path: str = ""
    # Source modes: trade the release profile's LTO and single codegen unit for
    # an incremental build.
    fast_build: str = "1"

    # ── identity ────────────────────────────────────────────────────────
    @property
    def web_base(self) -> str:
        return self.host.rstrip("/")

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"{self.web_base}/{self.slug}"

    @property
    def clone_url(self) -> str:
        return f"{self.repo_url}.git"

    @property
    def host_name(self) -> str:
        return urllib.parse.urlsplit(self.web_base).netloc or self.web_base

    @property
    def forge_label(self) -> str:
        if self.host_name.lower() == "codeberg.org":
            return "Codeberg (Forgejo)"
        return FORGE_LABELS.get(self.forge, self.forge)

    @property
    def label(self) -> str:
        where = "" if self.host_name in ("github.com", "codeberg.org") else f"{self.host_name}/"
        return f"{where}{self.slug} @ {self.branch}"

    @property
    def is_default(self) -> bool:
        """True when this points at upstream (a token does not make it a fork)."""
        return replace(self, token="") == replace(DEFAULT, token="")

    @property
    def is_upstream_repo(self) -> bool:
        """Upstream's repository, however it is built."""
        return (self.web_base.lower(), self.owner.lower(), self.repo.lower()) == \
               (DEFAULT_HOST, DEFAULT_OWNER.lower(), DEFAULT_REPO.lower())

    @property
    def is_old_upstream(self) -> bool:
        """Still pointing at upstream's former home on GitHub."""
        return (self.forge, self.web_base.lower(), self.owner.lower(), self.repo.lower()) == \
               (OLD_UPSTREAM[0], OLD_UPSTREAM[1], OLD_UPSTREAM[2].lower(), OLD_UPSTREAM[3].lower())

    @property
    def compiles(self) -> bool:
        return self.build_mode in SOURCE_MODES

    @property
    def build_origin(self) -> str:
        """How a build from this source is labelled in the version history.

        Names where the BINARY came from, not which repository is configured:
        a fork set to the xiv.zone URL installs upstream's build, and labelling
        that with the fork's name is exactly the confusion this prevents.
        """
        if self.build_mode == "url":
            return urllib.parse.urlsplit(self.build_url).hostname or "url"
        if self.build_mode == "local":
            return f"built locally ({self.local_path.rstrip('/').rsplit('/', 1)[-1]})"
        where = "" if self.host_name in ("github.com", "codeberg.org") else f"{self.host_name}/"
        if self.build_mode == "source":
            return f"built {where}{self.slug}"
        return f"{where}{self.slug} {self.build_mode}"

    @property
    def downloads_upstream_for_fork(self) -> bool:
        """A fork configured to download upstream's published binary."""
        return (self.build_mode == "url" and self.build_url == C.BUILD_URL
                and not self.is_upstream_repo and not self.is_old_upstream)

    def commit_url(self, sha: str) -> str:
        return f"{self.repo_url}/commit/{sha}"

    # ── endpoints ───────────────────────────────────────────────────────
    @property
    def api_root(self) -> str:
        if self.api_base:
            return self.api_base.rstrip("/")
        if self.forge == "github":
            return GITHUB_API if self.web_base.lower() == GITHUB_WEB else f"{self.web_base}/api/v3"
        return f"{self.web_base}/api/v1"

    def api_url(self, path: str) -> str:
        return f"{self.api_root}/repos/{self.slug}{path}"

    def raw_url(self, sha: str, path: str) -> str:
        if self.forge == "gitea":
            return self.api_url(f"/raw/{urllib.parse.quote(path)}?ref={urllib.parse.quote(sha)}")
        base = (self.raw_base or (GITHUB_RAW if self.web_base.lower() == GITHUB_WEB
                                  else f"{self.web_base}/raw")).rstrip("/")
        return f"{base}/{self.slug}/{sha}/{path}"

    def contents_url(self, sha: str, path: str) -> str:
        return self.api_url(f"/contents/{path}?ref={urllib.parse.quote(sha)}")

    # ── credentials ─────────────────────────────────────────────────────
    def auth_header(self) -> str:
        """Authorization header value for the API, or "" without a token."""
        if not self.token:
            return ""
        return f"token {self.token}" if self.forge == "gitea" else f"Bearer {self.token}"

    def git_auth_header(self) -> str:
        """A full ``Authorization: ...`` line for git over HTTP, or "".

        GitHub's git endpoint wants Basic with the token as the password;
        Gitea and Forgejo take the same ``token`` scheme as their API.
        """
        if not self.token:
            return ""
        if self.forge == "gitea":
            return f"Authorization: token {self.token}"
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return f"Authorization: Basic {basic}"

    def trusted_hosts(self) -> set[str]:
        """Hosts the token may be sent to: the API host, and for Gitea the web
        host too (private release assets download from it). Never a third
        party - a build URL may point anywhere."""
        hosts = {(urllib.parse.urlsplit(self.api_root).hostname or "").lower()}
        if self.forge == "gitea":
            hosts.add((urllib.parse.urlsplit(self.web_base).hostname or "").lower())
        hosts.discard("")
        return hosts

    # ── validation ──────────────────────────────────────────────────────
    def problems(self) -> list[str]:
        """Everything wrong with this configuration, before any network call."""
        out = []
        if self.forge not in FORGES:
            out.append(f"Unknown forge '{self.forge}'.")
        if not re.match(r"^https?://[^/\s]+$", self.web_base):
            out.append("The forge address must look like https://codeberg.org or "
                       "http://100.79.235.78:3769 (no path).")
        if not _NAME.match(self.owner or ""):
            out.append("Owner must be a user or organisation name.")
        if not _NAME.match(self.repo or ""):
            out.append("Repository must be a repository name.")
        if not (self.branch or "").strip():
            out.append("Branch cannot be empty.")
        for field_name, value in (("API override", self.api_base), ("Raw override", self.raw_base)):
            if value and not value.startswith(("http://", "https://")):
                out.append(f"{field_name} must be an http(s) URL, or empty.")
        if self.build_mode not in BUILD_MODES:
            out.append(f"Unknown build source '{self.build_mode}'.")
        if self.build_mode == "url" and not self.build_url.startswith(
                ("http://", "https://")):
            out.append("A direct archive URL is required for that build source.")
        if self.build_mode == "actions" and not self.token:
            out.append("Downloading a CI artifact needs a token - no forge serves "
                       "artifacts anonymously.")
        if self.build_mode == "local" and not self.local_path.startswith("/"):
            out.append("Building a local folder needs the checkout's absolute path "
                       "on this server, e.g. /home/you/src/Kawari.")
        if self.downloads_upstream_for_fork:
            out.append(f"The build source is upstream's archive on xiv.zone, so "
                       f"updating installs redstrate/Kawari's code - not "
                       f"{self.slug}'s. Choose 'Build from source' (or a CI "
                       f"artifact) to run your fork.")
        return out


DEFAULT = Source()
_current = DEFAULT


def source() -> Source:
    return _current


def configure(src: Source | None = None, **fields) -> Source:
    """Point the whole process at a source. Unknown fields are ignored, so a
    settings dict can be splatted in without filtering."""
    global _current
    base = src if src is not None else _current
    known = {k: v for k, v in fields.items()
             if k in Source.__dataclass_fields__ and v is not None}
    _current = replace(base, **known)
    return _current


def reset() -> Source:
    global _current
    _current = DEFAULT
    return _current


# ════════════════════════════════════════════════════════════════════════════
#  Transport
# ════════════════════════════════════════════════════════════════════════════
def _request(url: str, *, accept: str = "", token: str = "",
             src: Source | None = None) -> urllib.request.Request:
    src = src or _current
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if accept:
        req.add_header("Accept", accept)
    if token:
        src = replace(src, token=token)
    header = src.auth_header()
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if header and host in src.trusted_hosts():
        req.add_header("Authorization", header)
    return req


def _open(req, timeout: int, src: Source | None = None):
    src = src or _current
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 429):
            raise
        # 403 is GitHub's answer to both "out of quota" and "you may not have
        # this", and the two need different advice. The header distinguishes them.
        remaining = (exc.headers or {}).get("x-ratelimit-remaining")
        if remaining == "0" or exc.code == 429:
            raise RateLimited(
                f"{src.forge_label}'s rate limit is reached"
                + (" (60 requests an hour without a token)" if src.forge == "github" else "")
                + ". Add a token in Updates > Source, or try again later."
            ) from exc
        raise RateLimited(
            f"{src.forge_label} refused the request (403). A private repository "
            f"needs a token with repository read access; downloading a CI "
            f"artifact also needs Actions read access."
        ) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(
            f"Could not reach {urllib.parse.urlsplit(req.full_url).netloc}: {exc.reason}. "
            f"For a forge on a Tailscale address, check this machine is on the tailnet."
        ) from exc


def api_json(path: str, *, token: str = "", timeout: int = 20,
             src: Source | None = None):
    """GET a repository API path, e.g. "/branches/master"."""
    src = src or _current
    req = _request(src.api_url(path), accept="application/json" if src.forge == "gitea"
                   else "application/vnd.github+json", token=token, src=src)
    with _open(req, timeout, src) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_text(sha: str, path: str, *, timeout: int = 30,
               src: Source | None = None) -> str:
    """One file from the repository at one commit.

    GitHub: raw.githubusercontent.com first - no rate limit, which matters
    because schema generation reads five files per commit - falling back for a
    private repository to the Contents API, which the token can open.
    Gitea/Forgejo: the API's raw endpoint, which serves private files too.
    """
    src = src or _current
    if src.forge == "gitea":
        with _open(_request(src.raw_url(sha, path), src=src), timeout, src) as resp:
            return resp.read().decode("utf-8")
    try:
        with urllib.request.urlopen(
                _request(src.raw_url(sha, path), src=src), timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403, 404) or not src.token:
            raise
    req = _request(src.contents_url(sha, path), accept="application/vnd.github.raw",
                   src=src)
    with _open(req, timeout, src) as resp:
        return resp.read().decode("utf-8")


def download(url: str, *, timeout: int = 600, src: Source | None = None) -> bytes:
    """Fetch an archive. Authenticated only on the forge's own hosts."""
    src = src or _current
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    accept = ("application/octet-stream"
              if src.forge == "github" and host in src.trusted_hosts() else "")
    with _open(_request(url, accept=accept, src=src), timeout, src) as resp:
        return resp.read()


# ════════════════════════════════════════════════════════════════════════════
#  Commits
# ════════════════════════════════════════════════════════════════════════════
def commit_summary(c: dict) -> dict:
    """One commit, the same shape from either forge."""
    commit = c.get("commit") or {}
    message = (commit.get("message") or "").strip()
    author = commit.get("author") or {}
    return {
        "sha": c.get("sha", ""),
        "message": message.splitlines()[0] if message else "",
        "author": author.get("name", ""),
        "date": (author.get("date") or "")[:10],
    }


def _gitea_list(ref: str, limit: int) -> str:
    # stat/verification/files off: they make Gitea diff every commit listed.
    return (f"/commits?sha={urllib.parse.quote(ref, safe='')}&limit={limit}"
            f"&stat=false&verification=false&files=false")


def resolve(ref: str, *, src: Source | None = None, token: str = "") -> dict:
    """Any ref - branch, tag, full or short commit - to its commit."""
    src = src or _current
    ref = (ref or "").strip()
    if not ref:
        raise UpstreamError("No commit, tag or branch given.")
    try:
        if src.forge == "gitea":
            data = api_json(_gitea_list(ref, 1), src=src, token=token)
            if not data:
                raise UpstreamError(f"{ref} has no commits")
            return commit_summary(data[0])
        return commit_summary(api_json(f"/commits/{urllib.parse.quote(ref, safe='')}",
                                       src=src, token=token))
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 422):
            raise UpstreamError(
                f"{src.slug} has no commit, tag or branch called {ref!r}") from exc
        raise


def head(branch: str = "", *, src: Source | None = None, token: str = "") -> dict:
    """The newest commit on a branch (the configured one by default)."""
    src = src or _current
    return resolve(branch or src.branch, src=src, token=token)


def recent_commits(ref: str = "", limit: int = 30, *, src: Source | None = None,
                   token: str = "") -> list[dict]:
    """The newest ``limit`` commits reachable from ``ref``, newest first."""
    src = src or _current
    ref = ref or src.branch
    limit = max(1, min(100, limit))
    if src.forge == "gitea":
        data = api_json(_gitea_list(ref, limit), src=src, token=token, timeout=30)
    else:
        data = api_json(f"/commits?sha={urllib.parse.quote(ref, safe='')}&per_page={limit}",
                        src=src, token=token, timeout=30)
    return [commit_summary(c) for c in data or []]


def compare(base: str, head_sha: str, *, src: Source | None = None,
            token: str = "") -> dict:
    """Commits from ``base`` (exclusive) to ``head_sha``: {ahead_by, commits
    OLDEST FIRST, files}. Raises Unrelated when the two share no history here."""
    src = src or _current
    try:
        data = api_json(f"/compare/{base}...{head_sha}", src=src, token=token, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 422):
            raise
        if src.forge == "gitea":
            # Gitea before 1.22 and some Forgejo versions lack /compare; 404 is
            # also what an unknown base gives. Walk back from head to tell.
            return _compare_by_walking(base, head_sha, src, token)
        raise Unrelated(f"{base[:9]} is not a commit in {src.slug}") from exc
    commits = [commit_summary(c) for c in data.get("commits") or []]
    # GitHub lists oldest first; Gitea newest first. Put head last either way.
    if len(commits) > 1 and commits[0]["sha"] == head_sha:
        commits.reverse()
    ahead = data.get("ahead_by", data.get("total_commits", len(commits)))
    return {"ahead_by": int(ahead or 0), "commits": commits,
            "files": [f.get("filename", "") for f in data.get("files") or []]}


def _compare_by_walking(base: str, head_sha: str, src: Source, token: str) -> dict:
    newest_first = recent_commits(head_sha, 100, src=src, token=token)
    for i, c in enumerate(newest_first):
        if c["sha"].startswith(base) or base.startswith(c["sha"]):
            commits = list(reversed(newest_first[:i]))
            return {"ahead_by": len(commits), "commits": commits, "files": []}
    raise Unrelated(f"{base[:9]} is not among the last 100 commits of {src.slug}")


def forge_version(src: Source | None = None) -> str:
    """'Gitea 1.24.6', 'Forgejo 11.0.1' ... for the test button."""
    src = src or _current
    if src.forge == "github":
        return "GitHub" if src.web_base.lower() == GITHUB_WEB else "GitHub Enterprise"
    with _open(_request(f"{src.api_root}/version", src=src), 15, src) as resp:
        version = json.loads(resp.read().decode("utf-8")).get("version", "")
    # Forgejo reports e.g. "11.0.1+gitea-1.22.0".
    if "+gitea" in version:
        return f"Forgejo {version.split('+', 1)[0]}"
    return f"Gitea {version}" if version else "Gitea"


# ════════════════════════════════════════════════════════════════════════════
#  Finding the build archive
# ════════════════════════════════════════════════════════════════════════════
def _score_name(name: str) -> int:
    """How much a file name looks like the Linux x86-64 build we want."""
    low = name.lower()
    score = 0
    if low.endswith(".zip"):
        score += 4
    if "linux" in low:
        score += 3
    if "x86_64" in low or "x64" in low or "amd64" in low:
        score += 2
    if any(bad in low for bad in ("aarch64", "arm64", "windows", "macos",
                                  "darwin", "debug", "symbols", "pdb")):
        score -= 6
    return score


def _pick(names: list[str], wanted: str) -> int | None:
    """Index of the best candidate, or None."""
    if not names:
        return None
    if wanted:
        for i, n in enumerate(names):
            if wanted.lower() in n.lower():
                return i
        return None
    ranked = sorted(range(len(names)), key=lambda i: -_score_name(names[i]))
    return ranked[0]


def resolve_build(src: Source | None = None, timeout: int = 30, ref: str = "") -> dict:
    """Where a build archive can be downloaded from: the newest, or the one
    for ``ref`` (a commit for CI artifacts, a tag for releases).

    Returns {"url", "name", "origin", "note"}. Raises UpstreamError with an
    explanation a non-expert can act on.
    """
    src = src or _current
    ref = (ref or "").strip()
    if src.compiles:
        raise UpstreamError(
            f"'{MODE_LABELS[src.build_mode]}' compiles rather than downloads - "
            f"there is no archive to resolve.")
    if src.build_mode == "url":
        if ref:
            raise UpstreamError(
                "A direct archive URL serves whatever it serves - it cannot pick a "
                "commit. To install a specific commit, switch the build source to "
                "'Build from source: clone the branch'.")
        if not src.build_url:
            raise UpstreamError("No archive URL is configured.")
        return {"url": src.build_url, "name": src.build_url.rsplit("/", 1)[-1],
                "origin": urllib.parse.urlsplit(src.build_url).hostname or "url",
                "note": ""}
    if src.build_mode == "release":
        return _resolve_release(src, ref, timeout)
    return _resolve_artifact(src, ref, timeout)


def _resolve_release(src: Source, tag: str, timeout: int) -> dict:
    path = (f"/releases/tags/{urllib.parse.quote(tag, safe='')}" if tag
            else "/releases/latest")
    try:
        rel = api_json(path, timeout=timeout, src=src)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpstreamError(
                f"{src.slug} has no release tagged {tag!r}." if tag else
                f"{src.slug} has no published releases. Switch the build source "
                f"to a CI artifact, a direct URL or a source build.") from exc
        raise
    assets = rel.get("assets") or []
    names = [a.get("name", "") for a in assets]
    idx = _pick(names, src.artifact)
    if idx is None:
        raise UpstreamError(
            f"Release {rel.get('tag_name', '?')} has no asset matching "
            f"'{src.artifact or 'a Linux zip'}'. It has: "
            f"{', '.join(names) or 'no assets'}.")
    asset = assets[idx]
    if src.forge == "github":
        # The API url works for private repositories too; browser_download_url
        # does not.
        url = asset.get("url") if src.token else (
            asset.get("browser_download_url") or asset.get("url"))
    else:
        # Gitea's browser_download_url is on the forge's own host, which the
        # token is sent to - so it works for a private repository as it is.
        url = asset.get("browser_download_url", "")
    return {"url": url, "name": asset.get("name", ""), "origin": "release",
            "note": f"release {rel.get('tag_name', '')}"}


def _resolve_artifact(src: Source, ref: str, timeout: int) -> dict:
    want_sha = resolve(ref, src=src)["sha"] if ref else ""
    if src.forge == "gitea":
        return _resolve_gitea_artifact(src, want_sha, timeout)
    query = (f"head_sha={want_sha}" if want_sha
             else f"branch={urllib.parse.quote(src.branch)}")
    runs = api_json(f"/actions/runs?{query}&status=success&per_page=15",
                    timeout=timeout, src=src)
    for run in runs.get("workflow_runs") or []:
        arts = api_json(f"/actions/runs/{run['id']}/artifacts", timeout=timeout,
                        src=src).get("artifacts") or []
        live = [a for a in arts if not a.get("expired")]
        idx = _pick([a.get("name", "") for a in live], src.artifact)
        if idx is None:
            continue
        art = live[idx]
        return {"url": art.get("archive_download_url", ""),
                "name": art.get("name", ""), "origin": "actions",
                "note": (f"run {run.get('run_number', '')} "
                         f"({(run.get('head_sha') or '')[:9]}) "
                         f"{(run.get('created_at') or '')[:10]}")}
    raise UpstreamError(
        (f"No successful Actions run for {want_sha[:9]}" if want_sha
         else f"No successful Actions run on {src.branch}")
        + " has an unexpired artifact"
        + (f" matching '{src.artifact}'" if src.artifact else "")
        + ". GitHub deletes artifacts after 90 days by default; a source build "
          "can still install that commit.")


def _resolve_gitea_artifact(src: Source, want_sha: str, timeout: int) -> dict:
    """Gitea 1.23+ lists a repository's artifacts with the commit each came
    from. Forgejo does not (yet), and answers 404."""
    try:
        data = api_json("/actions/artifacts?limit=50", timeout=timeout, src=src)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpstreamError(
                f"{src.host_name} has no artifact API (Forgejo, and Gitea before "
                f"1.23). Publish a release, or build from source.") from exc
        raise
    arts = [a for a in data.get("artifacts") or [] if not a.get("expired")]
    if want_sha:
        arts = [a for a in arts
                if ((a.get("workflow_run") or {}).get("head_sha") or "") == want_sha]
    arts.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    idx = _pick([a.get("name", "") for a in arts], src.artifact)
    if idx is None:
        raise UpstreamError(
            (f"No artifact was built from {want_sha[:9]}" if want_sha
             else "This repository has no unexpired CI artifacts")
            + (f" matching '{src.artifact}'" if src.artifact else "")
            + ". A source build can install any commit.")
    art = arts[idx]
    head_sha = ((art.get("workflow_run") or {}).get("head_sha") or "")[:9]
    return {"url": art.get("archive_download_url", ""), "name": art.get("name", ""),
            "origin": "actions",
            "note": f"artifact from {head_sha or '?'} {(art.get('created_at') or '')[:10]}"}


# ════════════════════════════════════════════════════════════════════════════
#  The test button
# ════════════════════════════════════════════════════════════════════════════
def probe(src: Source | None = None) -> dict:
    """Try everything this source is used for and report what works.

    Independent checks, because they fail independently: a forge can be
    reachable while the repository is private, a branch readable while it has
    no artifact. Each line is what the dashboard shows.
    """
    src = src or _current
    out = {"ok": True, "checks": [], "head": "", "build": {}}

    def check(name: str, fn):
        try:
            out["checks"].append({"name": name, "ok": True, "detail": fn()})
        except Exception as exc:                        # noqa: BLE001
            out["ok"] = False
            out["checks"].append({"name": name, "ok": False, "detail": str(exc)})

    for problem in src.problems():
        out["ok"] = False
        out["checks"].append({"name": "Configuration", "ok": False,
                              "detail": problem})

    def forge():
        return f"{forge_version(src)} at {src.web_base}"

    def branch():
        c = head(src=src)
        out["head"] = c["sha"]
        return f"{c['sha'][:9]} — {c['message']} ({c['date']})"

    def sources():
        if not out["head"]:
            raise UpstreamError("skipped: the branch could not be read")
        text = fetch_text(out["head"], "servers/world/migrations/current_world/up.sql", src=src)
        tables = len(re.findall(r"CREATE TABLE", text, re.I))
        return f"read the world migration ({tables} tables) at {out['head'][:9]}"

    def readme():
        if not out["head"]:
            raise UpstreamError("skipped: the branch could not be read")
        text = fetch_text(out["head"], "README.md", src=src)
        m = re.search(r"supports patch \*\*([^*]+)\*\*", text)
        return (f"supports patch {m.group(1).strip()}" if m
                else "README read, but it does not state a supported patch")

    def build():
        if src.build_mode == "source":
            if not out["head"]:
                raise UpstreamError("skipped: the branch could not be read")
            return (f"will clone {src.clone_url} and compile {out['head'][:9]} "
                    f"({'fast incremental' if src.fast_build == '1' else 'full release'} "
                    f"profile), or any commit you choose")
        if src.build_mode == "local":
            return (f"will compile {src.local_path} - the folder itself is checked "
                    f"by the builder when a build starts, since only the host can "
                    f"see it")
        info = resolve_build(src)
        out["build"] = info
        return (f"{MODE_LABELS[src.build_mode]}: {info['name'] or info['url']}"
                + (f" — {info['note']}" if info["note"] else ""))

    check("Forge", forge)
    check("Branch", branch)
    check("Source files", sources)
    check("Supported patch", readme)
    check("Build archive", build)
    return out
