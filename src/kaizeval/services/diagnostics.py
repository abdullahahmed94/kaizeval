"""Failure diagnosis and recommendations.

Findings separate what was observed from what is only suspected. A cause that cannot be observed
from outside the agent is reported as a hypothesis with a low confidence, never as a fact.
Diagnosis only ever reads regression results; holdout runs are refused.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..connectors import AdapterError, build_adapter
from ..gateway import Gateway, GatewayError
from ..gateway.schemas import FAILURE_CATEGORIES
from ..models import Agent, CaseResult, EvaluationRun, Finding, Recommendation, TestCase
from ..textutil import looks_like_abstention, normalize

TEMPLATES: dict[str, dict[str, str]] = {
    "citation": {
        "title": "Make the agent cite the source of each answer",
        "detail": "Answers were returned without a usable source reference, or cited the wrong document. "
                  "Have the agent append the document (and section) it used so answers can be verified.",
        "target": "response format / answer template", "risk": "low"},
    "instruction_prompt": {
        "title": "Add an explicit rule to decline when no supporting evidence is found",
        "detail": "The agent answered questions that the knowledge base does not cover. Add a refusal rule "
                  "(or a retrieval-confidence threshold) so it says it does not know instead of guessing. "
                  "Risk: a threshold that is too strict makes the agent refuse answerable questions.",
        "target": "system prompt / answer policy", "risk": "medium"},
    "retrieval_grounding": {
        "title": "Improve retrieval so the right passage reaches the answer step",
        "detail": "The knowledge base contains the answer, but the agent declined or answered from the wrong "
                  "passage. Review retrieval depth, query matching and chunking.",
        "target": "retrieval configuration", "risk": "medium"},
    "output_formatting": {
        "title": "Let answers include every required fact",
        "detail": "The agent found the right source but left out facts the reference requires. This usually "
                  "means answers are cut too short or the response template drops detail.",
        "target": "answer length / response template", "risk": "low"},
    "missing_kb_evidence": {
        "title": "Review knowledge gaps and unverified references",
        "detail": "Some test references could not be linked to verbatim evidence. Confirm whether the knowledge "
                  "base really contains this information, then approve or correct the reference answers.",
        "target": "knowledge base / reference answers", "risk": "low"},
    "orchestration": {
        "title": "Fix agent availability and error handling",
        "detail": "Calls to the agent failed or timed out. Check the endpoint, its timeout and rate limits.",
        "target": "agent runtime", "risk": "low"},
    "tool_calling": {
        "title": "Review tool-call failures",
        "detail": "Tool usage could not be verified from the available traces.",
        "target": "tools", "risk": "medium"},
    "unknown": {
        "title": "Review unexplained failures manually",
        "detail": "These failures did not match a known pattern. Inspect the cases and the agent's traces.",
        "target": "manual review", "risk": "low"},
}


def _signals(result: CaseResult, case: TestCase) -> dict[str, Any]:
    scores = result.final.get("scores") or {}
    cited = [normalize(c) for c in result.response.get("citations", [])]
    expected_files = {normalize(r.get("filename", "")) for r in case.evidence_refs or [] if r.get("filename")}
    return {
        "error": result.error, "expected_behavior": (case.expected or {}).get("behavior", "answer"),
        "abstained": looks_like_abstention(result.response.get("text", "")),
        "correctness": (scores.get("correctness") or {}).get("score"),
        "missing_facts": (result.deterministic.get("correctness") or {}).get("missing", []),
        "citation": (scores.get("citation_accuracy") or {}).get("score"),
        "has_citations": bool(cited),
        "cites_expected_document": any(any(f and f in c for f in expected_files) for c in cited),
        "reference_status": case.answer_status, "turns": len(case.turns or []),
    }


def classify(signals: dict[str, Any]) -> list[tuple[str, str, str, float]]:
    """Rule-based classification -> [(category, observation, hypothesis, confidence)]."""
    out: list[tuple[str, str, str, float]] = []
    if signals["error"]:
        return [("orchestration", f"The agent call failed: {signals['error']}",
                 "The agent endpoint was unavailable, too slow or returned an invalid response.", 0.8)]
    if signals["expected_behavior"] == "abstain":
        if not signals["abstained"]:
            out.append(("instruction_prompt", "The agent answered a question that the knowledge base does not cover.",
                        "The agent has no rule or threshold for declining when retrieval finds no support.", 0.7))
        return out
    correctness = signals["correctness"]
    if signals["reference_status"] in ("unverified", "needs_human_review") and correctness is None:
        out.append(("missing_kb_evidence", "The reference answer for this case is not linked to verbatim evidence.",
                    "The knowledge base may not contain this information, or the drafted reference is wrong.", 0.5))
    elif signals["abstained"]:
        out.append(("retrieval_grounding", "The agent declined a question that the knowledge base answers.",
                    "Retrieval did not surface the passage, or the refusal threshold is too strict.", 0.6))
    elif correctness is not None and correctness < 0.999:
        missing = ", ".join(str(m) for m in signals["missing_facts"][:4])
        observed = f"The answer is missing required facts: {missing}." if missing else "The answer scored low on correctness."
        if signals["has_citations"] and not signals["cites_expected_document"]:
            out.append(("retrieval_grounding", observed + " It cites a different document than the one holding the evidence.",
                        "Retrieval returned the wrong passage.", 0.6))
        elif signals["cites_expected_document"]:
            out.append(("output_formatting", observed + " It cites the correct document.",
                        "The right passage was retrieved but the answer was truncated or dropped detail.", 0.6))
        elif correctness > 0:
            out.append(("output_formatting", observed + " Part of the expected content is present.",
                        "The answer is probably too short; the retrieved passage cannot be observed without citations.", 0.4))
        else:
            out.append(("retrieval_grounding", observed + " None of the expected content is present.",
                        "The agent probably answered from the wrong passage; this cannot be confirmed without citations or traces.", 0.4))
    if signals["citation"] is not None and signals["citation"] < 1.0:
        out.append(("citation", "The answer does not cite the document that holds the evidence."
                    if signals["has_citations"] else "The answer cites no source.",
                    "The response format does not include source references.", 0.8 if not signals["has_citations"] else 0.5))
    return out


def diagnose(db: Session, run: EvaluationRun, *, use_model: bool = True) -> list[Finding]:
    """(Re)build findings for a regression run. Holdout runs are refused to keep hidden cases out of feedback loops."""
    if run.split != "regression":
        raise PermissionError("diagnostics are not produced for holdout runs")
    db.execute(delete(Finding).where(Finding.run_id == run.id))
    results = list(db.scalars(select(CaseResult).where(CaseResult.run_id == run.id)))
    cases = {c.id: c for c in db.scalars(select(TestCase).where(TestCase.id.in_([r.case_id for r in results])))} if results else {}
    findings: list[Finding] = []
    payload: list[dict[str, Any]] = []
    for result in results:
        case = cases.get(result.case_id)
        if case is None:
            continue
        signals = _signals(result, case)
        classified = classify(signals)
        if not classified and result.verdict == "fail":
            classified = [("unknown", "The case failed without a recognisable deterministic signal.",
                           "Cause not observable from the response alone.", 0.2)]
        for category, observation, hypothesis, confidence in classified:
            finding = Finding(workspace_id=run.workspace_id, run_id=run.id, case_result_id=result.id, category=category,
                              severity=case.severity, observation=observation, hypothesis=hypothesis, confidence=confidence,
                              evidence={"case_id": case.id, "question": case.turns[0]["content"] if case.turns else "",
                                        "response_excerpt": result.response.get("text", "")[:300],
                                        "signals": {k: v for k, v in signals.items() if k != "error"}})
            db.add(finding)
            findings.append(finding)
        if classified and len(payload) < 10 and result.verdict != "pass":
            payload.append({"case_ref": result.id, "question": case.turns[0]["content"] if case.turns else "",
                            "response": result.response.get("text", "")[:500], "signals": signals,
                            "rule_category": classified[0][0]})
    db.flush()
    gateway = Gateway(db, run.workspace_id, run_id=run.id, scope_id=run.optimization_run_id or run.id,
                      limits=(run.config or {}).get("limits"))
    if use_model and payload and gateway.is_configured("root_cause"):
        try:
            out, _meta = gateway.generate("root_cause", {"failures": payload, "categories": FAILURE_CATEGORIES})
        except GatewayError:
            out = None  # diagnosis degrades to the rule-based findings; the run itself is unaffected
        if out is not None:
            by_result: dict[str, list[Finding]] = defaultdict(list)
            for finding in findings:
                by_result[finding.case_result_id].append(finding)
            for item in out.findings:
                target = by_result.get(item.case_ref)
                if target and item.hypothesis.strip():
                    primary = target[0]
                    primary.hypothesis = item.hypothesis
                    primary.source = "rule+model"
                    primary.evidence = {**primary.evidence, "model_category": item.category,
                                        "model_confidence": item.confidence}
            db.flush()
    return findings


def _patch_for(category: str, schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for key, meta in schema.items():
        if category not in meta.get("addresses", []):
            continue
        if meta.get("type") == "boolean" and config.get(key) is not True:
            patch[key] = True
        elif meta.get("suggested") is not None and config.get(key) != meta["suggested"]:
            patch[key] = meta["suggested"]
    return patch


def recommend(db: Session, run: EvaluationRun) -> list[Recommendation]:
    """(Re)build prioritised recommendations from a run's findings. Keeps decisions already taken."""
    if run.split != "regression":
        raise PermissionError("recommendations are not produced for holdout runs")
    findings = list(db.scalars(select(Finding).where(Finding.run_id == run.id)))
    decided = {r.category: r for r in db.scalars(select(Recommendation).where(Recommendation.run_id == run.id))
               if r.state != "proposed"}
    db.execute(delete(Recommendation).where(Recommendation.run_id == run.id, Recommendation.state == "proposed"))
    agent = db.get(Agent, run.agent_id)
    assert agent is not None
    caps = set(agent.capabilities or [])
    schema: dict[str, Any] = {}
    config: dict[str, Any] = {}
    blocker: str | None = None
    if {"apply_patch", "create_candidate", "restore_candidate"} <= caps:
        try:
            info = build_adapter(db, agent).inspect("baseline")
            schema, config = info.get("schema") or {}, info.get("config") or {}
        except AdapterError as exc:
            blocker = f"{exc.code}: {exc.message}"
    elif agent.adapter == "http_chat":
        blocker = "UNSUPPORTED_CAPABILITY: a black-box HTTP agent cannot be patched; apply this change yourself"
    else:
        blocker = (f"INSUFFICIENT_PERMISSION: the agent's access level is '{agent.access_level}'; "
                   "candidate patching needs 'candidate_write'")

    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.category].append(finding)
    failed_cases = len({f.case_result_id for f in findings}) or 1
    created: list[Recommendation] = []
    for category, items in grouped.items():
        if category in decided:
            continue
        template = TEMPLATES.get(category, TEMPLATES["unknown"])
        affected = len({f.case_result_id for f in items})
        critical = sum(1 for f in items if f.severity == "critical")
        patch = _patch_for(category, schema, config) if schema else {}
        rec = Recommendation(
            workspace_id=run.workspace_id, run_id=run.id, title=template["title"], detail=template["detail"],
            category=category, target_component=template["target"], risk=template["risk"],
            impact={"cases_affected": affected, "share_of_cases_with_findings": round(affected / failed_cases, 4),
                    "critical_cases": critical,
                    "note": "Number of cases showing this problem; an indication of reach, not a promised gain."},
            required_capability="apply_patch" if patch else "",
            auto_apply_available=bool(patch), suggested_patch=patch or None,
            auto_apply_blocker=None if patch else (blocker or "no patchable setting is declared for this problem; apply manually"),
            priority=critical * 10 + affected, synthetic=run.synthetic,
        )
        db.add(rec)
        created.append(rec)
    db.flush()
    return created
