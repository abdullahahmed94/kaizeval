"""Controlled optimization of an isolated Candidate B against an immutable Baseline A.

Rules enforced here:
* patches are applied only to a candidate copy created through the connector; the baseline digest
  is re-checked after every patch;
* the whole regression set is re-run after each change and compared pairwise with the baseline;
* a change is kept only if it passes every gate, otherwise the candidate is restored;
* the loop is bounded by rounds, model/agent budgets, stagnation and cancellation;
* this module reads regression data only. Final validation is delegated to ``services.holdout``,
  which returns aggregates; holdout items never enter the optimizer's inputs;
* nothing here promotes to production. Promotion is a separate, explicitly approved action.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..connectors import AdapterError, AgentAdapter, build_adapter, render_diff, require
from ..gateway import Gateway, GatewayError
from ..jobs import JobCancelled, JobContext, ReviewRequired
from ..models import (
    Agent,
    Approval,
    Candidate,
    CandidatePatch,
    Dataset,
    EvaluationRun,
    Experiment,
    Finding,
    OptimizationRun,
    Workspace,
    utcnow,
)
from . import holdout as holdout_service
from .datasets import regression_cases, split_counts
from .diagnostics import diagnose
from .evaluation import create_run, execute_run
from .stats import compare_runs

DEFAULT_POLICY: dict[str, Any] = {
    "max_rounds": 5,
    "target_score": None,
    "min_improvement": 0.005,
    "stagnation_rounds": 2,
    "max_category_regression": 0.05,
    "allow_new_critical_failures": False,
    "category_minimums": {},
    "max_latency_increase_pct": None,
    "eval_mode": "single",
    "eval_config": {},
    "run_holdout": True,
    "limits": {},  # max_model_calls, max_model_tokens, max_model_cost, max_agent_calls
}


class OptimizationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def validate_patch(patch: dict[str, Any], schema: dict[str, Any]) -> None:
    """Platform-side allowlist check, applied before the connector is asked to change anything."""
    if not isinstance(patch, dict) or not patch:
        raise OptimizationError("INVALID_PATCH", "the patch is empty")
    for key, value in patch.items():
        meta = schema.get(key)
        if meta is None:
            raise OptimizationError("INVALID_PATCH", f"'{key}' is not an allowlisted setting")
        kind = meta.get("type")
        if kind == "boolean":
            if not isinstance(value, bool):
                raise OptimizationError("INVALID_PATCH", f"'{key}' must be a boolean")
        elif kind in ("integer", "number"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OptimizationError("INVALID_PATCH", f"'{key}' must be a number")
            if kind == "integer" and int(value) != value:
                raise OptimizationError("INVALID_PATCH", f"'{key}' must be a whole number")
            if (meta.get("min") is not None and value < meta["min"]) or (meta.get("max") is not None and value > meta["max"]):
                raise OptimizationError("INVALID_PATCH", f"'{key}' is outside the allowed range")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > int(meta.get("max_length", 4000)):
                raise OptimizationError("INVALID_PATCH", f"'{key}' must be text within the allowed length")
        else:
            raise OptimizationError("INVALID_PATCH", f"'{key}' has an unsupported type")


def create_optimization_run(db: Session, *, workspace_id: str, agent_id: str, dataset_id: str, mode: str,
                            policy: dict[str, Any] | None, user_id: str) -> OptimizationRun:
    if mode not in ("human_guided", "autonomous"):
        raise OptimizationError("INVALID_MODE", "mode must be 'human_guided' or 'autonomous'")
    agent, dataset = db.get(Agent, agent_id), db.get(Dataset, dataset_id)
    workspace = db.get(Workspace, workspace_id)
    assert workspace is not None
    if agent is None or dataset is None or agent.workspace_id != workspace_id or dataset.workspace_id != workspace_id:
        raise OptimizationError("NOT_FOUND", "agent or dataset not found in this workspace")
    try:
        for capability in ("inspect", "create_candidate", "apply_patch", "restore_candidate"):
            require(agent, capability)
    except AdapterError as exc:
        raise OptimizationError(exc.code, exc.message + ". Only recommendations are available for this agent.") from exc
    if mode == "autonomous" and not (workspace.settings or {}).get("allow_autonomous_optimization"):
        raise OptimizationError("POLICY_BLOCKED", "autonomous optimization is disabled for this workspace; "
                                "an owner must enable it, or use human-guided mode")
    merged = {**DEFAULT_POLICY, **(policy or {})}
    counts = split_counts(db, dataset_id)
    if not counts.get("regression"):
        raise OptimizationError("NO_REGRESSION_SET", "the dataset has no regression cases")
    if merged["run_holdout"] and counts.get("holdout") and dataset.holdout_exposed:
        raise OptimizationError("HOLDOUT_SPENT", "this holdout already informed a decision or was viewed; rotate it "
                                "before optimizing again, or set run_holdout to false")
    run = OptimizationRun(workspace_id=workspace_id, agent_id=agent_id, dataset_id=dataset_id, mode=mode,
                          policy=merged, state={"phase": "init", "round": 0, "history": [], "stagnation": 0},
                          created_by=user_id)
    db.add(run)
    db.flush()
    audit.record(db, workspace_id=workspace_id, user_id=user_id, action="optimization.created",
                 subject_type="optimization_run", subject_id=run.id, detail={"mode": mode, "policy": merged})
    return run


# --- loop ---------------------------------------------------------------------------------


def _eval_config(run: OptimizationRun) -> dict[str, Any]:
    return {**(run.policy.get("eval_config") or {}), "limits": run.policy.get("limits") or {}}


def _evaluate(db: Session, run: OptimizationRun, version: str, kind: str, ctx: JobContext | None) -> EvaluationRun:
    eval_run = create_run(db, workspace_id=run.workspace_id, agent_id=run.agent_id, dataset_id=run.dataset_id,
                          split="regression", mode=run.policy["eval_mode"], kind=kind, agent_version=version,
                          config=_eval_config(run), user_id=run.created_by, optimization_run_id=run.id)
    db.commit()
    eval_run = execute_run(db, eval_run.id, ctx=ctx)
    if eval_run.status == "completed":
        diagnose(db, eval_run, use_model=False)
        db.commit()
    return eval_run


def _failure_summary(db: Session, eval_run: EvaluationRun) -> dict[str, Any]:
    """What the optimizer is allowed to see: regression findings and a few regression examples."""
    assert eval_run.split == "regression"
    findings = list(db.scalars(select(Finding).where(Finding.run_id == eval_run.id)))
    by_category = Counter(f.category for f in findings)
    allowed_ids = {c.id for c in regression_cases(db, eval_run.dataset_id)}
    examples: list[dict[str, Any]] = []
    for finding in findings:
        case_id = finding.evidence.get("case_id")
        if case_id in allowed_ids and len(examples) < 5:
            examples.append({"category": finding.category, "question": finding.evidence.get("question", ""),
                             "observation": finding.observation})
    summary = eval_run.summary or {}
    return {"by_category": dict(by_category), "overall": summary.get("overall"), "pass_rate": summary.get("pass_rate"),
            "by_dimension": {d: v["mean"] for d, v in (summary.get("by_dimension") or {}).items()},
            "critical_failures": summary.get("critical_failures", 0), "examples": examples}


def _gates(policy: dict[str, Any], comparison_vs_baseline: dict[str, Any], candidate_overall: float,
           best_overall: float, candidate_summary: dict[str, Any]) -> list[str]:
    """Return the list of violated gates (empty means the change may be kept)."""
    reasons = []
    if candidate_overall - best_overall < float(policy["min_improvement"]):
        reasons.append(f"no sufficient improvement over the best version so far "
                       f"({(candidate_overall - best_overall) * 100:+.2f} pp, minimum {float(policy['min_improvement']) * 100:.2f} pp)")
    if not policy.get("allow_new_critical_failures") and comparison_vs_baseline["critical_failures"]["new"] > 0:
        reasons.append(f"{comparison_vs_baseline['critical_failures']['new']} new critical failure(s) compared with the baseline")
    tolerance = float(policy["max_category_regression"]) * 100
    for category, values in comparison_vs_baseline["by_category"].items():
        if values["delta_percentage_points"] < -tolerance:
            reasons.append(f"category '{category}' regressed by {abs(values['delta_percentage_points']):.2f} pp "
                           f"(tolerance {tolerance:.2f} pp)")
    for category, minimum in (policy.get("category_minimums") or {}).items():
        mean = ((candidate_summary.get("by_category") or {}).get(category) or {}).get("mean")
        if mean is not None and mean < float(minimum):
            reasons.append(f"category '{category}' is below its required minimum ({mean:.2f} < {float(minimum):.2f})")
    limit = policy.get("max_latency_increase_pct")
    base_p50, cand_p50 = comparison_vs_baseline["latency_ms"]["baseline_p50"], comparison_vs_baseline["latency_ms"]["candidate_p50"]
    if limit is not None and base_p50 and cand_p50 and (cand_p50 - base_p50) / base_p50 * 100 > float(limit):
        reasons.append(f"median latency rose by more than {limit}%")
    return reasons


def _budget_reason(db: Session, run: OptimizationRun) -> str | None:
    limits = run.policy.get("limits") or {}
    usage = Gateway(db, run.workspace_id).usage(scope_id=run.id)
    if limits.get("max_model_calls") is not None and usage["calls"] >= limits["max_model_calls"]:
        return "model call budget reached"
    if limits.get("max_model_tokens") is not None and usage["prompt_tokens"] + usage["completion_tokens"] >= limits["max_model_tokens"]:
        return "model token budget reached"
    if limits.get("max_model_cost") is not None and (usage["estimated_cost"] or 0) >= limits["max_model_cost"]:
        return "estimated model cost budget reached"
    if limits.get("max_agent_calls") is not None:
        calls = sum(((r.summary or {}).get("agent") or {}).get("calls", 0) for r in db.scalars(
            select(EvaluationRun).where(EvaluationRun.optimization_run_id == run.id)))
        if calls >= limits["max_agent_calls"]:
            return "agent call budget reached"
    return None


def _save(db: Session, run: OptimizationRun, state: dict[str, Any]) -> None:
    run.state = dict(state)
    db.commit()


def _finalize(db: Session, run: OptimizationRun, state: dict[str, Any], reason: str, ctx: JobContext | None) -> None:
    candidate = db.get(Candidate, run.candidate_id) if run.candidate_id else None
    kept = [h for h in state["history"] if h.get("outcome") == "kept"]
    run.stop_reason = reason
    if candidate is not None and kept and run.policy.get("run_holdout"):
        run.holdout = holdout_service.validate(
            db, workspace_id=run.workspace_id, agent_id=run.agent_id, dataset_id=run.dataset_id,
            candidate_version=candidate.external_id, mode=run.policy["eval_mode"], config=_eval_config(run),
            optimization_run_id=run.id, user_id=run.created_by, ctx=ctx)
    elif not kept:
        run.holdout = {"status": "skipped", "reason": "no change was kept, so there is no candidate to validate"}
    else:
        run.holdout = {"status": "skipped", "reason": "holdout validation was disabled for this run"}
    if candidate is not None:
        candidate.status = "awaiting_decision" if kept else "rejected"
    state["phase"] = "done"
    run.state, run.status, run.finished_at = dict(state), "completed", utcnow()
    audit.record(db, workspace_id=run.workspace_id, user_id=run.created_by, action="optimization.completed",
                 subject_type="optimization_run", subject_id=run.id,
                 detail={"stop_reason": reason, "rounds": state["round"], "kept_changes": len(kept)})
    db.commit()


def _fail(db: Session, run: OptimizationRun, message: str) -> None:
    run.status, run.error, run.finished_at = "failed", message, utcnow()
    db.commit()


def advance(db: Session, run_id: str, ctx: JobContext | None = None) -> OptimizationRun:
    """Drive the optimization state machine until it finishes, fails or pauses for a human decision."""
    run = db.get(OptimizationRun, run_id)
    if run is None:
        raise OptimizationError("NOT_FOUND", "optimization run not found")
    agent = db.get(Agent, run.agent_id)
    assert agent is not None
    state = dict(run.state or {})
    state.setdefault("history", [])
    policy = run.policy
    try:
        adapter: AgentAdapter = build_adapter(db, agent)
        info = adapter.inspect("baseline")
    except AdapterError as exc:
        _fail(db, run, f"{exc.code}: {exc.message}")
        return run
    schema = info.get("schema") or {}
    run.status = "running"
    db.commit()

    try:
        if state.get("phase") == "init":
            first_run = _evaluate(db, run, "baseline", "baseline", ctx)
            if first_run.status != "completed":
                _fail(db, run, f"baseline evaluation failed: {first_run.error}")
                return run
            external_id = adapter.create_candidate()
            created = Candidate(workspace_id=run.workspace_id, agent_id=agent.id, external_id=external_id,
                                  baseline_snapshot=info["config"], baseline_digest=info.get("digest", ""),
                                  config_snapshot=dict(info["config"]), optimization_run_id=run.id)
            db.add(created)
            db.flush()
            run.candidate_id, run.baseline_run_id, run.best_run_id = created.id, first_run.id, first_run.id
            run.synthetic = bool(first_run.synthetic)
            state.update({"phase": "propose", "round": 0, "best_overall": (first_run.summary or {}).get("overall") or 0.0,
                          "baseline_overall": (first_run.summary or {}).get("overall") or 0.0, "stagnation": 0})
            audit.record(db, workspace_id=run.workspace_id, user_id=run.created_by, action="candidate.created",
                         subject_type="candidate", subject_id=created.id,
                         detail={"external_id": external_id, "baseline_digest": created.baseline_digest})
            _save(db, run, state)

        candidate = db.get(Candidate, run.candidate_id) if run.candidate_id else None
        assert candidate is not None
        baseline_run = db.get(EvaluationRun, run.baseline_run_id)
        assert baseline_run is not None

        while True:
            if ctx and ctx.cancelled():
                raise JobCancelled()

            if state["phase"] == "propose":
                target = policy.get("target_score")
                if target is not None and state["best_overall"] >= float(target):
                    _finalize(db, run, state, "target score reached", ctx)
                    return run
                if state["round"] >= int(policy["max_rounds"]):
                    _finalize(db, run, state, "maximum number of rounds reached", ctx)
                    return run
                if state["stagnation"] >= int(policy["stagnation_rounds"]):
                    _finalize(db, run, state, f"no improvement in {state['stagnation']} consecutive round(s)", ctx)
                    return run
                budget = _budget_reason(db, run)
                if budget:
                    _finalize(db, run, state, budget, ctx)
                    return run
                best_run = db.get(EvaluationRun, run.best_run_id)
                assert best_run is not None
                gateway = Gateway(db, run.workspace_id, run_id=run.id, scope_id=run.id, limits=policy.get("limits"))
                try:
                    proposal, meta = gateway.generate("optimize", {
                        "config_schema": schema, "current_config": candidate.config_snapshot,
                        "failure_summary": _failure_summary(db, best_run),
                        "history": [{"patch": h["patch"], "outcome": h["outcome"]} for h in state["history"]],
                    })
                except GatewayError as exc:
                    db.commit()
                    _finalize(db, run, state, f"optimizer unavailable ({exc.code}: {exc.message})", ctx)
                    return run
                run.synthetic = run.synthetic or meta.synthetic
                if not proposal.patch:
                    _finalize(db, run, state, "the optimizer found no further safe change", ctx)
                    return run
                state["round"] += 1
                try:
                    validate_patch(proposal.patch, schema)
                except OptimizationError as exc:
                    state["history"].append({"round": state["round"], "patch": proposal.patch, "outcome": "invalid",
                                             "reasons": [exc.message]})
                    state["stagnation"] += 1
                    _save(db, run, state)
                    continue
                after = {**candidate.config_snapshot, **proposal.patch}
                patch = CandidatePatch(workspace_id=run.workspace_id, candidate_id=candidate.id, round=state["round"],
                                       patch=proposal.patch, diff=render_diff(candidate.config_snapshot, after),
                                       rationale=proposal.rationale, risk=proposal.risk, synthetic=meta.synthetic)
                db.add(patch)
                db.flush()
                state["pending_patch_id"] = patch.id
                if run.mode == "human_guided":
                    state["phase"] = "await_approval"
                    run.status = "review_required"
                    _save(db, run, state)
                    raise ReviewRequired({"optimization_run_id": run.id, "patch_id": patch.id})
                patch.state = "approved"
                patch.decision_note = "auto-approved by the autonomous policy of this run"
                state["phase"] = "apply"
                _save(db, run, state)

            elif state["phase"] == "await_approval":
                current_patch = db.get(CandidatePatch, state.get("pending_patch_id"))
                assert current_patch is not None
                if current_patch.state == "approved":
                    state["phase"] = "apply"
                    _save(db, run, state)
                elif current_patch.state == "rejected":
                    state["history"].append({"round": current_patch.round, "patch": current_patch.patch, "outcome": "rejected",
                                             "reasons": [current_patch.decision_note or "rejected by reviewer"]})
                    state["stagnation"] += 1
                    state["phase"], state["pending_patch_id"] = "propose", None
                    _save(db, run, state)
                else:
                    run.status = "review_required"
                    db.commit()
                    raise ReviewRequired({"optimization_run_id": run.id, "patch_id": current_patch.id})

            elif state["phase"] == "apply":
                current_patch = db.get(CandidatePatch, state.get("pending_patch_id"))
                assert current_patch is not None
                previous_config = dict(candidate.config_snapshot)
                applied = adapter.apply_patch(candidate.external_id, current_patch.patch)
                # Baseline immutability is verified after every write, not assumed.
                if adapter.inspect("baseline").get("digest", "") != candidate.baseline_digest:
                    _fail(db, run, "NO_SAFE_ISOLATION: the baseline configuration changed while patching the candidate")
                    return run
                current_patch.state = "applied"
                audit.record(db, workspace_id=run.workspace_id, user_id=current_patch.decided_by or run.created_by,
                             action="candidate.patched", subject_type="candidate", subject_id=candidate.id,
                             detail={"patch": current_patch.patch, "round": current_patch.round})
                db.commit()
                candidate_run = _evaluate(db, run, candidate.external_id, "candidate", ctx)
                if candidate_run.status != "completed":
                    adapter.restore_candidate(candidate.external_id, previous_config)
                    current_patch.state = "rolled_back"
                    current_patch.outcome = {"reasons": [f"evaluation did not complete: {candidate_run.error}"]}
                    state["history"].append({"round": current_patch.round, "patch": current_patch.patch, "outcome": "rolled_back",
                                             "reasons": current_patch.outcome["reasons"]})
                    _save(db, run, state)
                    _finalize(db, run, state, f"stopped: {candidate_run.error}", ctx)
                    return run
                comparison = compare_runs(db, baseline_run, candidate_run)
                overall = (candidate_run.summary or {}).get("overall") or 0.0
                reasons = _gates(policy, comparison, overall, state["best_overall"], candidate_run.summary or {})
                outcome = "kept" if not reasons else "rolled_back"
                if outcome == "kept":
                    candidate.config_snapshot = applied.get("after") or {**previous_config, **current_patch.patch}
                    state["best_overall"], state["stagnation"] = overall, 0
                    run.best_run_id = candidate_run.id
                    current_patch.state = "kept"
                else:
                    adapter.restore_candidate(candidate.external_id, previous_config)
                    state["stagnation"] += 1
                    current_patch.state = "rolled_back"
                    audit.record(db, workspace_id=run.workspace_id, user_id=run.created_by, action="candidate.rolled_back",
                                 subject_type="candidate", subject_id=candidate.id,
                                 detail={"patch": current_patch.patch, "round": current_patch.round, "reasons": reasons})
                current_patch.outcome = {"overall": overall, "delta_vs_baseline_pp": comparison["overall"]["delta_percentage_points"],
                                 "verdict": comparison["overall"]["verdict"], "reasons": reasons,
                                 "evaluation_run_id": candidate_run.id}
                db.add(Experiment(workspace_id=run.workspace_id, optimization_run_id=run.id, round=current_patch.round,
                                  kind="regression", baseline_run_id=baseline_run.id, candidate_run_id=candidate_run.id,
                                  comparison=comparison, decision=outcome))
                state["history"].append({"round": current_patch.round, "patch": current_patch.patch, "outcome": outcome, "overall": overall,
                                         "delta_vs_baseline_pp": comparison["overall"]["delta_percentage_points"],
                                         "reasons": reasons, "evaluation_run_id": candidate_run.id})
                state["phase"], state["pending_patch_id"] = "propose", None
                _save(db, run, state)
            else:
                return run
    except JobCancelled:
        # Stop where we are. The candidate keeps only changes that already passed every gate.
        db.rollback()
        run.status, run.stop_reason, run.finished_at = "cancelled", "cancelled by a user", utcnow()
        current = db.get(Candidate, run.candidate_id) if run.candidate_id else None
        if current is not None:
            pending = db.get(CandidatePatch, state.get("pending_patch_id")) if state.get("pending_patch_id") else None
            if pending is not None and pending.state == "applied":
                try:
                    adapter.restore_candidate(current.external_id, current.config_snapshot)
                    pending.state = "rolled_back"
                    pending.outcome = {"reasons": ["run cancelled before the change was evaluated"]}
                except AdapterError:
                    pass
            current.status = "awaiting_decision" if any(h.get("outcome") == "kept" for h in state["history"]) else "rejected"
        db.commit()
        raise
    except AdapterError as exc:
        db.rollback()
        _fail(db, run, f"{exc.code}: {exc.message}")
        return run
    finally:
        adapter.close()


# --- human decisions ----------------------------------------------------------------------


def decide_patch(db: Session, patch: CandidatePatch, *, decision: str, user_id: str, note: str | None) -> CandidatePatch:
    if decision not in ("approved", "rejected"):
        raise OptimizationError("INVALID_DECISION", "decision must be 'approved' or 'rejected'")
    if patch.state != "proposed":
        raise OptimizationError("ALREADY_DECIDED", f"this patch is already '{patch.state}'")
    if decision == "rejected" and not (note or "").strip():
        raise OptimizationError("REASON_REQUIRED", "a rejection needs a reason")
    patch.state, patch.decided_by, patch.decision_note = decision, user_id, note
    db.add(Approval(workspace_id=patch.workspace_id, subject_type="candidate_patch", subject_id=patch.id,
                    action="patch", decision=decision, user_id=user_id, note=note, consumed=True))
    audit.record(db, workspace_id=patch.workspace_id, user_id=user_id, action=f"patch.{decision}",
                 subject_type="candidate_patch", subject_id=patch.id, detail={"patch": patch.patch, "note": note})
    db.flush()
    return patch


def approve_promotion(db: Session, candidate: Candidate, *, decision: str, user_id: str, note: str | None) -> Approval:
    """Record the customer's explicit release decision. Approving does not deploy anything by itself."""
    if candidate.status != "awaiting_decision":
        raise OptimizationError("NOT_PROMOTABLE", f"candidate is '{candidate.status}', not awaiting a decision")
    if decision not in ("approved", "rejected"):
        raise OptimizationError("INVALID_DECISION", "decision must be 'approved' or 'rejected'")
    approval = Approval(workspace_id=candidate.workspace_id, subject_type="candidate", subject_id=candidate.id,
                        action="promote", decision=decision, user_id=user_id, note=note)
    db.add(approval)
    if decision == "rejected":
        candidate.status = "rejected"
        approval.consumed = True
    audit.record(db, workspace_id=candidate.workspace_id, user_id=user_id, action=f"promotion.{decision}",
                 subject_type="candidate", subject_id=candidate.id, detail={"note": note})
    db.flush()
    return approval


