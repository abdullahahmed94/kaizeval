"""Background job handlers. Importing this module registers them with the queue."""

from __future__ import annotations

from typing import Any

from . import audit
from .db import session_factory
from .gateway import GatewayError
from .jobs import ORPHAN_HOOKS, JobContext, JobFailed, handler
from .models import Dataset, EvaluationRun, Job, OptimizationRun, utcnow
from .services import datasets, diagnostics, optimization, specs
from .services.datasets import HoldoutAccess
from .services.evaluation import execute_run


@handler("specification.build")
def build_specification(ctx: JobContext) -> dict[str, Any]:
    db = session_factory()()
    try:
        spec = specs.build_specification(
            db, workspace_id=ctx.workspace_id, agent_id=ctx.payload.get("agent_id"),
            requirements_text=ctx.payload.get("requirements_text"),
            requirement_document_ids=ctx.payload.get("requirement_document_ids") or [],
            infer=bool(ctx.payload.get("infer", True)), user_id=ctx.created_by, run_id=ctx.job_id)
        db.commit()
        return {"specification_id": spec.id, "synthetic": spec.synthetic}
    except specs.SpecError as exc:
        db.rollback()
        raise JobFailed(str(exc)) from exc
    finally:
        db.close()


@handler("dataset.generate")
def generate_dataset(ctx: JobContext) -> dict[str, Any]:
    db = session_factory()()
    try:
        dataset = db.get(Dataset, ctx.payload["dataset_id"])
        if dataset is None:
            raise JobFailed("dataset not found")
        try:
            datasets.generate_dataset(db, dataset, ctx.payload.get("params") or {})
        except (datasets.DatasetError, GatewayError) as exc:
            db.rollback()
            dataset = db.get(Dataset, ctx.payload["dataset_id"])
            assert dataset is not None
            dataset.status = "failed"
            dataset.notes = [*(dataset.notes or []), f"Generation failed: {exc}"]
            db.commit()
            raise JobFailed(str(exc)) from exc
        audit.record(db, workspace_id=ctx.workspace_id, user_id=ctx.created_by, action="dataset.generated",
                     subject_type="dataset", subject_id=dataset.id,
                     detail={"synthetic": dataset.synthetic, "counts": datasets.split_counts(db, dataset.id)})
        db.commit()
        return {"dataset_id": dataset.id, "counts": datasets.split_counts(db, dataset.id), "synthetic": dataset.synthetic}
    finally:
        db.close()


@handler("dataset.rotate_holdout")
def rotate_holdout(ctx: JobContext) -> dict[str, Any]:
    db = session_factory()()
    try:
        dataset = db.get(Dataset, ctx.payload["dataset_id"])
        if dataset is None:
            raise JobFailed("dataset not found")
        try:
            result = datasets.rotate_holdout(db, dataset, ctx.payload.get("params") or {}, ctx.created_by)
        except (datasets.DatasetError, GatewayError) as exc:
            db.rollback()
            raise JobFailed(str(exc)) from exc
        db.commit()
        return result
    finally:
        db.close()


@handler("evaluation.run")
def run_evaluation(ctx: JobContext) -> dict[str, Any]:
    db = session_factory()()
    try:
        run = db.get(EvaluationRun, ctx.payload["run_id"])
        if run is None:
            raise JobFailed("evaluation run not found")
        access = None
        if run.split == "holdout":
            # A manually launched holdout run is an audited, privileged action that spends the holdout.
            access = HoldoutAccess(purpose="holdout_validation", actor=f"user:{ctx.created_by}")
        run = execute_run(db, run.id, ctx=ctx, access=access)
        if run.status == "completed" and run.split == "regression":
            diagnostics.diagnose(db, run, use_model=True)
            diagnostics.recommend(db, run)
            db.commit()
        if run.status == "completed" and run.split == "holdout":
            dataset = db.get(Dataset, run.dataset_id)
            assert dataset is not None
            dataset.holdout_exposed = True
            db.commit()
        if run.status == "failed":
            raise JobFailed(run.error or "evaluation failed")
        return {"run_id": run.id, "status": run.status, "overall": (run.summary or {}).get("overall")}
    finally:
        db.close()


@handler("optimization.run")
def run_optimization(ctx: JobContext) -> dict[str, Any]:
    db = session_factory()()
    try:
        run = optimization.advance(db, ctx.payload["optimization_run_id"], ctx)
        if run.status == "failed":
            raise JobFailed(run.error or "optimization failed")
        return {"optimization_run_id": run.id, "status": run.status, "stop_reason": run.stop_reason}
    except optimization.OptimizationError as exc:
        db.rollback()
        raise JobFailed(str(exc)) from exc
    finally:
        db.close()


def _mark_orphaned_run(db: Any, job: Job) -> None:
    """When a worker dies mid-job, the run it was executing must not stay 'running' forever."""
    message = "the worker stopped while this run was in progress; start it again"
    payload = job.payload or {}
    if job.kind == "evaluation.run":
        run = db.get(EvaluationRun, payload.get("run_id"))
        if run is not None and run.status in ("queued", "running"):
            run.status, run.error, run.finished_at = "failed", message, utcnow()
    elif job.kind == "optimization.run":
        optimization_run = db.get(OptimizationRun, payload.get("optimization_run_id"))
        if optimization_run is not None and optimization_run.status in ("queued", "running"):
            optimization_run.status, optimization_run.error, optimization_run.finished_at = "failed", message, utcnow()
            for run in db.query(EvaluationRun).filter(EvaluationRun.optimization_run_id == optimization_run.id,
                                                      EvaluationRun.status.in_(("queued", "running"))):
                run.status, run.error, run.finished_at = "failed", message, utcnow()
    elif job.kind == "dataset.generate":
        dataset = db.get(Dataset, payload.get("dataset_id"))
        if dataset is not None and dataset.status == "generating":
            dataset.status = "failed"
            dataset.notes = [*(dataset.notes or []), "Generation was interrupted by a worker restart."]


if _mark_orphaned_run not in ORPHAN_HOOKS:
    ORPHAN_HOOKS.append(_mark_orphaned_run)
