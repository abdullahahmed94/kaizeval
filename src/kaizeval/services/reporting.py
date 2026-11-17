"""Evidence-based reports for evaluation and optimization runs (JSON, with a Markdown rendering)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import render_diff
from ..gateway import Gateway
from ..models import (
    Agent,
    Approval,
    Candidate,
    CandidatePatch,
    Dataset,
    EvaluationRun,
    Finding,
    OptimizationRun,
    Recommendation,
    iso,
    utcnow,
)
from .stats import compare_runs

SYNTHETIC_BANNER = ("SYNTHETIC RESULT: produced with the deterministic fake model provider. It demonstrates the "
                    "workflow only and is not evidence of real agent quality or improvement.")


def _run_block(run: EvaluationRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    s = run.summary or {}
    return {"run_id": run.id, "agent_version": run.agent_version, "config_digest": run.agent_config_digest,
            "mode": run.mode, "status": run.status, "cases": s.get("cases"), "scored": s.get("scored"),
            "overall": s.get("overall"), "pass_rate": s.get("pass_rate"), "by_category": s.get("by_category"),
            "by_dimension": s.get("by_dimension"), "critical_failures": s.get("critical_failures"),
            "needs_human_review": s.get("needs_human_review"), "latency_ms": s.get("latency_ms"),
            "evaluator_agreement": s.get("evaluator_agreement"), "reference_basis": s.get("reference_basis")}


def _header(db: Session, workspace_id: str, agent_id: str, dataset_id: str) -> dict[str, Any]:
    agent, dataset = db.get(Agent, agent_id), db.get(Dataset, dataset_id)
    return {
        "generated_at": utcnow().isoformat(),
        "agent": {"id": agent_id, "name": agent.name if agent else "", "adapter": agent.adapter if agent else "",
                  "access_level": agent.access_level if agent else ""},
        "dataset": {"id": dataset_id, "name": dataset.name if dataset else "", "version": dataset.version if dataset else None,
                    "holdout_generation": dataset.holdout_generation if dataset else None},
    }


def evaluation_report(db: Session, run: EvaluationRun) -> dict[str, Any]:
    report: dict[str, Any] = {"type": "evaluation", "id": run.id, **_header(db, run.workspace_id, run.agent_id, run.dataset_id),
                              "synthetic": run.synthetic, "banner": SYNTHETIC_BANNER if run.synthetic else None,
                              "split": run.split, "result": _run_block(run), "error": run.error,
                              "usage": {"platform_models": (run.summary or {}).get("model_usage"),
                                        "target_agent": (run.summary or {}).get("agent")},
                              "caveats": (run.summary or {}).get("caveats", [])}
    if run.split == "regression":
        findings = list(db.scalars(select(Finding).where(Finding.run_id == run.id)))
        by_category: dict[str, int] = {}
        for f in findings:
            by_category[f.category] = by_category.get(f.category, 0) + 1
        report["findings_by_category"] = by_category
        report["recommendations"] = [
            {"id": r.id, "title": r.title, "category": r.category, "target_component": r.target_component, "risk": r.risk,
             "impact": r.impact, "state": r.state, "auto_apply_available": r.auto_apply_available,
             "auto_apply_blocker": r.auto_apply_blocker, "suggested_patch": r.suggested_patch}
            for r in db.scalars(select(Recommendation).where(Recommendation.run_id == run.id).order_by(Recommendation.priority.desc()))]
    else:
        report["note"] = "Holdout run: only aggregate results are reported; no per-case feedback is produced."
    return report


def optimization_report(db: Session, run: OptimizationRun) -> dict[str, Any]:
    baseline = db.get(EvaluationRun, run.baseline_run_id) if run.baseline_run_id else None
    best = db.get(EvaluationRun, run.best_run_id) if run.best_run_id else None
    candidate = db.get(Candidate, run.candidate_id) if run.candidate_id else None
    patches = list(db.scalars(select(CandidatePatch).where(CandidatePatch.candidate_id == run.candidate_id)
                              .order_by(CandidatePatch.round))) if run.candidate_id else []
    approvals = list(db.scalars(select(Approval).where(Approval.subject_type == "candidate",
                                                       Approval.subject_id == run.candidate_id))) if run.candidate_id else []
    comparison = compare_runs(db, baseline, best, include_cases=False) if baseline and best and baseline.id != best.id else None
    agent_calls, agent_cost, cost_known = 0, 0.0, False
    for r in db.scalars(select(EvaluationRun).where(EvaluationRun.optimization_run_id == run.id)):
        agent = (r.summary or {}).get("agent") or {}
        agent_calls += agent.get("calls", 0)
        if agent.get("estimated_cost") is not None:
            agent_cost, cost_known = agent_cost + agent["estimated_cost"], True
    caveats = ["Results describe behaviour on this test set only; they are not a measure of real-world accuracy.",
               "Nothing was changed in production. The baseline version was never modified."]
    if comparison and comparison["overall"]["verdict"] == "inconclusive":
        caveats.append("The regression difference is statistically inconclusive at this sample size.")
    holdout = run.holdout or {}
    if holdout.get("status") == "completed" and holdout["overall"]["verdict"] != "improved":
        caveats.append("The sealed holdout does not confirm an improvement; treat the regression gain with caution.")
    return {
        "type": "optimization", "id": run.id, **_header(db, run.workspace_id, run.agent_id, run.dataset_id),
        "synthetic": run.synthetic, "banner": SYNTHETIC_BANNER if run.synthetic else None,
        "mode": run.mode, "status": run.status, "stop_reason": run.stop_reason, "error": run.error,
        "rounds": (run.state or {}).get("round", 0), "policy": run.policy,
        "baseline": _run_block(baseline), "best": _run_block(best),
        "regression_comparison": comparison, "holdout": run.holdout,
        "changes": [{"round": p.round, "patch": p.patch, "diff": p.diff, "rationale": p.rationale, "risk": p.risk,
                     "state": p.state, "outcome": p.outcome, "decided_by": p.decided_by, "synthetic": p.synthetic}
                    for p in patches],
        "candidate": {"id": candidate.id, "version": candidate.external_id, "status": candidate.status,
                      "baseline_digest": candidate.baseline_digest,
                      "diff_from_baseline": render_diff(candidate.baseline_snapshot, candidate.config_snapshot)} if candidate else None,
        "usage": {"platform_models": Gateway(db, run.workspace_id).usage(scope_id=run.id),
                  "target_agent": {"calls": agent_calls, "estimated_cost": round(agent_cost, 6) if cost_known else None,
                                   "cost_note": "Reported separately from platform model usage."}},
        "release_decision": {
            "candidate_status": candidate.status if candidate else None,
            "approvals": [{"decision": a.decision, "by": a.user_id, "at": iso(a.created_at), "note": a.note,
                           "consumed": a.consumed} for a in approvals],
            "note": "Promotion to production requires an explicit approval and is never automatic."},
        "caveats": caveats,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Kaizeval {report['type']} report", ""]
    if report.get("banner"):
        lines += [f"> **{report['banner']}**", ""]
    lines += [f"- Agent: **{report['agent']['name']}** ({report['agent']['adapter']}, {report['agent']['access_level']})",
              f"- Dataset: **{report['dataset']['name']}** v{report['dataset']['version']}",
              f"- Generated: {report['generated_at']}", ""]

    def table(block: dict[str, Any] | None, title: str) -> None:
        if not block:
            return
        lines.extend([f"## {title}", "",
                      f"Overall **{_pct(block.get('overall'))}**, pass rate {_pct(block.get('pass_rate'))}, "
                      f"{block.get('scored')}/{block.get('cases')} cases scored, "
                      f"{block.get('critical_failures')} critical failure(s).", "",
                      "| Category | Cases | Mean score | Pass rate |", "|---|---|---|---|"])
        for name, v in (block.get("by_category") or {}).items():
            lines.append(f"| {name} | {v['n']} | {_pct(v.get('mean'))} | {_pct(v.get('pass_rate'))} |")
        lines.append("")

    if report["type"] == "evaluation":
        table(report.get("result"), f"Result ({report.get('split')} set)")
        for rec in report.get("recommendations") or []:
            lines.append(f"- **{rec['title']}** ({rec['category']}, risk {rec['risk']}, "
                         f"{rec['impact'].get('cases_affected')} case(s)) [{rec['state']}]")
    else:
        lines += [f"Status: **{report['status']}**. Stop reason: {report.get('stop_reason') or 'n/a'}. "
                  f"Rounds: {report.get('rounds')}.", ""]
        table(report.get("baseline"), "Baseline A (regression set)")
        table(report.get("best"), "Best version (regression set)")
        comparison = report.get("regression_comparison")
        if comparison:
            o = comparison["overall"]
            lines += ["## Paired A/B comparison (regression set)", "",
                      f"Change: **{o['delta_percentage_points']:+.2f} percentage points** over {o['n']} paired cases "
                      f"(95% CI {o.get('ci95_percentage_points')}, relative {o.get('relative_change_pct')}%). "
                      f"Verdict: **{o['verdict']}**. {o['interpretation']}", ""]
        holdout = report.get("holdout") or {}
        lines += ["## Independent holdout validation", ""]
        if holdout.get("status") == "completed":
            o = holdout["overall"]
            lines += [f"Change: **{o['delta_percentage_points']:+.2f} percentage points** over {o['n']} sealed cases "
                      f"(95% CI {o.get('ci95_percentage_points')}). Verdict: **{o['verdict']}**. {o['interpretation']}", ""]
        else:
            lines += [f"{holdout.get('status', 'not run')}: {holdout.get('reason', '')}", ""]
        lines += ["## Changes", ""]
        for change in report.get("changes") or []:
            lines += [f"- Round {change['round']}: `{change['patch']}` -> **{change['state']}**. {change.get('rationale', '')}"]
        lines.append("")
    usage = report.get("usage") or {}
    models, agent = usage.get("platform_models") or {}, usage.get("target_agent") or {}
    lines += ["## Usage", "",
              f"- Platform models: {models.get('calls', 0)} calls, {models.get('prompt_tokens', 0)} prompt tokens, "
              f"{models.get('completion_tokens', 0)} completion tokens, estimated cost: {models.get('estimated_cost')}",
              f"- Target agent: {agent.get('calls', 0)} calls, estimated cost: {agent.get('estimated_cost')}", "",
              "## Caveats", ""]
    lines += [f"- {c}" for c in report.get("caveats") or []]
    return "\n".join(lines) + "\n"
