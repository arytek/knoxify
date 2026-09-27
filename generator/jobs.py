"""One background export at a time, with observable progress and cancellation."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


class Cancelled(Exception):
    pass


class Busy(Exception):
    def __init__(self, job_id: str):
        self.job_id = job_id


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "running"
    stage: str = "starting"
    message: str = "Preparing export"
    progress: float = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    result: dict | None = None
    error: str | None = None
    fetch: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def update(self, progress: float, message: str, stage: str, **details):
        self.check()
        with self.lock:
            self.progress = max(self.progress, min(99, progress))
            self.message, self.stage = message, stage
            if "fetch" in details:
                self.fetch = details["fetch"]
            self.details.update({key: value for key, value in details.items() if key != "fetch"})

    def cancel(self):
        with self.lock:
            if self.state in {"running", "cancelling"}:
                self.cancel_event.set()
                self.state = "cancelling"
                self.message = "Cancelling after the current operation finishes"

    def snapshot(self):
        with self.lock:
            return {
                "id": self.id, "state": self.state, "stage": self.stage,
                "message": self.message, "progress": round(self.progress, 1),
                "elapsedSeconds": round((self.finished or time.monotonic()) - self.started, 1),
                "fetch": self.fetch.copy(), "result": self.result, "error": self.error,
                "details": self.details.copy(),
            }


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def start(self, work: Callable[[Job], dict]) -> Job:
        with self.lock:
            for job in self.jobs.values():
                if not job.done.is_set():
                    raise Busy(job.id)
            # Keep recent results for browser reconnection without unbounded RAM growth.
            while len(self.jobs) >= 100:
                del self.jobs[next(iter(self.jobs))]
            job = Job()
            self.jobs[job.id] = job
            threading.Thread(target=self._run, args=(job, work), daemon=True).start()
            return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    @staticmethod
    def _run(job, work):
        try:
            result = work(job)
            with job.lock:
                if job.cancel_event.is_set():
                    raise Cancelled()
                job.result = result
                job.state, job.stage, job.message = "complete", "complete", "Map ready"
                job.progress = 100
        except Cancelled:
            with job.lock:
                job.state, job.message = "cancelled", "Generation cancelled"
        except Exception as exc:
            with job.lock:
                job.state, job.message, job.error = "failed", "Generation failed", str(exc)
        finally:
            with job.lock:
                job.finished = time.monotonic()
            job.done.set()
