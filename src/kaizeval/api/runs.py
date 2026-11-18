"""Evaluation runs, recommendations, optimization, candidates, experiments, reports, audit and jobs."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import audit, jobs
from ..connectors import render_diff
from ..models import (
    Agent,
    Approval,
    AuditEvent,
    Candidate,
    CandidatePatch,
    CaseResult,
    Dataset,
    EvaluationRun,
    Experiment,
    Finding,
    Job,
    OptimizationRun,
    Recommendation,
    TestCase,
    utcnow,
)
from ..services import diagnostics, optimization, reporting
from ..services.evaluation import EvaluationError, create_run
from ..services.stats import compare_runs
from .deps import Ctx, fail, idempotency_key, job_dict, page, scoped, ser, workspace_ctx

router = APIRouter(prefix="/workspaces/{workspace_id}")


# --- evaluation runs ----------------------------------------------------------------------


class RunCreate(BaseModel):
    agent_id: str
    dataset_id: str
    # single = 1 reviewer, dual = 2, panel = config.reviewers (2-7). config.testers (1-8) sets parallel testers.
    mode: Literal["deterministic", "single", "dual", "panel"] = "single"
    split: Literal["regression", "holdout"] = "regression"
    agent_version: str = Field(default="baseline", max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)


def _run_dict(run: EvaluationRun) -> dict[str, Any]:
    return ser(run)


@router.post("/evaluation-runs", status_code=202)
def start_run(body: RunCreate, request: Request, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    key = idempotency_key(request)
    if key:
        existing = ctx.db.scalar(select(Job).where(Job.workspace_id == ctx.ws, Job.idempotency_key == key))
        if existing is not None:
            return {"job": job_dict(existing), "run": _run_dict(scoped(ctx, EvaluationRun, existing.payload["run_id"]))}
    if body.split == "holdout":
        ctx.require("owner")  # spending the sealed holdout is an owner decision
    agent = scoped(ctx, Agent, body.agent_id)
    if body.agent_version != "baseline":
        candidate = ctx.db.scalar(select(Candidate).where(Candidate.agent_id == agent.id,
                                                          Candidate.external_id == body.agent_version))
        if candidate is None:
            raise fail(404, "UNKNOWN_VERSION", "agent_version must be 'baseline' or a candidate of this agent")
    try:
        run = create_run(ctx.db, workspace_id=ctx.ws, agent_id=body.agent_id, dataset_id=body.dataset_id,
                         split=body.split, mode=body.mode, agent_version=body.agent_version, config=body.config,
                         kind="holdout" if body.split == "holdout" else ("baseline" if body.agent_version == "baseline" else "candidate"),
                         user_id=ctx.user.id)
    except EvaluationError as exc:
        raise fail(409, "CANNOT_START", str(exc)) from exc
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="evaluation.started",
                 subject_type="evaluation_run", subject_id=run.id,
                 detail={"split": body.split, "mode": body.mode, "agent_version": body.agent_version})
    run_id = run.id
    job = jobs.enqueue(ctx.db, workspace_id=ctx.ws, kind="evaluation.run", payload={"run_id": run_id},
                       created_by=ctx.user.id, idempotency_key=key)
    ctx.db.expire_all()
    run = scoped(ctx, EvaluationRun, run_id)
    if run.job_id is None:
        run.job_id = job.id
        ctx.db.commit()
    return {"job": job_dict(job), "run": _run_dict(run)}


@router.get("/evaluation-runs")
def list_runs(agent_id: str | None = None, dataset_id: str | None = None, standalone: bool = False,
              limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(EvaluationRun).where(EvaluationRun.workspace_id == ctx.ws).order_by(EvaluationRun.created_at.desc())
    if agent_id:
        query = query.where(EvaluationRun.agent_id == agent_id)
    if dataset_id:
        query = query.where(EvaluationRun.dataset_id == dataset_id)
    if standalone:
        query = query.where(EvaluationRun.optimization_run_id.is_(None))
    return page(ctx.db, query, limit=limit, offset=offset, transform=_run_dict)


@router.get("/evaluation-runs/{run_id}")
def get_run(run_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    run = scoped(ctx, EvaluationRun, run_id)
    job = ctx.db.get(Job, run.job_id) if run.job_id else None
    return {**_run_dict(run), "job": job_dict(job) if job else None}


@router.get("/evaluation-runs/{run_id}/results")
def run_results(run_id: str, verdict: str | None = None, acknowledge: bool = False, limit: int = 50, offset: int = 0,
                ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    """Per-case results with evidence. Holdout case content is restricted to reviewers and audited."""
    run = scoped(ctx, EvaluationRun, run_id)
    if run.split == "holdout":
        ctx.require("reviewer")
        if not acknowledge:
            raise fail(403, "HOLDOUT_SEALED", "per-case holdout results are sealed. Pass acknowledge=true to open "
                       "them; this is audited and the holdout will need rotating before it is used again.")
        dataset = ctx.db.get(Dataset, run.dataset_id)
        already_open = ctx.db.scalar(select(AuditEvent.id).where(
            AuditEvent.workspace_id == ctx.ws, AuditEvent.action == "holdout.results_viewed",
            AuditEvent.subject_id == run.id, AuditEvent.user_id == ctx.user.id).limit(1))
        if dataset is not None and not dataset.holdout_exposed:
            dataset.holdout_exposed = True
        if not already_open:
            # Recorded once per person and run; paging through the same results adds nothing new.
            audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="holdout.results_viewed",
                         subject_type="evaluation_run", subject_id=run.id)
        ctx.db.commit()
    query = select(CaseResult).where(CaseResult.run_id == run.id).order_by(CaseResult.created_at, CaseResult.id)
    if verdict:
        query = query.where(CaseResult.verdict == verdict)
    result = page(ctx.db, query, limit=limit, offset=offset)
    case_ids = [item["case_id"] for item in result["items"]]
    cases = {c.id: c for c in ctx.db.scalars(select(TestCase).where(TestCase.id.in_(case_ids)))} if case_ids else {}
    for item in result["items"]:
        case = cases.get(item["case_id"])
        if case is not None:
            item["case"] = {"category": case.category, "severity": case.severity, "language": case.language,
                            "turns": case.turns, "expected": case.expected, "answer_status": case.answer_status,
                            "evidence_refs": case.evidence_refs, "stale": case.stale}
    return result


@router.get("/evaluation-runs/{run_id}/findings")
def run_findings(run_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    run = scoped(ctx, EvaluationRun, run_id)
    query = select(Finding).where(Finding.run_id == run.id).order_by(Finding.category, Finding.created_at)
    return page(ctx.db, query, limit=limit, offset=offset)


@router.get("/compare")
def compare(baseline_run_id: str, candidate_run_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    """Paired comparison of two completed runs over the same split."""
    a, b = scoped(ctx, EvaluationRun, baseline_run_id), scoped(ctx, EvaluationRun, candidate_run_id)
    if a.split != b.split or a.dataset_id != b.dataset_id:
        raise fail(409, "NOT_COMPARABLE", "runs must use the same dataset and the same split")
    if a.status != "completed" or b.status != "completed":
        raise fail(409, "NOT_COMPARABLE", "both runs must be completed")
    if a.mode != b.mode:
        # Scores from different evaluation modes use different dimensions and are not on the same scale.
        raise fail(409, "NOT_COMPARABLE", f"runs were scored with different evaluation modes ('{a.mode}' and '{b.mode}')")
    return compare_runs(ctx.db, a, b, include_cases=a.split == "regression")


# --- recommendations ----------------------------------------------------------------------


class RecommendationState(BaseModel):
    state: Literal["proposed", "approved", "deferred", "rejected", "applied"]
    reason: str | None = Field(default=None, max_length=2000)


class RecommendationsBuild(BaseModel):
    run_id: str


@router.post("/recommendations")
def build_recommendations(body: RecommendationsBuild, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    run = scoped(ctx, EvaluationRun, body.run_id)
    if run.status != "completed" or run.split != "regression":
        raise fail(409, "NOT_AVAILABLE", "recommendations are produced for completed regression runs only")
    diagnostics.diagnose(ctx.db, run, use_model=True)
    diagnostics.recommend(ctx.db, run)
    ctx.db.commit()
    return list_recommendations(run_id=run.id, ctx=ctx)


@router.get("/recommendations")
def list_recommendations(run_id: str | None = None, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(Recommendation).where(Recommendation.workspace_id == ctx.ws).order_by(Recommendation.priority.desc())
    if run_id:
        query = query.where(Recommendation.run_id == run_id)
    return {"items": [ser(r) for r in ctx.db.scalars(query.limit(200))]}


@router.patch("/recommendations/{recommendation_id}")
def set_recommendation_state(recommendation_id: str, body: RecommendationState,
                             ctx: Ctx = Depends(workspace_ctx("reviewer"))) -> dict[str, Any]:
    rec = scoped(ctx, Recommendation, recommendation_id)
    if body.state == "rejected" and not (body.reason or "").strip():
        raise fail(422, "REASON_REQUIRED", "a rejection needs a reason")
    rec.state, rec.state_reason, rec.state_by = body.state, body.reason, ctx.user.id
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action=f"recommendation.{body.state}",
                 subject_type="recommendation", subject_id=rec.id, detail={"reason": body.reason})
    ctx.db.commit()
    return ser(rec)


# --- optimization -------------------------------------------------------------------------


class OptimizationCreate(BaseModel):
    agent_id: str
    dataset_id: str
    mode: Literal["human_guided", "autonomous"] = "human_guided"
    policy: dict[str, Any] = Field(default_factory=dict)


class OptimizationContinue(BaseModel):
    # Optional changes: max_rounds, stagnation_rounds, target_score, min_improvement, limits, run_holdout
    policy: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


class Note(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


def _opt_dict(ctx: Ctx, run: OptimizationRun, *, detail: bool = False) -> dict[str, Any]:
    out = ser(run)
    if detail:
        patches = list(ctx.db.scalars(select(CandidatePatch).where(CandidatePatch.candidate_id == run.candidate_id)
                                      .order_by(CandidatePatch.round))) if run.candidate_id else []
        experiments = list(ctx.db.scalars(select(Experiment).where(Experiment.optimization_run_id == run.id)
                                          .order_by(Experiment.created_at)))
        job = ctx.db.get(Job, run.job_id) if run.job_id else None
        out.update({"patches": [ser(p) for p in patches],
                    "experiments": [{"id": e.id, "round": e.round, "kind": e.kind, "decision": e.decision,
                                     "overall": e.comparison.get("overall"), "critical_failures": e.comparison.get("critical_failures")}
                                    for e in experiments],
                    "job": job_dict(job) if job else None})
    return out


@router.post("/optimization-runs", status_code=202)
def start_optimization(body: OptimizationCreate, request: Request, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Start a bounded optimization of an isolated candidate. Refused when the connector cannot patch safely."""
    key = idempotency_key(request)
    if key:
        existing = ctx.db.scalar(select(Job).where(Job.workspace_id == ctx.ws, Job.idempotency_key == key))
        if existing is not None:
            return {"job": job_dict(existing),
                    "optimization_run": _opt_dict(ctx, scoped(ctx, OptimizationRun, existing.payload["optimization_run_id"]))}
    if body.mode == "autonomous":
        ctx.require("owner")
    try:
        run = optimization.create_optimization_run(ctx.db, workspace_id=ctx.ws, agent_id=body.agent_id,
                                                   dataset_id=body.dataset_id, mode=body.mode, policy=body.policy,
                                                   user_id=ctx.user.id)
    except optimization.OptimizationError as exc:
        status = 404 if exc.code == "NOT_FOUND" else 409
        raise fail(status, exc.code, exc.message) from exc
    run_id = run.id
    job = jobs.enqueue(ctx.db, workspace_id=ctx.ws, kind="optimization.run", payload={"optimization_run_id": run_id},
                       created_by=ctx.user.id, idempotency_key=key)
    ctx.db.expire_all()
    run = scoped(ctx, OptimizationRun, run_id)
    if run.job_id is None:
        run.job_id = job.id
        ctx.db.commit()
    ctx.db.refresh(job)
    return {"job": job_dict(job), "optimization_run": _opt_dict(ctx, run)}


