"""Evaluation runs: invoke the agent, apply deterministic checks first, then model judges when needed.

Precedence, in order:
1. a decisive deterministic check or an approved/evidence-linked reference;
2. a model judgment (single judge, or two independent judges with a conditional arbiter),
   always labelled as model-estimated;
3. no score at all ("n/a") when neither is available. Unobservable attributes are never scored.
"""

from __future__ import annotations

import statistics
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..connectors import AdapterError, AgentAdapter, build_adapter
from ..gateway import RUBRIC, Gateway, GatewayError
from ..jobs import JobCancelled, JobContext
from ..models import Agent, CaseResult, Dataset, EvaluationRun, EvidenceChunk, TestCase, utcnow
from ..textutil import contains_fact, dominant_script, is_verifiable_refusal, looks_like_abstention, normalize
from .datasets import HoldoutAccess, cases_for_run

DEFAULT_CONFIG: dict[str, Any] = {
    "pass_threshold": 0.7,
    "disagreement_threshold": 0.3,
    "require_citations": True,
    "repeats": 1,
    "abstain_patterns": [],
    "weights": {"correctness": 0.4, "groundedness": 0.2, "citation_accuracy": 0.15, "relevance": 0.1,
                "abstention": 1.0, "multi_turn_consistency": 0.15, "language": 0.15, "instruction_compliance": 0.1},
    "limits": {},  # max_model_calls, max_model_tokens, max_model_cost, max_agent_calls
}
MAX_REVIEWERS, MAX_TESTERS = 7, 8
MODES = ("deterministic", "single", "dual", "panel")


def reviewer_count(mode: str, config: dict[str, Any] | None) -> int:
    """How many independent model reviewers score each answer: 0 (deterministic), 1, 2, or a panel of 2-7."""
    if mode == "deterministic":
        return 0
    if mode == "single":
        return 1
    if mode == "dual":
        return 2
    return max(2, min(MAX_REVIEWERS, int((config or {}).get("reviewers") or 3)))


def tester_count(config: dict[str, Any] | None) -> int:
    """How many testers call the agent in parallel."""
    return max(1, min(MAX_TESTERS, int((config or {}).get("testers") or 1)))


JUDGE_DIMENSIONS = ["correctness", "groundedness", "relevance", "multi_turn_consistency", "abstention"]
FATAL_MODEL_CODES = {"BUDGET_EXCEEDED", "TIMEOUT", "CONNECTION", "AUTH", "EGRESS_BLOCKED", "MODEL_NOT_CONFIGURED",
                     "PREFLIGHT_REQUIRED", "RATE_LIMITED", "PROVIDER_ERROR"}


class EvaluationError(Exception):
    pass


def merged_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = {**DEFAULT_CONFIG, **(config or {})}
    merged["weights"] = {**DEFAULT_CONFIG["weights"], **((config or {}).get("weights") or {})}
    return merged


def create_run(db: Session, *, workspace_id: str, agent_id: str, dataset_id: str, split: str, mode: str,
               kind: str, agent_version: str = "baseline", config: dict[str, Any] | None = None,
               user_id: str | None = None, optimization_run_id: str | None = None) -> EvaluationRun:
    if mode not in MODES:
        raise EvaluationError("mode must be 'deterministic', 'single', 'dual' or 'panel'")
    dataset = db.get(Dataset, dataset_id)
    agent = db.get(Agent, agent_id)
    if dataset is None or agent is None or dataset.workspace_id != workspace_id or agent.workspace_id != workspace_id:
        raise EvaluationError("agent or dataset not found in this workspace")
    if dataset.status != "ready":
        raise EvaluationError("the dataset is not ready")
    full_config = merged_config(config)
    full_config["reviewers"] = reviewer_count(mode, full_config)
    full_config["testers"] = tester_count(full_config)
    run = EvaluationRun(workspace_id=workspace_id, agent_id=agent_id, dataset_id=dataset_id,
                        dataset_version=dataset.version, split=split, kind=kind, agent_version=agent_version,
                        mode=mode, config=full_config, created_by=user_id,
                        optimization_run_id=optimization_run_id)
    db.add(run)
    db.flush()
    return run


