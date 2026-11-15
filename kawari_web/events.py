"""Dashboard -> Events: turn the seasonal and special events the fork has built on, off, or on for a time (2.33.0).

An event is a festival to the game: while one is on, the server puts its festival in every ZoneInit, so the event's
NPCs and decorations are out and its quests and duties can be played. kawari_core.events has the file and the rule;
the server reads the file again whenever it changes, so there's no restart. What's listed comes from the active
build (resources/data/fork_events.json and festivals.yml) and the names of its quests and duties from the game's
indexes, so no game text is kept here.
"""

from __future__ import annotations

import time
from pathlib import Path

from flask import flash, redirect, render_template, request, url_for

from kawari_core import content_progress as CP
from kawari_core import events as E
from kawari_core import questlab as Q

from . import auth
from .admin_base import dashboard, settings
from .questlab import _active_build


def event_path() -> Path:
    return Path(settings().db_dir) / E.EVENT_FILE


def _page(data: dict, now: float) -> list[dict]:
    build = _active_build()
    dumps = Q.dumps_root(settings().data_dir)
    festivals = E.festival_names(build)
    quests = Q.load_index(dumps)
    duties = {d.id: d.name for d in CP.load(dumps, build).duties}
    rows = []
    for event in E.built(build):
        setting = data["events"].get(event["key"], {})
        kind, sentence = E.state(setting, now)
        rows.append({
            **event,
            "name": festivals.get(event["festival"]) or event["key"].replace("_", " ").title(),
            "quest_names": [(q, (quests.get(q) or {}).get("name") or f"Quest {q}") for q in event["quests"]],
            "duty_names": [(d, duties.get(d) or f"Duty {d}") for d in event["duties"]],
            "mode": setting.get("mode", "off"), "start": E.to_form(setting.get("start")),
            "end": E.to_form(setting.get("end")), "state": kind, "sentence": sentence,
            "retail": (E.clock(event["retail_start"]), E.clock(event["retail_end"])),
            "retail_form": (E.to_form(event["retail_start"]), E.to_form(event["retail_end"])),
        })
    return rows


@dashboard.route("/events")
@auth.admin_required
def events():
    data, modified = E.read(event_path())
    now = time.time()
    return render_template(
        "dashboard/events.html", section="events", rows=_page(data, now), path=event_path(),
        build=_active_build() is not None, now=E.clock(int(now)),
        modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(modified)) if modified else "")


def from_form(form, keys: list[str], old: dict) -> tuple[dict, list[str]]:
    """The file a saved form makes, and what was wrong with it. An event the page didn't list keeps its setting."""
    data = {"format": 1, "events": {key: setting for key, setting in old["events"].items() if key not in keys}}
    problems = []
    for key in keys:
        mode = form.get(f"mode|{key}", "off")
        if mode == "on":
            data["events"][key] = {"mode": "on"}
        elif mode == "scheduled":
            start, end = E.from_form(form.get(f"start|{key}")), E.from_form(form.get(f"end|{key}"))
            if start is not None and end is not None and end <= start:
                problems.append(f"{key}: its end is before its start, so it would never be on; left as it was.")
                if key in old["events"]:
                    data["events"][key] = old["events"][key]
                continue
            setting = {"mode": "scheduled"}
            if start is not None:
                setting["start"] = start
            if end is not None:
                setting["end"] = end
            data["events"][key] = setting
    return data, problems


@dashboard.route("/events", methods=["POST"])
@auth.admin_required
def events_save():
    old, _ = E.read(event_path())
    keys = [event["key"] for event in E.built(_active_build())]
    data, problems = from_form(request.form, keys, old)
    E.write(event_path(), data)
    for problem in problems:
        flash(problem, "err")
    now = time.time()
    on = sum(E.is_on(setting, now) for setting in data["events"].values())
    flash(f"Saved: {on} event{'s' if on != 1 else ''} on now, {len(data['events'])} set. The server reads it at once; "
          "players see an event's NPCs after their next zone change.", "ok")
    return redirect(url_for("dashboard.events"))
