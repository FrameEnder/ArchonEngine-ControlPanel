"""Background jobs.

Downloading a build, building an image and switching versions take minutes, so
they cannot run inside a request. Each job runs on its own thread, reports its
steps, and appends to a log the browser polls. Only one job runs at a time -
these operations all touch the same container, image and databases, and letting
two overlap is how you corrupt a database.

Deliberately in-process and in-memory: state resets if the app restarts, which is
correct here. A half-finished update should not look resumable when the process
that was running it is gone.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field


class SkipStep(Exception):
    """Raised by a step that decides it has nothing to do."""


@dataclass
class Step:
    label: str
    state: str = "pending"          # pending | running | ok | skip | fail
    detail: str = ""


@dataclass
class Job:
    id: str
    title: str
    steps: list = field(default_factory=list)
    log: list = field(default_factory=list)
    state: str = "running"          # running | done | failed
    summary: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    context: dict = field(default_factory=dict)

    def say(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self.log.append(line)
        del self.log[:-4000]

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "state": self.state,
            "summary": self.summary,
            "elapsed": int((self.finished or time.time()) - self.started),
            "steps": [{"label": s.label, "state": s.state, "detail": s.detail}
                      for s in self.steps],
            "log": "\n".join(self.log),
        }


class JobRunner:
    """Runs jobs on a worker thread INSIDE a Flask application context.

    Without the context, any step calling current_app - which every helper that
    reaches settings or podman does - dies with "Working outside of application
    context". The context has to be pushed on the worker thread itself; it does
    not inherit from the request that started the job.
    """

    def __init__(self, app=None):
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.history: list = []
        self.app = app

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.state == "running"

    def get(self, job_id: str) -> Job | None:
        if self.current and self.current.id == job_id:
            return self.current
        return next((j for j in self.history if j.id == job_id), None)

    def start(self, title: str, steps: list) -> Job:
        """steps: [(label, callable(job) -> str|None)]. Raises if one is running."""
        with self._lock:
            if self.busy:
                raise RuntimeError(
                    f"'{self.current.title}' is still running. Wait for it to finish - "
                    f"these operations share the container and databases.")
            job = Job(id=uuid.uuid4().hex[:12], title=title,
                      steps=[Step(label) for label, _fn in steps])
            self.current = job

        def run():
            context = self.app.app_context() if self.app is not None else None
            if context is not None:
                context.push()
            try:
                for index, (_label, fn) in enumerate(steps):
                    step = job.steps[index]
                    step.state = "running"
                    try:
                        detail = fn(job)
                    except SkipStep as skip:
                        step.state, step.detail = "skip", str(skip)
                        job.say(f"- skipped: {skip}")
                        continue
                    except Exception as exc:                # noqa: BLE001
                        step.state, step.detail = "fail", str(exc)
                        job.say(traceback.format_exc())
                        job.state = "failed"
                        job.summary = f"Stopped at step {index + 1}: {exc}"
                        return
                    step.state, step.detail = "ok", detail or ""
                    if detail:
                        job.say(f"- {step.label}: {detail}")
                    if "finish" in job.context:
                        for later in job.steps[index + 1:]:
                            later.state, later.detail = "skip", "not needed"
                        job.state = "done"
                        job.summary = job.context["finish"]
                        return
                job.state = "done"
                job.summary = job.context.get("summary", "Completed.")
            finally:
                job.finished = time.time()
                with self._lock:
                    self.history.insert(0, job)
                    del self.history[8:]
                if context is not None:
                    context.pop()

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job
