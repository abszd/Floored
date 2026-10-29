"""Background jobs on a thread pool, with their state kept in the database."""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import Job

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
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


def submit(kind: str, payload: dict[str, Any] | None = None) -> str:
    if kind not in _handlers:
        raise KeyError(f"unknown job kind {kind!r}")
    with session_scope() as s:
        job = Job(kind=kind, payload=payload or {})
        s.add(job)
        s.flush()
        job_id = job.id
    if synchronous:
        _run(job_id)
    else:
        _pool().submit(_run, job_id)
    return job_id


def _update(job_id: str, **fields: Any) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            for k, v in fields.items():
                setattr(job, k, v)


def _run(job_id: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        kind, payload = job.kind, dict(job.payload or {})
    _update(job_id, status="running", started_at=datetime.now(timezone.utc))

    def progress(fraction: float, message: str) -> None:
        _update(job_id, progress=round(max(0.0, min(1.0, fraction)), 3), message=message[:500])

    try:
        result = _handlers[kind](payload, progress)
        _update(job_id, status="done", progress=1.0, result=result or {}, finished_at=datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - job failures are reported, not raised
        log.exception("job %s (%s) failed", job_id, kind)
        _update(
            job_id,
            status="failed",
            message=f"{type(exc).__name__}: {exc}"[:500],
            result={"traceback": traceback.format_exc()[-4000:]},
            finished_at=datetime.now(timezone.utc),
        )