@router.post("/optimization-runs/{run_id}/continue", status_code=202)
def continue_optimization(run_id: str, body: OptimizationContinue | None = None,
                          ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Keep improving from the candidate of a stopped run instead of releasing it. Both versions are measured again first."""
    run = scoped(ctx, OptimizationRun, run_id)
    if run.mode == "autonomous":
        ctx.require("owner")
    try:
        optimization.continue_run(ctx.db, run, policy=(body.policy if body else None), user_id=ctx.user.id)
    except optimization.OptimizationError as exc:
        raise fail(409, exc.code, exc.message) from exc
    ctx.db.commit()
    job = jobs.enqueue(ctx.db, workspace_id=ctx.ws, kind="optimization.run", payload={"optimization_run_id": run_id},
                       created_by=ctx.user.id)
    ctx.db.expire_all()
    run = scoped(ctx, OptimizationRun, run_id)
    run.job_id = job.id
    ctx.db.commit()
    ctx.db.refresh(job)
    return {"job": job_dict(job), "optimization_run": _opt_dict(ctx, run)}


@router.get("/optimization-runs")
def list_optimizations(limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(OptimizationRun).where(OptimizationRun.workspace_id == ctx.ws).order_by(OptimizationRun.created_at.desc())
    return page(ctx.db, query, limit=limit, offset=offset, transform=lambda r: _opt_dict(ctx, r))


@router.get("/optimization-runs/{run_id}")
def get_optimization(run_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return _opt_dict(ctx, scoped(ctx, OptimizationRun, run_id), detail=True)


@router.post("/optimization-runs/{run_id}/stop")
def stop_optimization(run_id: str, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    run = scoped(ctx, OptimizationRun, run_id)
    job = ctx.db.get(Job, run.job_id) if run.job_id else None
    if job is not None:
        was_paused = job.status in ("queued", "review_required")
        jobs.request_cancel(ctx.db, job)
        if was_paused and run.status not in ("completed", "failed", "cancelled"):
            run.status, run.stop_reason, run.finished_at = "cancelled", "cancelled by a user", utcnow()
            run.holdout = {"status": "skipped", "reason": "the run was stopped before validation"}
            for pending in ctx.db.scalars(select(CandidatePatch).where(CandidatePatch.candidate_id == run.candidate_id,
                                                                       CandidatePatch.state == "proposed")):
                pending.state, pending.decision_note = "rejected", "not decided: the run was stopped"
            candidate = ctx.db.get(Candidate, run.candidate_id) if run.candidate_id else None
            if candidate is not None and candidate.status == "active":
                kept = any(h.get("outcome") == "kept" for h in (run.state or {}).get("history", []))
                candidate.status = "awaiting_decision" if kept else "rejected"
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="optimization.stop_requested",
                 subject_type="optimization_run", subject_id=run.id)
    ctx.db.commit()
    return _opt_dict(ctx, run, detail=True)


@router.post("/candidate-patches/{patch_id}/decision")
def decide_patch(patch_id: str, body: Decision, ctx: Ctx = Depends(workspace_ctx("reviewer"))) -> dict[str, Any]:
    """Approve or reject one proposed change to the candidate (human-guided mode). Never touches production."""
    patch = scoped(ctx, CandidatePatch, patch_id)
    try:
        optimization.decide_patch(ctx.db, patch, decision=body.decision, user_id=ctx.user.id, note=body.note)
    except optimization.OptimizationError as exc:
        raise fail(422 if exc.code == "REASON_REQUIRED" else 409, exc.code, exc.message) from exc
    ctx.db.commit()
    candidate = ctx.db.get(Candidate, patch.candidate_id)
    run = ctx.db.get(OptimizationRun, candidate.optimization_run_id) if candidate and candidate.optimization_run_id else None
    if run is not None and run.job_id:
        jobs.resume(ctx.db, run.job_id)
        ctx.db.expire_all()
        run = ctx.db.get(OptimizationRun, run.id)
    patch = scoped(ctx, CandidatePatch, patch_id)
    return {"patch": ser(patch), "optimization_run": _opt_dict(ctx, run) if run else None}


# --- candidates and promotion -------------------------------------------------------------


def _candidate_dict(ctx: Ctx, candidate: Candidate, *, detail: bool = False) -> dict[str, Any]:
    out = ser(candidate, extra={"diff_from_baseline": render_diff(candidate.baseline_snapshot, candidate.config_snapshot)})
    if detail:
        out["patches"] = [ser(p) for p in ctx.db.scalars(
            select(CandidatePatch).where(CandidatePatch.candidate_id == candidate.id).order_by(CandidatePatch.round))]
        out["approvals"] = [ser(a) for a in ctx.db.scalars(
            select(Approval).where(Approval.subject_type == "candidate", Approval.subject_id == candidate.id)
            .order_by(Approval.created_at))]
    return out


@router.get("/candidates")
def list_candidates(agent_id: str | None = None, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(Candidate).where(Candidate.workspace_id == ctx.ws).order_by(Candidate.created_at.desc())
    if agent_id:
        query = query.where(Candidate.agent_id == agent_id)
    return {"items": [_candidate_dict(ctx, c) for c in ctx.db.scalars(query.limit(200))]}


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return _candidate_dict(ctx, scoped(ctx, Candidate, candidate_id), detail=True)


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, body: Decision, ctx: Ctx = Depends(workspace_ctx("owner"))) -> dict[str, Any]:
    """Record the explicit release decision for a validated candidate. Approval alone deploys nothing."""
    candidate = scoped(ctx, Candidate, candidate_id)
    try:
        approval = optimization.approve_promotion(ctx.db, candidate, decision=body.decision, user_id=ctx.user.id, note=body.note)
    except optimization.OptimizationError as exc:
        raise fail(409, exc.code, exc.message) from exc
    ctx.db.commit()
    return {"approval": ser(approval), "candidate": _candidate_dict(ctx, candidate)}


@router.post("/candidates/{candidate_id}/promote")
def promote_candidate(candidate_id: str, ctx: Ctx = Depends(workspace_ctx("owner"))) -> dict[str, Any]:
    """Promote an approved candidate through the connector. Needs the promote capability and a prior approval."""
    candidate = scoped(ctx, Candidate, candidate_id)
    try:
        result = optimization.promote(ctx.db, candidate, user_id=ctx.user.id)
    except optimization.OptimizationError as exc:
        raise fail(409, exc.code, exc.message) from exc
    ctx.db.commit()
    return {"result": result, "candidate": _candidate_dict(ctx, candidate)}


@router.post("/candidates/{candidate_id}/rollback")
def rollback_candidate(candidate_id: str, body: Note, ctx: Ctx = Depends(workspace_ctx("owner"))) -> dict[str, Any]:
    candidate = scoped(ctx, Candidate, candidate_id)
    try:
        result = optimization.rollback_production(ctx.db, candidate, user_id=ctx.user.id, note=body.note)
    except optimization.OptimizationError as exc:
        raise fail(409, exc.code, exc.message) from exc
    ctx.db.commit()
    return {"result": result, "candidate": _candidate_dict(ctx, candidate)}


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return ser(scoped(ctx, Experiment, experiment_id))


# --- reports, audit, jobs -----------------------------------------------------------------


@router.get("/reports/{object_id}")
def report(object_id: str, format: Literal["json", "md"] = "json", ctx: Ctx = Depends(workspace_ctx())) -> Any:
    """Report for an evaluation run or an optimization run, as JSON or Markdown."""
    optimization_run = ctx.db.get(OptimizationRun, object_id)
    if optimization_run is not None and optimization_run.workspace_id == ctx.ws:
        data = reporting.optimization_report(ctx.db, optimization_run)
    else:
        data = reporting.evaluation_report(ctx.db, scoped(ctx, EvaluationRun, object_id))
    if format == "md":
        return PlainTextResponse(reporting.to_markdown(data), media_type="text/markdown; charset=utf-8")
    return data


@router.get("/audit")
def audit_log(action: str | None = None, limit: int = 100, offset: int = 0,
              ctx: Ctx = Depends(workspace_ctx("reviewer"))) -> dict[str, Any]:
    query = select(AuditEvent).where(AuditEvent.workspace_id == ctx.ws).order_by(AuditEvent.created_at.desc())
    if action:
        query = query.where(AuditEvent.action.like(f"{action}%"))
    return page(ctx.db, query, limit=limit, offset=offset)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return job_dict(scoped(ctx, Job, job_id))


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Cancel a queued or running evaluation or optimization run."""
    run = ctx.db.get(EvaluationRun, run_id) or ctx.db.get(OptimizationRun, run_id)
    if run is None or run.workspace_id != ctx.ws:
        raise fail(404, "NOT_FOUND", "run not found")
    if isinstance(run, OptimizationRun):
        return stop_optimization(run_id, ctx)
    job = ctx.db.get(Job, run.job_id) if run.job_id else None
    if job is not None:
        queued = job.status == "queued"
        jobs.request_cancel(ctx.db, job)
        if queued:
            run.status = "cancelled"
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="evaluation.cancel_requested",
                 subject_type="evaluation_run", subject_id=run.id)
    ctx.db.commit()
    return _run_dict(run)