def promote(db: Session, candidate: Candidate, *, user_id: str) -> dict[str, Any]:
    """Promote through the connector. Requires the promote capability and an unused explicit approval."""
    agent = db.get(Agent, candidate.agent_id)
    assert agent is not None
    try:
        require(agent, "promote")
    except AdapterError as exc:
        raise OptimizationError(exc.code, exc.message + ". Release the candidate with your own deployment process.") from exc
    approval = db.scalar(select(Approval).where(
        Approval.subject_type == "candidate", Approval.subject_id == candidate.id, Approval.action == "promote",
        Approval.decision == "approved", Approval.consumed.is_(False)).order_by(Approval.created_at.desc()))
    if approval is None or candidate.status != "awaiting_decision":
        raise OptimizationError("APPROVAL_REQUIRED", "promotion needs an explicit approval for this candidate version")
    adapter = build_adapter(db, agent)
    try:
        result = adapter.promote(candidate.external_id, approval.id)
    except AdapterError as exc:
        raise OptimizationError(exc.code, exc.message) from exc
    finally:
        adapter.close()
    approval.consumed = True
    candidate.status = "promoted"
    audit.record(db, workspace_id=candidate.workspace_id, user_id=user_id, action="candidate.promoted",
                 subject_type="candidate", subject_id=candidate.id,
                 detail={"approval_id": approval.id, "approved_by": approval.user_id, "result": result})
    db.flush()
    return result


