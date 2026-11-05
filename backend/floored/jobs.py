"""Background jobs on a thread pool, with their state kept in the database."""

from __future__ import annotations

import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import Job

log = logging.getLogger(__name__)



class Progress:
    """What a job handler reports through: progress, a message, and live numbers.

    ``progress(0.4, "Reading pages", {"pages_read": 12})`` updates the job;
    ``progress.stop_requested`` says whether the user asked it to stop.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._stop = False
        self._checked = 0.0

    def __call__(self, fraction: float, message: str, detail: dict[str, Any] | None = None) -> None:
        fields: dict[str, Any] = {"progress": round(max(0.0, min(1.0, fraction)), 3), "message": message[:500]}
        if detail is not None:
            fields["detail"] = detail
        _update(self.job_id, **fields)

    @property
    def stop_requested(self) -> bool:
        if not self._stop and time.monotonic() - self._checked > 1.0:
            self._checked = time.monotonic()
            with session_scope() as s:
                job = s.get(Job, self.job_id)
                self._stop = bool(job and job.stop_requested)
        return self._stop


Handler = Callable[[dict[str, Any], Progress], dict[str, Any] | None]

_handlers: dict[str, Handler] = {}
_executor: ThreadPoolExecutor | None = None
# Run jobs inline (tests, CLI) instead of on the pool.
synchronous = False


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return register


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=get_settings().workers, thread_name_prefix="floored-job")
    return _executor


def recover() -> None:
    """Jobs that were running when the server stopped will never finish; mark them."""
    with session_scope() as s:
        for job in s.scalars(select(Job).where(Job.status.in_(("queued", "running")))):
            job.status = "failed"
            job.message = "interrupted by a server restart"


def submit(kind: str, payload: dict[str, Any] | None = None, *, label: str | None = None) -> str:
    """Queue a job. ``label`` is what the activity list calls it ("Crawling Cal Poly")."""
    if kind not in _handlers:
        raise KeyError(f"unknown job kind {kind!r}")
    payload = dict(payload or {})
    if label:
        payload["label"] = label
    with session_scope() as s:
        job = Job(kind=kind, payload=payload)
        s.add(job)
        s.flush()
        job_id = job.id
    if synchronous:
        _run(job_id)
    else:
        _pool().submit(_run, job_id)
    return job_id


def request_stop(job_id: str) -> bool:
    """Ask a job to stop. A queued job is cancelled; a running one stops at its next check."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job.stop_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.message = "cancelled before it started"
            job.finished_at = datetime.now(timezone.utc)
        return True


def _update(job_id: str, **fields: Any) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            for k, v in fields.items():
                setattr(job, k, v)


def _run(job_id: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job.status != "queued":  # cancelled while it waited
            return
        kind, payload = job.kind, dict(job.payload or {})
        job.status, job.started_at = "running", datetime.now(timezone.utc)
    progress = Progress(job_id)

    try:
        # A job that was asked to stop and wound down on its own still finished:
        # it says so in its result.
        result = _handlers[kind](payload, progress) or {}
        _update(job_id, status="done", progress=1.0, result=result, finished_at=datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - job failures are reported, not raised
        log.exception("job %s (%s) failed", job_id, kind)
        _update(
            job_id,
            status="failed",
            message=f"{type(exc).__name__}: {exc}"[:500],
            result={"traceback": traceback.format_exc()[-4000:]},
            finished_at=datetime.now(timezone.utc),
        )
