"""A small database-backed job queue.

Jobs are rows. A worker claims a queued row with a conditional UPDATE, so several workers can
share one database safely. This keeps the default installation free of a separate broker and
makes job state transactional with the data it produces.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .models import Job, utcnow
from .security import redact

log = logging.getLogger("kaizeval.jobs")

TERMINAL = {"completed", "failed", "cancelled"}


class JobCancelled(Exception):
    pass


class ReviewRequired(Exception):
    """The job paused and is waiting for a human decision."""

    def __init__(self, detail: dict[str, Any] | None = None):
        super().__init__("review required")
        self.detail = detail or {}


class RetryableJobError(Exception):
    pass


class JobFailed(Exception):
    """An expected, already-explained failure (no traceback is logged)."""


class JobContext:
    def __init__(self, job_id: str, workspace_id: str, payload: dict[str, Any], created_by: str | None):
        self.job_id = job_id
        self.workspace_id = workspace_id
        self.payload = payload
        self.created_by = created_by

    def progress(self, **fields: Any) -> None:
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is not None:
                job.progress = {**(job.progress or {}), **fields}

    def cancelled(self) -> bool:
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            return bool(job and job.cancel_requested)

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise JobCancelled()


Handler = Callable[[JobContext], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}
# Called with (session, job) for every job that is reclaimed after a worker died.
ORPHAN_HOOKS: list[Callable[[Session, Job], None]] = []


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


def enqueue(db: Session, *, workspace_id: str, kind: str, payload: dict[str, Any],
            created_by: str | None = None, idempotency_key: str | None = None) -> Job:
    """Create a job. With an idempotency key, a repeated request returns the original job."""
    if idempotency_key:
        existing = db.scalar(
            select(Job).where(Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
    job = Job(workspace_id=workspace_id, kind=kind, payload=payload, created_by=created_by,
              idempotency_key=idempotency_key)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Job).where(Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        return existing
    if get_settings().worker_mode == "eager":
        run_job(job.id)
        db.refresh(job)
    return job


def resume(db: Session, job_id: str) -> None:
    """Put a job that was waiting for review back on the queue."""
    job = db.get(Job, job_id)
    if job is None or job.status != "review_required":
        return
    job.status = "queued"
    db.commit()
    if get_settings().worker_mode == "eager":
        run_job(job.id)


def request_cancel(db: Session, job: Job) -> None:
    if job.status in TERMINAL:
        return
    job.cancel_requested = True
    if job.status in ("queued", "review_required"):
        job.status = "cancelled"
        job.finished_at = utcnow()
    db.commit()


def _claim(job_id: str) -> bool:
    with session_scope() as db:
        result = db.execute(
            update(Job).where(Job.id == job_id, Job.status == "queued")
            .values(status="running", started_at=utcnow(), attempts=Job.attempts + 1)
        )
        return result.rowcount == 1  # type: ignore[attr-defined]


def run_job(job_id: str) -> None:
    """Claim and execute one job. Never raises; the outcome is written to the row."""
    if not _claim(job_id):
        return
    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        ctx = JobContext(job.id, job.workspace_id, dict(job.payload or {}), job.created_by)
        kind, attempts = job.kind, job.attempts
    fn = HANDLERS.get(kind)
    status, result, error = "completed", None, None
    try:
        if fn is None:
            raise RuntimeError(f"no handler registered for job kind '{kind}'")
        if ctx.cancelled():
            raise JobCancelled()
        result = fn(ctx)
    except JobCancelled:
        status = "cancelled"
    except ReviewRequired as exc:
        status, result = "review_required", exc.detail
    except JobFailed as exc:
        status, error = "failed", redact(str(exc))
    except RetryableJobError as exc:
        if attempts < get_settings().job_max_attempts:
            status, error = "queued", redact(str(exc))
        else:
            status, error = "failed", redact(f"gave up after {attempts} attempts: {exc}")
    except Exception as exc:  # noqa: BLE001 - job boundary
        status, error = "failed", redact(f"{type(exc).__name__}: {exc}")
        log.error("job %s (%s) failed: %s", job_id, kind, redact(traceback.format_exc()))
    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = status
        job.error = error
        if result is not None:
            job.result = result
        if status in TERMINAL:
            job.finished_at = utcnow()


def drain(max_jobs: int = 1000) -> int:
    """Run queued jobs until none are left. Returns how many were executed."""
    done = 0
    while done < max_jobs:
        with session_scope() as db:
            job_id = db.scalar(select(Job.id).where(Job.status == "queued").order_by(Job.created_at).limit(1))
        if job_id is None:
            break
        run_job(job_id)
        done += 1
    return done


class Worker:
    """Polls the queue on a background thread (inline mode) or in the foreground (`kaizeval worker`)."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def recover_orphans(self, *, only_stale: bool = False) -> int:
        """Fail jobs left 'running' by a process that died, so they do not hang forever.

        Inline mode has a single worker, so every running job found at start-up is orphaned. With
        external workers (``only_stale``) a job is only reclaimed after ``job_stale_minutes``.
        """
        cutoff = utcnow() - timedelta(minutes=get_settings().job_stale_minutes)
        recovered: list[Job] = []
        with session_scope() as db:
            for job in db.scalars(select(Job).where(Job.status == "running")):
                started = job.started_at
                if only_stale and started is not None:
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if started > cutoff:
                        continue
                job.status = "failed"
                job.error = "worker stopped while the job was running"
                job.finished_at = utcnow()
                recovered.append(job)
            db.flush()
            for job in recovered:
                for hook in ORPHAN_HOOKS:
                    hook(db, job)
        return len(recovered)

    def run_forever(self) -> None:
        poll = get_settings().worker_poll_seconds
        while not self._stop.is_set():
            try:
                if drain(max_jobs=10) == 0:
                    self._stop.wait(poll)
            except Exception:  # noqa: BLE001
                log.error("worker loop error: %s", redact(traceback.format_exc()))
                time.sleep(poll)

    def start(self) -> None:
        self.recover_orphans()
        self._thread = threading.Thread(target=self.run_forever, name="kaizeval-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