def rollback_production(db: Session, candidate: Candidate, *, user_id: str, note: str | None) -> dict[str, Any]:
    """Point production back at the untouched baseline."""
    agent = db.get(Agent, candidate.agent_id)
    assert agent is not None
    if candidate.status != "promoted":
        raise OptimizationError("NOT_PROMOTED", "only a promoted candidate can be rolled back")
    try:
        require(agent, "promote")
    except AdapterError as exc:
        raise OptimizationError(exc.code, exc.message) from exc
    approval = Approval(workspace_id=candidate.workspace_id, subject_type="candidate", subject_id=candidate.id,
                        action="promote", decision="approved", user_id=user_id, note=f"rollback: {note or ''}", consumed=True)
    db.add(approval)
    db.flush()
    adapter = build_adapter(db, agent)
    try:
        result = adapter.promote("baseline", approval.id)
    except AdapterError as exc:
        raise OptimizationError(exc.code, exc.message) from exc
    finally:
        adapter.close()
    candidate.status = "rolled_back"
    audit.record(db, workspace_id=candidate.workspace_id, user_id=user_id, action="candidate.production_rolled_back",
                 subject_type="candidate", subject_id=candidate.id, detail={"note": note, "result": result})
    db.flush()
    return result


__all__ = ["DEFAULT_POLICY", "OptimizationError", "advance", "approve_promotion", "create_optimization_run",
           "decide_patch", "promote", "rollback_production", "validate_patch"]
