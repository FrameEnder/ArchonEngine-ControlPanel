"""Persisting which Kawari repository this installation tracks.

``kawari_core.upstream`` holds the source as process-wide state; this is the
bridge between that and the settings table. The web app runs as one waitress
process, so applying it at startup plus after each save is enough - but it is
also applied on every dashboard request, which costs one indexed SQLite read and
means a source saved in one tab is in force in another.

Settings written by 2.6 - 2.8 predate forges: they always meant GitHub, and they
stored GitHub's API and raw hosts explicitly. ``read`` recognises that shape so
an existing installation keeps pointing exactly where it did - a saved fork on
GitHub must not silently become a Codeberg URL because the default moved.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kawari_core import upstream

from . import webdb

# setting key -> attribute on upstream.Source. The token keeps its old key, so
# a token saved before this existed carries over.
KEYS = {
    "upstream_forge": "forge",
    "upstream_host": "host",
    "upstream_owner": "owner",
    "upstream_repo": "repo",
    "upstream_branch": "branch",
    "upstream_api_base": "api_base",
    "upstream_raw_base": "raw_base",
    "upstream_build_mode": "build_mode",
    "upstream_build_url": "build_url",
    "upstream_artifact": "artifact",
    "github_token": "token",
    "upstream_local_path": "local_path",
    "upstream_fast_build": "fast_build",
}
# Settings that could be stored as "" on purpose, so "" must not mean
# "fall back to the default" for them.
EMPTY_IS_VALUE = {"api_base", "raw_base", "artifact", "token", "local_path"}
# What 2.6 - 2.8 stored for the hosts; meaningless now that they are derived.
LEGACY_GITHUB_HOSTS = {upstream.GITHUB_API, upstream.GITHUB_RAW,
                       upstream.GITHUB_API + "/", upstream.GITHUB_RAW + "/"}


def read(db: Path) -> upstream.Source:
    """The stored source, falling back field by field to the default."""
    stored = {key: webdb.get_setting(db, key, None) for key in KEYS}
    fields = {}
    for key, attr in KEYS.items():
        value = stored[key]
        if value is None or (value == "" and attr not in EMPTY_IS_VALUE):
            continue
        fields[attr] = value

    if stored["upstream_forge"] in (None, "") and any(
            stored[k] not in (None, "") for k in ("upstream_owner", "upstream_api_base")):
        # A 2.6 - 2.8 source: it was GitHub, whatever the default is today.
        fields["forge"], fields["host"] = "github", upstream.GITHUB_WEB
        api = fields.get("api_base", "")
        if api and api not in LEGACY_GITHUB_HOSTS:
            # A GitHub Enterprise API override: keep it, and the host it implies.
            fields["host"] = api.split("/api/")[0] if "/api/" in api else api
        for attr in ("api_base", "raw_base"):
            if fields.get(attr, "") in LEGACY_GITHUB_HOSTS:
                fields[attr] = ""
    return replace(upstream.DEFAULT, **fields)


def apply(db: Path) -> upstream.Source:
    """Point this process at the stored source."""
    return upstream.configure(read(db))


def save(db: Path, src: upstream.Source, *, keep_token: bool = False) -> None:
    for key, attr in KEYS.items():
        if keep_token and attr == "token":
            continue
        webdb.set_setting(db, key, getattr(src, attr))
    apply(db)


def clear(db: Path, *, keep_token: bool = True) -> None:
    """Back to upstream. The token is a credential, not a source, so it stays
    unless asked for explicitly."""
    token = read(db).token if keep_token else ""
    save(db, replace(upstream.DEFAULT, token=token))


def is_customised(db: Path) -> bool:
    return not read(db).is_default