# --- deterministic checks -----------------------------------------------------------------


def dimensions_for(case: TestCase, config: dict[str, Any]) -> list[str]:
    if (case.expected or {}).get("behavior") == "abstain":
        return ["abstention"]
    dims = ["correctness", "groundedness", "relevance"]
    if config.get("require_citations") and case.evidence_refs:
        dims.append("citation_accuracy")
    if len(case.turns or []) > 1:
        dims.append("multi_turn_consistency")
    if case.category == "language_variant":
        dims.append("language")
    return dims


def _names_source(text: str, filename: str) -> bool:
    """True when an answer names a knowledge document in its text (file name, or its name without extension)."""
    name = normalize(filename)
    stem = name.rsplit(".", 1)[0]
    variants = {name, stem, stem.replace("-", " ").replace("_", " ")}
    return any(len(v) >= 5 and v in text for v in variants)


def deterministic_checks(case: TestCase, replies: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Checks that need no model. ``decisive`` marks results that outrank a model judgment."""
    expected = case.expected or {}
    out: dict[str, dict[str, Any]] = {}
    texts = [r.get("text", "") for r in replies]
    final_text = texts[-1] if texts else ""
    patterns = config.get("abstain_patterns") or []

    if expected.get("behavior") == "abstain":
        abstained = looks_like_abstention(final_text, patterns)
        out["abstention"] = {"score": 1.0 if abstained else 0.0, "decisive": abstained,
                             "detail": "the agent declined" if abstained else "no refusal phrase was detected"}
        return out

    reference_usable = case.answer_status in ("approved", "draft")
    facts = list(expected.get("key_facts") or [])
    follow_facts = list(expected.get("follow_up_key_facts") or [])
    if reference_usable and (facts or follow_facts):
        if looks_like_abstention(texts[0] if texts else "", patterns) and facts:
            out["correctness"] = {"score": 0.0, "decisive": True, "detail": "the agent declined an answerable question"}
        else:
            found = [f for f in facts if contains_fact(texts[0] if texts else "", f)]
            missing = [f for f in facts if f not in found]
            if follow_facts and len(texts) > 1:
                follow_found = [f for f in follow_facts if contains_fact(texts[1], f)]
                found += follow_found
                missing += [f for f in follow_facts if f not in follow_found]
            total = len(found) + len(missing)
            basis = "approved reference" if case.answer_status == "approved" else "evidence-linked draft reference"
            # Key facts are written in the language of the documents. For a question asked in another
            # language the wording of a correct answer can differ, so a reviewer decides when there is one.
            out["correctness"] = {"score": round(len(found) / total, 4) if total else 0.0,
                                  "decisive": case.category != "language_variant",
                                  "detail": f"{len(found)}/{total} key facts present ({basis})", "missing": missing}

    if config.get("require_citations") and case.evidence_refs:
        cited = [normalize(c) for r in replies for c in r.get("citations", [])]
        expected_files = {normalize(ref.get("filename", "")) for ref in case.evidence_refs if ref.get("filename")}
        known = {normalize(name) for name in config.get("_known_sources", [])}
        if not cited:
            # Many agents name their source in the answer itself instead of returning a separate list.
            spoken = normalize(" ".join(texts))
            cited = [name for name in sorted(known | expected_files) if name and _names_source(spoken, name)]
        if not cited:
            out["citation_accuracy"] = {"score": 0.0, "decisive": True, "detail": "the answer cites no source"}
        elif any(any(name and name in c for name in expected_files) for c in cited):
            out["citation_accuracy"] = {"score": 1.0, "decisive": True, "detail": "cites the document that holds the evidence"}
        elif known and all(any(name in c for name in known) for c in cited):
            out["citation_accuracy"] = {"score": 0.5, "decisive": True,
                                        "detail": "cites a real knowledge document, but not the one that holds the evidence"}
        else:
            out["citation_accuracy"] = {"score": 0.0, "decisive": True, "detail": "cites a source that is not in the knowledge base"}

    if case.category == "language_variant":
        asked = dominant_script(case.turns[-1]["content"])
        answered = dominant_script(final_text)
        out["language"] = {"score": 1.0 if asked == answered else 0.0, "decisive": True,
                           "detail": f"question script: {asked}; answer script: {answered}"}
    return out


# --- judging ------------------------------------------------------------------------------


def _evidence_texts(db: Session, case: TestCase) -> list[str]:
    ids = [ref.get("chunk_id") for ref in case.evidence_refs or [] if ref.get("chunk_id")]
    if not ids:
        return []
    return [c.text for c in db.scalars(select(EvidenceChunk).where(EvidenceChunk.id.in_(ids)))]


def _judge_inputs(db: Session, case: TestCase, replies: list[dict[str, Any]], dims: list[str]) -> dict[str, Any]:
    conversation = []
    for index, turn in enumerate(case.turns or []):
        conversation.append(turn)
        if index < len(replies):
            conversation.append({"role": "assistant", "content": replies[index].get("text", "")})
    expected = case.expected or {}
    return {
        "turns": conversation, "response": replies[-1].get("text", "") if replies else "",
        "expected": {"behavior": expected.get("behavior", "answer"), "answer": expected.get("answer", ""),
                     "key_facts": expected.get("key_facts", []), "reference_status": case.answer_status},
        "evidence": _evidence_texts(db, case), "dimensions": dims,
        "rubric": {d: RUBRIC[d] for d in dims if d in RUBRIC},
    }


def _judge(db: Session, gateway: Gateway, run: EvaluationRun, case: TestCase, replies: list[dict[str, Any]],
           dims: list[str], config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Returns (recorded judgments, merged view). Every reviewer gets its own independent context."""
    inputs = _judge_inputs(db, case, replies, dims)
    judgments: list[dict[str, Any]] = []

    answer_text = replies[-1].get("text", "") if replies else ""

    def call(task: str, payload: dict[str, Any], reviewer: int | None = None) -> dict[str, Any]:
        result, meta = gateway.generate(task, payload)
        record = {"task": task, "role": meta.role, "model": meta.model, "prompt_version": meta.prompt_version,
                  "synthetic": meta.synthetic, "reviewer": reviewer, **result.model_dump()}
        # A reviewer may claim that the agent declined. That claim only counts when it quotes a
        # sentence that is really in the answer and reads like a refusal; otherwise it is overruled.
        if record["scores"].get("abstention", 0.0) > 0.5 and not is_verifiable_refusal(
                answer_text, record.get("refusal_quote", ""), config.get("abstain_patterns") or []):
            record["scores"]["abstention"] = 0.0
            record["overruled"] = "claimed a refusal without quoting one from the answer"
            if set(dims) == {"abstention"}:
                record["verdict"] = "fail"
        judgments.append(record)
        return record

    reviewers = reviewer_count(run.mode, config)
    # Each reviewer receives the same case, rubric and evidence and nothing from the other reviewers.
    # Reviewers alternate between the two evaluator roles, so two different models can be mixed.
    panel = [call("judge.primary" if index % 2 == 0 else "judge.secondary", inputs, index + 1)
             for index in range(reviewers)]
    first = panel[0]
    merged: dict[str, Any] = {"scores": first["scores"], "verdict": first["verdict"], "confidence": first["confidence"],
                              "agreement": None, "arbitrated": False, "needs_human_review": False,
                              "reviewers": reviewers}
    if reviewers == 1:
        return judgments, merged

    shared = [d for d in dims if all(d in j["scores"] for j in panel)]
    gap = max((max(j["scores"][d] for j in panel) - min(j["scores"][d] for j in panel) for d in shared), default=0.0)
    verdicts = {j["verdict"] for j in panel}
    agree = gap <= float(config["disagreement_threshold"]) and len(verdicts) == 1
    # Critical cases go to the arbiter whenever any reviewer is not confident it passed.
    critical_doubt = case.severity == "critical" and verdicts != {"pass"}
    merged.update({"agreement": agree, "max_gap": round(gap, 4)})
    if agree and not critical_doubt:
        merged["scores"] = {d: round(statistics.fmean(j["scores"][d] for j in panel), 4) for d in shared}
        merged["confidence"] = round(min(j["confidence"] for j in panel), 4)
        return judgments, merged
    decision = call("arbiter", {**inputs, "judgments": [
        {"scores": j["scores"], "verdict": j["verdict"], "rationale": j["rationale"]} for j in panel]})
    merged.update({"scores": decision["scores"], "verdict": decision["verdict"], "confidence": decision["confidence"],
                   "arbitrated": True, "needs_human_review": bool(decision.get("needs_human_review"))})
    return judgments, merged


def finalize_case(case: TestCase, dims: list[str], det: dict[str, dict[str, Any]], judge: dict[str, Any] | None,
                  config: dict[str, Any], error: str | None) -> dict[str, Any]:
    weights = config["weights"]
    scores: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    for dim in dims:
        d, j = det.get(dim), (judge or {}).get("scores", {}).get(dim)
        if error:
            scores[dim] = {"score": 0.0, "method": "agent_error"}
        elif d is not None and d.get("decisive"):
            scores[dim] = {"score": d["score"], "method": "deterministic", "detail": d.get("detail", "")}
            if j is not None and abs(d["score"] - j) >= 0.5:
                conflicts.append(dim)
        elif j is not None:
            scores[dim] = {"score": j, "method": "model_judge"}
        elif d is not None:
            scores[dim] = {"score": d["score"], "method": "deterministic", "detail": d.get("detail", "")}
    primary = "abstention" if (case.expected or {}).get("behavior") == "abstain" else "correctness"
    total_weight = sum(weights.get(dim, 0.1) for dim in scores)
    if primary not in scores or not total_weight:
        # The main attribute could not be observed: report "n/a" rather than a made-up score.
        return {"scores": scores, "overall": None, "verdict": "n/a", "critical_failure": False,
                "needs_human_review": case.answer_status in ("unverified", "needs_human_review"),
                "reason": "no verified reference and no model judge available for the main dimension",
                "conflicts": conflicts}
    overall = round(sum(scores[dim]["score"] * weights.get(dim, 0.1) for dim in scores) / total_weight, 4)
    passed = overall >= float(config["pass_threshold"]) and scores[primary]["score"] >= 0.5
    review = bool((judge or {}).get("needs_human_review")) or bool(conflicts) or case.answer_status == "needs_human_review"
    return {"scores": scores, "overall": overall, "verdict": "pass" if passed else "fail",
            "critical_failure": case.severity == "critical" and not passed, "needs_human_review": review,
            "conflicts": conflicts, "model_estimated": any(s["method"] == "model_judge" for s in scores.values())}


# --- execution ----------------------------------------------------------------------------


def _invoke_case(adapter: AgentAdapter, case: TestCase, version: str) -> tuple[list[dict[str, Any]], int, float | None, str | None]:
    session_id = uuid.uuid4().hex
    messages: list[dict[str, str]] = []
    replies: list[dict[str, Any]] = []
    latency, cost, has_cost = 0, 0.0, False
    for turn in case.turns or []:
        messages.append({"role": turn["role"], "content": turn["content"]})
        try:
            reply = adapter.invoke(messages, version=version, session_id=session_id)
        except AdapterError as exc:
            return replies, latency, cost if has_cost else None, f"{exc.code}: {exc.message}"
        latency += reply.latency_ms
        if reply.cost is not None:
            cost, has_cost = cost + float(reply.cost), True
        replies.append({"text": reply.text, "citations": reply.citations, "latency_ms": reply.latency_ms})
        messages.append({"role": "assistant", "content": reply.text})
    return replies, latency, cost if has_cost else None, None


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarize(db: Session, run: EvaluationRun, results: list[CaseResult], cases: dict[str, TestCase]) -> dict[str, Any]:
    scored = [r for r in results if r.overall is not None]

    def weight(r: CaseResult) -> float:
        return float(cases[r.case_id].weight) if r.case_id in cases else 1.0

    total_w = sum(weight(r) for r in scored)
    by_dim: dict[str, list[tuple[float, str]]] = {}
    by_cat: dict[str, list[CaseResult]] = {}
    for r in results:
        case = cases.get(r.case_id)
        if case is not None:
            by_cat.setdefault(case.category, []).append(r)
        for dim, info in (r.final.get("scores") or {}).items():
            by_dim.setdefault(dim, []).append((info["score"], info["method"]))
    dual = [r.final.get("judge", {}) for r in results if (r.final.get("judge") or {}).get("agreement") is not None]
    latencies = [r.latency_ms for r in results if not r.error]
    agent_costs = [r.agent_cost for r in results if r.agent_cost is not None]
    usage = Gateway(db, run.workspace_id).usage(run_id=run.id)
    caveats = ["Scores describe behaviour on this test set only; they are not a measure of real-world accuracy."]
    if run.synthetic:
        caveats.insert(0, "SYNTHETIC RUN: the fake model provider was used. Judge scores are text-overlap heuristics, "
                          "not a real semantic evaluation.")
    if any(m == "model_judge" for values in by_dim.values() for _, m in values):
        caveats.append("Dimensions scored by a model judge are estimates, not verified facts.")
    reviewers = reviewer_count(run.mode, run.config)
    if reviewers > 1:
        caveats.append("Agreement between reviewers is not ground truth; model reviewers can share the same bias.")
        try:
            gateway = Gateway(db, run.workspace_id)
            first, first_policy = gateway.resolve("evaluator_primary")
            second, second_policy = gateway.resolve("evaluator_secondary")
            if (first.id, first_policy.model or first.default_model) == (second.id, second_policy.model or second.default_model):
                caveats.append("All reviewers ran on the same model (with different briefs). For a genuinely independent "
                               "second opinion, route the evaluator_secondary role to a different model.")
        except GatewayError:
            pass
    if len(scored) < len(results):
        caveats.append(f"{len(results) - len(scored)} case(s) could not be scored (no verified reference or judge).")
    return {
        "cases": len(results), "scored": len(scored), "not_scored": len(results) - len(scored),
        "overall": round(sum((r.overall or 0.0) * weight(r) for r in scored) / total_w, 4) if total_w else None,
        "pass_rate": round(sum(1 for r in scored if r.verdict == "pass") / len(scored), 4) if scored else None,
        "by_dimension": {dim: {"mean": round(sum(s for s, _ in values) / len(values), 4), "n": len(values),
                               "methods": {m: sum(1 for _, mm in values if mm == m) for m in {mm for _, mm in values}}}
                         for dim, values in sorted(by_dim.items())},
        "by_category": {cat: {"n": len(rs), "scored": len([r for r in rs if r.overall is not None]),
                              "mean": round(statistics.fmean([r.overall for r in rs if r.overall is not None]), 4)
                              if any(r.overall is not None for r in rs) else None,
                              "pass_rate": round(sum(1 for r in rs if r.verdict == "pass") / len(rs), 4)}
                        for cat, rs in sorted(by_cat.items())},
        "critical_failures": sum(1 for r in results if r.final.get("critical_failure")),
        "needs_human_review": sum(1 for r in results if r.final.get("needs_human_review")),
        "agent_errors": sum(1 for r in results if r.error),
        "evaluator_agreement": {"compared": len(dual), "agreed": sum(1 for d in dual if d.get("agreement")),
                                "rate": round(sum(1 for d in dual if d.get("agreement")) / len(dual), 4) if dual else None,
                                "arbitrated": sum(1 for d in dual if d.get("arbitrated"))} if reviewers > 1 else None,
        "reviewers": reviewers, "testers": tester_count(run.config),
        "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95),
                       "mean": int(statistics.fmean(latencies)) if latencies else 0},
        "agent": {"calls": sum(len(r.response.get("turns", [])) or 1 for r in results),
                  "estimated_cost": round(sum(agent_costs), 6) if agent_costs else None,
                  "cost_note": "Sum of the per-call cost declared on the agent connection."
                  if agent_costs else "No per-call cost is declared for this agent."},
        "model_usage": usage,
        "reference_basis": {status: sum(1 for r in results if r.case_id in cases and cases[r.case_id].answer_status == status)
                            for status in sorted({c.answer_status for c in cases.values()})},
        "caveats": caveats,
    }


