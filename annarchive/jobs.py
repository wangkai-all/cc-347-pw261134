"""In-process background job runner.

One daemon thread, one job at a time — sufficient for this archive and
deliberately simple: it removes any race between two concurrent re-fetches
of the same announcement.  Each job opens its own SQLite connection.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db
from .config import Config


@dataclass
class Job:
    id: str
    kind: str
    target: str
    state: str = "queued"            # queued | running | done | error
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = field(default_factory=db.utcnow)
    finished_at: str | None = None


class JobRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jobs: dict[str, Job] = {}
        self._cv = threading.Condition()
        self._thread = threading.Thread(
            target=self._loop, name="annarchive-jobs", daemon=True)
        self._closed = False

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def submit(self, kind: str, target: str,
               func: Callable[[], dict[str, Any]]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, target=target)
        with self._cv:
            self._jobs[job.id] = job
            self._cv.notify_all()
        # store the callable alongside; lock only guards bookkeeping
        job.__dict__["_func"] = func
        return job

    def get(self, job_id: str) -> Job | None:
        with self._cv:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[Job]:
        with self._cv:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def _loop(self) -> None:
        while True:
            with self._cv:
                pending = [j for j in self._jobs.values()
                           if j.state == "queued"]
                if not pending:
                    self._cv.wait(timeout=5)
                    if self._closed:
                        return
                    continue
                job = sorted(pending, key=lambda j: j.created_at)[0]
                job.state = "running"
                func = job.__dict__.pop("_func", None)
            if func is None:
                continue
            try:
                job.result = func() or {}
                job.state = "done"
            except Exception as exc:  # noqa: BLE001 - surfaced via UI/API
                job.state = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.result = {"traceback": traceback.format_exc()}
            finally:
                job.finished_at = db.utcnow()
                with self._cv:
                    self._cv.notify_all()
