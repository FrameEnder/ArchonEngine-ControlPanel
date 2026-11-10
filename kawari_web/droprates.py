"""Dashboard -> Drop rates: how often each dungeon's and trial's mounts and minions drop from its coffers.

The world server reads drop-rates.json from its data folder (the folder mounted at /kawari/data, beside world.db and
glamours.txt; servers/world/src/server/drop_rates.rs) again whenever it changes, so a change needs no restart:

    {"format": 1, "default": {"mounts_minions": 90}, "duties": {"57": {"mounts_minions": 100}}}

``duties`` is by ContentFinderCondition ID. A duty left blank here takes the default; without the file the server uses
90 %. A coffer's other items aren't affected: they're picked from what the duty's script lists, as before. The duties'
names come from content-index.json (kawari-questdump content), so no game text is kept here.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from flask import flash, redirect, render_template, request, url_for

from kawari_core import content_progress as CP
from kawari_core import questlab as Q

from . import auth
from .admin_base import dashboard, settings
from .questlab import _active_build

RATE_FILE = "drop-rates.json"
KEY = "mounts_minions"
# The server's own default (drop_rates.rs, MOUNTS_MINIONS_PERCENT), for when the file has none.
SERVER_DEFAULT = 90
# ContentType rows this page lists: dungeons and trials (names come from the index.)
CONTENT_TYPES = (2, 4)


def rate_path() -> Path:
    return Path(settings().db_dir) / RATE_FILE


def read_rates() -> tuple[dict, float | None]:
    path = rate_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else {}), path.stat().st_mtime
    except (OSError, ValueError):
        return {}, None


def _percent(value) -> int | None:
    """A rate typed in the form or found in the file: a whole percent 0-100, or None for blank or nonsense."""
    if value is None:
        return None
    text = str(value).strip().rstrip("%").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return max(0, min(100, round(number)))


def default_rate(data: dict) -> int:
    rate = _percent((data.get("default") or {}).get(KEY))
    return SERVER_DEFAULT if rate is None else rate


def duty_rates(data: dict) -> dict[int, int]:
    rates = {}
    for key, entry in (data.get("duties") or {}).items():
        rate = _percent((entry or {}).get(KEY)) if isinstance(entry, dict) else None
        if rate is not None and str(key).isdigit():
            rates[int(key)] = rate
    return rates


def write_rates(default: int, duties: dict[int, int]) -> None:
    """Written beside the file and renamed over it, so the server never reads half of one."""
    path = rate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": 1, "default": {KEY: default},
            "duties": {str(k): {KEY: v} for k, v in sorted(duties.items())}}
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".drop-rates-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=1)
        out.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _groups():
    """The dungeons and trials from content-index.json, by content type then expansion and level."""
    data = CP.load(Q.dumps_root(settings().data_dir), _active_build())
    groups = []
    for content_type in CONTENT_TYPES:
        duties = [d for d in data.duties if d.content_type == content_type]
        if duties:
            name = data.type_names.get(content_type) or CP.FALLBACK_TYPE_NAMES.get(content_type, str(content_type))
            groups.append({"type": content_type, "name": name, "duties": duties})
    return data, groups


@dashboard.route("/drop-rates")
@auth.admin_required
def drop_rates():
    data, modified = read_rates()
    index, groups = _groups()
    return render_template(
        "dashboard/drop_rates.html", section="droprates",
        groups=groups, loaded=index.loaded, default=default_rate(data), rates=duty_rates(data),
        expansion_names=index.expansion_names, path=rate_path(), server_default=SERVER_DEFAULT,
        modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)) if modified else "")


@dashboard.route("/drop-rates", methods=["POST"])
@auth.admin_required
def drop_rates_save():
    default = _percent(request.form.get("default"))
    if default is None:
        default = SERVER_DEFAULT
    duties = {}
    for field, value in request.form.items():
        if field.startswith("duty-") and field[5:].isdigit():
            rate = _percent(value)
            if rate is not None:
                duties[int(field[5:])] = rate
    write_rates(default, duties)
    flash(f"Saved: {default} % by default, {len(duties)} duties of their own. The server reads it at once.", "ok")
    return redirect(url_for("dashboard.drop_rates"))