def execute_run(db: Session, run_id: str, *, ctx: JobContext | None = None,
                access: HoldoutAccess | None = None) -> EvaluationRun:
    """Run every case. Commits after each case so progress and usage survive a later failure."""
    run = db.get(EvaluationRun, run_id)
    if run is None:
        raise EvaluationError("run not found")
    agent = db.get(Agent, run.agent_id)
    assert agent is not None
    config = merged_config(run.config)
    limits = config.get("limits") or {}
    gateway = Gateway(db, run.workspace_id, run_id=run.id, scope_id=run.optimization_run_id or run.id, limits=limits)

    def fail(message: str) -> EvaluationRun:
        run.status, run.error, run.finished_at = "failed", message, utcnow()
        db.commit()
        return run

    try:
        cases = cases_for_run(db, run.dataset_id, run.split, access)
    except PermissionError as exc:
        return fail(str(exc))
    if not cases:
        return fail(f"the dataset has no {run.split} cases")
    if run.mode != "deterministic":
        # Fail before touching the agent: model-dependent steps never start without a verified model.
        roles = ["evaluator_primary"] + (["evaluator_secondary", "arbiter"] if reviewer_count(run.mode, config) > 1 else [])
        for role in roles:
            try:
                provider, _ = gateway.resolve(role)
            except GatewayError as exc:
                return fail(f"{exc.code}: {exc.message}. Deterministic mode can run without a model.")
            if not (provider.last_preflight and provider.last_preflight.get("ok")):
                return fail(f"PREFLIGHT_REQUIRED: provider '{provider.name}' (role {role}) has not passed preflight")
        run.synthetic = any(gateway.is_synthetic(role) for role in roles)
    if "invoke" not in (agent.capabilities or []):
        return fail("the agent has not been verified; verify the connection first")

    from ..models import Document  # local import to avoid a cycle at module load
    config["_known_sources"] = [name for (name,) in db.execute(
        select(Document.filename).where(Document.workspace_id == run.workspace_id, Document.kind == "kb"))]
    try:
        adapter = build_adapter(db, agent)
    except AdapterError as exc:
        return fail(f"{exc.code}: {exc.message}")

    run.status = "running"
    db.commit()
    if ctx:
        ctx.progress(run_id=run.id, total=len(cases), done=0)
    repeats = max(1, int(config.get("repeats", 1)))
    testers = tester_count(config)
    max_agent_calls = limits.get("max_agent_calls")
    agent_calls, consecutive_errors, stop_error = 0, 0, None
    done_ids = {r.case_id for r in db.scalars(select(CaseResult).where(CaseResult.run_id == run.id))}
    pending = [c for c in cases if c.id not in done_ids]
    completed, position = len(cases) - len(pending), 0

    def test(case: TestCase) -> list[tuple[list[dict[str, Any]], int, float | None, str | None]]:
        return [_invoke_case(adapter, case, run.agent_version) for _ in range(repeats)]

    with ThreadPoolExecutor(max_workers=testers, thread_name_prefix="kaizeval-tester") as pool:
        while position < len(pending) and not stop_error:
            if ctx and ctx.cancelled():
                run.status, run.finished_at = "cancelled", utcnow()
                db.commit()
                raise JobCancelled()
            batch: list[TestCase] = []
            for case in pending[position:position + testers]:
                turns = len(case.turns or []) or 1
                if max_agent_calls is not None and agent_calls + turns * repeats > max_agent_calls:
                    stop_error = f"BUDGET_EXCEEDED: agent call budget reached ({max_agent_calls})"
                    break
                agent_calls += turns * repeats
                batch.append(case)
            position += len(batch)
            if not batch:
                break
            # Testers call the agent independently and in parallel. Only when their answers are in
            # do the reviewers score them, so no reviewer output can influence a tester.
            answered = list(pool.map(test, batch))
            for case, raw_attempts in zip(batch, answered, strict=True):
                attempts: list[dict[str, Any]] = []
                shown: dict[str, Any] | None = None
                for replies, latency, cost, error in raw_attempts:
                    dims = dimensions_for(case, config)
                    det = {} if error else deterministic_checks(case, replies, config)
                    judgments: list[dict[str, Any]] = []
                    judge_view: dict[str, Any] | None = None
                    judge_error = None
                    wanted = [d for d in dims if d in JUDGE_DIMENSIONS and not (det.get(d) or {}).get("decisive")]
                    if run.mode != "deterministic" and not error and wanted:
                        try:
                            judgments, judge_view = _judge(db, gateway, run, case, replies, wanted, config)
                        except GatewayError as exc:
                            if exc.code in FATAL_MODEL_CODES:
                                stop_error = f"{exc.code}: {exc.message}"
                                break
                            judge_error = f"{exc.code}: {exc.message}"
                    final = finalize_case(case, dims, det, judge_view, config, error)
                    if judge_view:
                        final["judge"] = {k: judge_view[k] for k in ("agreement", "arbitrated", "confidence", "verdict", "reviewers")
                                          if k in judge_view}
                    if judge_error:
                        final["judge_error"], final["needs_human_review"] = judge_error, True
                    attempt = {"replies": replies, "latency": latency, "cost": cost, "error": error, "det": det,
                               "judgments": judgments, "final": final}
                    attempts.append(attempt)
                    shown = shown or attempt
                if stop_error:
                    break
                assert shown is not None
                final = dict(shown["final"])
                overalls = [a["final"]["overall"] for a in attempts if a["final"]["overall"] is not None]
                if repeats > 1 and overalls:
                    final["overall"] = round(statistics.fmean(overalls), 4)
                    final["attempt_overalls"] = overalls
                    final["verdict"] = "pass" if final["overall"] >= float(config["pass_threshold"]) else "fail"
                    final["critical_failure"] = case.severity == "critical" and final["verdict"] == "fail"
                verdict = "needs_human_review" if final.get("needs_human_review") and final["verdict"] != "n/a" and (
                    (shown["final"].get("judge") or {}).get("verdict") == "uncertain") else final["verdict"]
                db.add(CaseResult(
                    workspace_id=run.workspace_id, run_id=run.id, case_id=case.id, split=run.split,
                    response={"turns": shown["replies"], "text": shown["replies"][-1]["text"] if shown["replies"] else "",
                              "citations": [c for r in shown["replies"] for c in r["citations"]]},
                    latency_ms=int(statistics.fmean([a["latency"] for a in attempts])),
                    agent_cost=sum(a["cost"] for a in attempts if a["cost"] is not None)
                    if any(a["cost"] is not None for a in attempts) else None,
                    error=shown["error"], deterministic=shown["det"], judgments=shown["judgments"], final=final,
                    overall=final["overall"], verdict=verdict,
                ))
                consecutive_errors = consecutive_errors + 1 if shown["error"] else 0
                db.commit()
                completed += 1
                if ctx:
                    ctx.progress(done=completed)
                if consecutive_errors >= 5:
                    stop_error = f"the agent failed 5 times in a row ({shown['error']})"
                    break
    adapter.close()

    results = list(db.scalars(select(CaseResult).where(CaseResult.run_id == run.id)))
    summary = summarize(db, run, results, {c.id: c for c in cases})
    if stop_error:
        run.status, run.error = "failed", stop_error
        summary["partial"] = True
    else:
        run.status = "completed"
    run.summary = summary
    run.agent_config_digest = _digest(db, agent, run.agent_version)
    run.finished_at = utcnow()
    audit.record(db, workspace_id=run.workspace_id, user_id=run.created_by, action=f"evaluation.{run.status}",
                 subject_type="evaluation_run", subject_id=run.id,
                 detail={"split": run.split, "kind": run.kind, "mode": run.mode, "agent_version": run.agent_version,
                         "overall": run.summary.get("overall"), "synthetic": run.synthetic})
    db.commit()
    return run


def _digest(db: Session, agent: Agent, version: str) -> str | None:
    if "inspect" not in (agent.capabilities or []):
        return None
    try:
        return build_adapter(db, agent).inspect(version).get("digest")
    except AdapterError:
        return None
