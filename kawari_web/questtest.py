"""Quest Lab: "Test this quest". One test session from setup to diagnostics bundle.

Start a test (on a quest's page) puts the chosen character at the chosen point in the quest, the same way "Set up a
character" does, turns the world server's packet capture on if it isn't (restarting the server if asked, since it
only reads that at startup), and remembers the session: which quest, on whom, from where, since when.

While it runs, the quest's page (and Diagnostics) say so. Finish the test sends a diagnostics bundle covering the
session, with the quest test in its meta.json, so questkit triage and compare know which quest to look at, and can
turn capture back off if the test turned it on. Cancel drops the session without sending anything.

Only one test runs at a time; it lives in the web app's settings (diag_quest_test).
"""

from __future__ import annotations

import json
import time

from flask import flash, redirect, request, url_for

from kawari_core import questlab as Q

from . import auth, webdb
from .admin_base import dashboard, settings
from .questlab import _root, prepare_character

SETTING = "diag_quest_test"


def current_test() -> dict | None:
    raw = webdb.get_setting(settings().webapp_db, SETTING, "")
    if not raw:
        return None
    try:
        test = json.loads(raw)
    except ValueError:
        return None
    started = test.get("started_at", time.time())
    test["minutes"] = max(1, int((time.time() - started) / 60) + 1)
    test["since"] = time.strftime("%H:%M on %b %d", time.localtime(started))
    return test


def _save(test: dict | None) -> None:
    webdb.set_setting(settings().webapp_db, SETTING, json.dumps(test) if test else "")


def _quest_name(quest_id: int) -> str:
    root = _root()
    dump = Q.find_dump(root, quest_id)
    if dump is not None:
        return dump.name
    return (Q.load_index(root).get(quest_id) or {}).get("name", "")


@dashboard.route("/quests/<int:quest_id>/test/start", methods=["POST"])
@auth.admin_required
def quest_test_start(quest_id: int):
    from .diagnostics import capture_status, set_capture

    back = redirect(url_for("dashboard.quest", quest_id=quest_id) + "#test")
    running = current_test()
    if running:
        flash(f"A test of quest {running['quest']} is still running. Finish or cancel it first.", "err")
        return back

    form = request.form
    ok, message, ch = prepare_character(quest_id, form)
    if not ok:
        flash(message, "err")
        return back

    capture_was_on = capture_status()["enabled"]
    capture_message = "Packet capture was already on."
    if not capture_was_on:
        saved, capture_message = set_capture(True, restart=form.get("restart") == "on")
        if not saved:
            flash(f"{message} But the test didn't start: {capture_message}", "err")
            return back

    state = form.get("state", "available")
    sequence = form.get("sequence") or "1"
    start = {"available": "ready to accept", "active": f"sequence {sequence}", "completed": "completed",
             "reset": "never started"}.get(state, state)
    test = {
        "quest": quest_id,
        "name": _quest_name(quest_id),
        "character": getattr(ch, "name", ""),
        "content_id": int(form.get("content_id", "0") or 0),
        "from": start,
        "sequence": int(sequence) if state == "active" and str(sequence).isdigit() else None,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "started_at": time.time(),
        "capture_was_on": capture_was_on,
    }
    _save(test)
    flash(f"Testing quest {quest_id}. {message} {capture_message} Log in, play it, then come back and finish "
          f"the test to send what happened.", "ok")
    return back


@dashboard.route("/quests/test/finish", methods=["POST"])
@auth.admin_required
def quest_test_finish():
    from .diagnostics import GitHubError, _github, _set, build_bundle, set_capture

    test = current_test()
    if not test:
        flash("No quest test is running.", "err")
        return redirect(url_for("dashboard.quests"))
    back = redirect(url_for("dashboard.quest", quest_id=test["quest"]) + "#test")
    note = (request.form.get("note") or "").strip()[:1800]
    title = f"Quest test {test['quest']} {test.get('name', '')}".strip()
    full_note = f"{title}: {note}" if note else title
    minutes = test["minutes"] + 2
    meta = {k: test[k] for k in ("quest", "name", "character", "content_id", "from", "sequence", "started")
            if k in test}
    try:
        github = _github()
        folder, files = build_bundle(full_note, [test["content_id"]] if test.get("content_id") else [], minutes,
                                     quest_test=meta)
        url = github.commit_files(files, f"Diagnostics: {full_note[:60]}")
    except GitHubError as exc:
        flash(f"Not sent (the test is still running): {exc}", "err")
        return back
    except Exception as exc:                            # noqa: BLE001
        flash(f"Not sent (the test is still running): {exc}", "err")
        return back

    size = sum(len(v) for v in files.values())
    _set("last_sent", f"{time.strftime('%Y-%m-%d %H:%M')} - {folder} ({len(files)} files, {size // 1024} KiB)")
    _set("last_url", url)
    _save(None)
    message = f"Sent {folder} ({len(files)} files, {size // 1024} KiB)."
    if request.form.get("capture_off") == "on" and not test.get("capture_was_on"):
        _ok, capture_message = set_capture(False, restart=request.form.get("restart") == "on")
        message += " " + capture_message
    flash(message, "ok")
    return back


@dashboard.route("/quests/test/cancel", methods=["POST"])
@auth.admin_required
def quest_test_cancel():
    from .diagnostics import set_capture

    test = current_test()
    _save(None)
    if not test:
        return redirect(url_for("dashboard.quests"))
    message = f"Stopped testing quest {test['quest']}; nothing was sent."
    if request.form.get("capture_off") == "on" and not test.get("capture_was_on"):
        _ok, capture_message = set_capture(False, restart=False)
        message += " " + capture_message
    flash(message, "info")
    return redirect(url_for("dashboard.quest", quest_id=test["quest"]) + "#test")
