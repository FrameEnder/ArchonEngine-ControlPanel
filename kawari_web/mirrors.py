"""Git mirrors for building Kawari from source (Updates → Dependency mirrors).

Kawari's Cargo.toml fetches three of its libraries straight from GitHub (physis, Icarus, recastnavigation-rs-sys,
which also pulls the recastnavigation C++ library in as a git submodule). To build from your own Gitea or Forgejo
mirrors instead, each line here maps an original URL to a mirror; the builder turns them into
``git config url.<mirror>.insteadOf <original>`` and makes cargo fetch through git, so the rewrite applies to cargo's
own fetches and to the submodule, while Cargo.toml and Cargo.lock stay as they are (no merge conflicts with
upstream). A mirror has to have the commits the lock file asks for, so keep it syncing (a Gitea pull mirror).
"""

from __future__ import annotations

import re

from . import webdb

SETTING = "git_mirrors"

# What Kawari's build fetches from GitHub today, for the form's hint.
KNOWN = (
    "https://github.com/redstrate/physis",
    "https://github.com/redstrate/Icarus",
    "https://github.com/redstrate/recastnavigation-rs-sys",
    "https://github.com/recastnavigation/recastnavigation",
)

_URL = re.compile(r"^https?://[^\s'\"`$\;|&<>]+$")


def parse(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Lines of "original mirror" (or "original = mirror"); # starts a comment. Returns the pairs and the problems."""
    pairs, problems = [], []
    for number, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in re.split(r"\s*=\s*|\s+", line) if p]
        if len(parts) != 2:
            problems.append(f"Line {number}: give the original URL and the mirror's URL, separated by a space.")
            continue
        original, mirror = parts
        if not _URL.match(original) or not _URL.match(mirror):
            problems.append(f"Line {number}: both have to be plain http(s) URLs.")
            continue
        if original.rstrip("/") == mirror.rstrip("/"):
            problems.append(f"Line {number}: the mirror is the same URL.")
            continue
        pairs.append((original.rstrip("/"), mirror.rstrip("/")))
    return pairs, problems


def text(s) -> str:
    return webdb.get_setting(s.webapp_db, SETTING, "")


def pairs(s) -> list[tuple[str, str]]:
    return parse(text(s))[0]


def save(s, value: str) -> list[str]:
    found, problems = parse(value)
    if not problems:
        webdb.set_setting(s.webapp_db, SETTING, "\n".join(f"{a} {b}" for a, b in found))
    return problems


def builder_env(s) -> str:
    """For the builder: one "original mirror" pair per line."""
    return "\n".join(f"{a} {b}" for a, b in pairs(s))
