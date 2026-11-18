"""Exercise every model-assisted step of Kaizeval against a REAL OpenAI-compatible model.

The automated test-suite uses a fake provider and a mock server. This script is the complement:
it calls a real model for every gateway role, then runs dataset generation, the three review
modes and an optimization loop, and writes what happened (validity, retries, latency, sanity
checks) to a JSON file. Nothing here asserts quality; it records it so you can judge your model.

    python scripts/real_model_check.py --base-url http://localhost:11434/v1 --model qwen2.5:7b

Options: --stages roles,dataset,review,optimize (default: all)   --out real-model-check.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DATA = tempfile.mkdtemp(prefix="kaizeval-realcheck-")
os.environ.update({"KAIZEVAL_DATA_DIR": DATA, "KAIZEVAL_OFFLINE": "true", "KAIZEVAL_WORKER_MODE": "inline",
                   "KAIZEVAL_LOG_LEVEL": "WARNING"})

import uvicorn  # noqa: E402
from sqlalchemy import select  # noqa: E402

from kaizeval.app import create_app  # noqa: E402
from kaizeval.client import Api, ApiError  # noqa: E402
from kaizeval.db import session_scope  # noqa: E402
from kaizeval.gateway import Gateway, GatewayError  # noqa: E402
from kaizeval.models import ModelInvocation  # noqa: E402
from kaizeval.sample_agent import KB_DIR, create_sample_agent  # noqa: E402
from kaizeval.textutil import dominant_script, normalize  # noqa: E402

ADMIN = "realcheck-" + uuid.uuid4().hex[:8]
REFUND = ("Customers may return an unused appliance within 30 days of the delivery date for a full refund. "
          "Opened but undamaged items can be returned within 14 days and are subject to a 10% restocking fee. "
          "Items returned after these periods are not eligible for a refund.")
EXPRESS = ("Express delivery arrives within 2 business days and costs 45 USD per order. Express orders must be "
           "placed before 14 local time to be dispatched the same day.")


def serve(app: Any, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stage_roles(ws: str, out: dict[str, Any]) -> None:
    """Call each gateway task once with realistic input and record validity, attempts and latency."""
    chunks = [{"ref": "chunk:refund", "section": "Returns > Refund window", "document": "returns.md", "text": REFUND},
              {"ref": "chunk:express", "section": "Shipping > Express delivery", "document": "shipping.md", "text": EXPRESS}]
    judge = {"turns": [{"role": "user", "content": "How long do I have to return an unused appliance?"}],
             "expected": {"behavior": "answer", "answer": "Within 30 days of delivery for a full refund.",
                          "key_facts": ["30 days"], "reference_status": "draft"},
             "evidence": [REFUND], "dimensions": ["correctness", "groundedness", "relevance"],
             "rubric": {"correctness": "States the expected facts accurately.",
                        "groundedness": "Every claim is supported by the evidence.", "relevance": "Addresses the question."}}
    good = {**judge, "response": "You can return an unused appliance within 30 days of the delivery date for a full refund."}
    bad = {**judge, "response": "You have 90 days, and we also pay you a 20% bonus for returning it."}
    schema = {"system_prompt": {"type": "string", "max_length": 2000, "addresses": ["instruction_prompt", "citation"]},
              "top_k": {"type": "integer", "min": 1, "max": 6, "addresses": ["retrieval_grounding"]},
              "return_sources": {"type": "boolean", "addresses": ["citation"]}}
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("spec.infer", "spec.infer", {"requirements": "Always cite the source document.\nDecline when the documents do not contain the answer.",
                                      "chunks": chunks, "agent_profile": None}),
        ("generate.questions", "generate.questions", {"chunks": chunks, "per_chunk": 2, "categories": ["knowledge", "citation", "edge_case"],
                                                      "include_multi_turn": True, "variation": 0,
                                                      "persona": "a sceptical auditor who wants the exact source, numbers and conditions"}),
        ("generate.unanswerable", "generate.unanswerable", {"topics": ["Refund window", "Express delivery", "Warranty period"], "n": 3}),
        ("generate.variants", "generate.variants", {"cases": [{"case_ref": "q0", "question": "How long do I have to return an unused appliance?"}],
                                                    "languages": ["Egyptian Arabic", "French"]}),
        ("reference.draft", "reference.draft", {"items": [
            {"ref": "q0", "question": "How long do I have to return an unused appliance?", "chunk_text": REFUND, "is_follow_up": False},
            {"ref": "q1", "question": "What is the CEO's favourite colour?", "chunk_text": REFUND, "is_follow_up": False}]}),
        ("judge.primary (good answer)", "judge.primary", good),
        ("judge.primary (wrong answer)", "judge.primary", bad),
        ("judge.secondary (good answer)", "judge.secondary", good),
        ("root_cause", "root_cause", {"failures": [{"case_ref": "r1", "question": "What is the CEO's home address?",
                                                    "response": "The CEO lives at 12 Main Street.",
                                                    "signals": {"expected_behavior": "abstain", "abstained": False}, "rule_category": "instruction_prompt"}],
                                      "categories": ["instruction_prompt", "retrieval_grounding", "citation", "unknown"]}),
        ("optimize", "optimize", {"config_schema": schema,
                                  "current_config": {"system_prompt": "You are a helpful assistant.", "top_k": 1, "return_sources": False},
                                  "failure_summary": {"by_category": {"citation": 8, "instruction_prompt": 4}, "overall": 0.42,
                                                      "examples": [{"category": "instruction_prompt", "question": "What is the CEO's home address?",
                                                                    "observation": "The agent answered a question that the knowledge base does not cover."}]},
                                  "history": []}),
    ]
    results: dict[str, Any] = {}
    for label, task, inputs in calls:
        run_id = f"roles-{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        entry: dict[str, Any] = {"task": task}
        try:
            with session_scope() as db:
                output, _meta = Gateway(db, ws, run_id=run_id).generate(task, inputs)
                entry.update({"ok": True, "output": output.model_dump()})
        except GatewayError as exc:
            entry.update({"ok": False, "error": f"{exc.code}: {exc.message}"})
        with session_scope() as db:
            rows = list(db.scalars(select(ModelInvocation).where(ModelInvocation.run_id == run_id)))
            entry.update({"attempts": len(rows), "invalid_attempts": sum(1 for r in rows if r.status != "ok"),
                          "seconds": round(time.perf_counter() - started, 1),
                          "prompt_tokens": sum(r.prompt_tokens for r in rows), "completion_tokens": sum(r.completion_tokens for r in rows)})
        results[label] = entry
        print(f"  {label:<32} ok={entry['ok']} attempts={entry['attempts']} {entry['seconds']}s", flush=True)

    def out_of(label: str) -> dict[str, Any]:
        return results[label].get("output") or {}

    sanity: dict[str, Any] = {}
    g, b = out_of("judge.primary (good answer)").get("scores", {}), out_of("judge.primary (wrong answer)").get("scores", {})
    if g and b:
        sanity["judge_scores_good_answer"], sanity["judge_scores_wrong_answer"] = g, b
        sanity["judge_separates_good_from_wrong"] = g.get("correctness", 0) - b.get("correctness", 1) >= 0.4
    drafts = {d["ref"]: d for d in out_of("reference.draft").get("drafts", [])}
    if drafts:
        q0, q1 = drafts.get("q0", {}), drafts.get("q1", {})
        sanity["reference_quote_is_verbatim"] = bool(q0.get("evidence_quote")) and normalize(q0.get("evidence_quote", "")) in normalize(REFUND)
        sanity["reference_key_facts_verbatim"] = [f for f in q0.get("key_facts", []) if normalize(f) in normalize(REFUND)]
        sanity["reference_admits_insufficient_evidence"] = q1.get("sufficient") is False
    questions = out_of("generate.questions").get("questions", [])
    sanity["questions_generated"] = len(questions)
    sanity["questions_with_valid_chunk_ref"] = sum(1 for q in questions if q.get("chunk_ref") in ("chunk:refund", "chunk:express"))
    variants = out_of("generate.variants").get("variants", [])
    sanity["variants"] = [{"language": v.get("language"), "script": dominant_script(v.get("question", "")), "question": v.get("question")}
                          for v in variants]
    proposal = out_of("optimize")
    sanity["optimizer_patch_keys"] = sorted((proposal.get("patch") or {}).keys())
    sanity["optimizer_patch_within_schema"] = set((proposal.get("patch") or {}).keys()) <= set(schema)
    out["roles"] = {"calls": results, "sanity": sanity}


def stage_dataset(api: Api, ws: str, agent_id: str, out: dict[str, Any], cases: int) -> str:
    started = time.perf_counter()
    spec_job = api.wait_job(ws, api.call("POST", f"/workspaces/{ws}/specifications:build", json={
        "agent_id": agent_id, "requirements_text": "Always cite the source document.\nDecline when the documents do not contain the answer.",
        "infer": True}))
    spec = api.call("GET", f"/workspaces/{ws}/specifications/{spec_job['result']['specification_id']}")
    generated = api.call("POST", f"/workspaces/{ws}/datasets:generate", json={
        "name": "real-model tests", "agent_id": agent_id, "specification_id": spec["id"], "n_cases": cases, "testers": 2,
        "holdout_fraction": 0.25, "languages": ["Egyptian Arabic"]})
    api.wait_job(ws, generated["job"])
    dataset_id = generated["dataset"]["id"]
    coverage = api.call("GET", f"/workspaces/{ws}/datasets/{dataset_id}/coverage")
    sample = api.call("GET", f"/workspaces/{ws}/datasets/{dataset_id}/cases", params={"limit": 200})["items"]
    out["dataset"] = {
        "seconds": round(time.perf_counter() - started, 1),
        "specification": {"source": spec["source"], "objectives": len(spec["content"].get("objectives", [])),
                          "inferred_with_evidence": sum(1 for o in spec["content"].get("objectives", [])
                                                        if o.get("source") == "inferred" and o.get("evidence_refs")),
                          "example_objectives": [o["statement"] for o in spec["content"].get("objectives", [])[:4]]},
        "regression": coverage["regression"], "holdout_count": coverage["holdout"]["count"], "notes": coverage["notes"],
        "example_cases": [{"category": c["category"], "tester": c["provenance"].get("tester"), "question": c["turns"][0]["content"],
                           "key_facts": c["expected"].get("key_facts"), "status": c["answer_status"]} for c in sample[:8]],
    }
    print(f"  dataset: {coverage['regression']['count']} regression, {coverage['holdout']['count']} holdout, "
          f"status {coverage['regression']['by_answer_status']} in {out['dataset']['seconds']}s", flush=True)
    return dataset_id


def stage_review(api: Api, ws: str, agent_id: str, dataset_id: str, out: dict[str, Any]) -> None:
    out["review"] = {}
    for mode, config in (("single", {}), ("dual", {}), ("panel", {"reviewers": 3, "testers": 3})):
        started = time.perf_counter()
        try:
            run = api.call("POST", f"/workspaces/{ws}/evaluation-runs", json={"agent_id": agent_id, "dataset_id": dataset_id,
                                                                              "mode": mode, "config": config})
            api.wait_job(ws, run["job"])
        except ApiError as exc:
            out["review"][mode] = {"error": str(exc)}
            print(f"  {mode}: FAILED {exc}", flush=True)
            continue
        done = api.call("GET", f"/workspaces/{ws}/evaluation-runs/{run['run']['id']}")
        summary = done["summary"]
        usage = summary["model_usage"]
        with session_scope() as db:
            rows = list(db.scalars(select(ModelInvocation).where(ModelInvocation.run_id == done["id"])))
        latencies = [r.latency_ms for r in rows if r.status == "ok"]
        out["review"][mode] = {
            "seconds": round(time.perf_counter() - started, 1), "status": done["status"], "overall": summary["overall"],
            "pass_rate": summary["pass_rate"], "by_dimension": {k: v["mean"] for k, v in summary["by_dimension"].items()},
            "agreement": summary.get("evaluator_agreement"), "needs_human_review": summary["needs_human_review"],
            "model_calls": usage["calls"], "invalid_outputs": sum(1 for r in rows if r.status == "invalid_output"),
            "errors": sum(1 for r in rows if r.status == "error"),
            "median_call_seconds": round(statistics.median(latencies) / 1000, 1) if latencies else None,
            "tokens": usage["prompt_tokens"] + usage["completion_tokens"], "synthetic": done["synthetic"],
        }
        print(f"  {mode}: overall {summary['overall']} calls {usage['calls']} invalid "
              f"{out['review'][mode]['invalid_outputs']} in {out['review'][mode]['seconds']}s", flush=True)


def stage_optimize(api: Api, ws: str, agent_id: str, dataset_id: str, out: dict[str, Any]) -> None:
    started = time.perf_counter()
    api.call("PATCH", f"/workspaces/{ws}", json={"settings": {"allow_autonomous_optimization": True}})
    try:
        opt = api.call("POST", f"/workspaces/{ws}/optimization-runs", json={
            "agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous", "policy": {"eval_mode": "single", "target_score": 1.0}})
        api.wait_job(ws, opt["job"], timeout=7200)
    except ApiError as exc:
        out["optimize"] = {"error": str(exc)}
        print(f"  optimize: FAILED {exc}", flush=True)
        return
    report = api.call("GET", f"/workspaces/{ws}/reports/{opt['optimization_run']['id']}")
    out["optimize"] = {
        "seconds": round(time.perf_counter() - started, 1), "status": report["status"], "stop_reason": report["stop_reason"],
        "rounds": report["rounds"], "baseline_overall": (report["baseline"] or {}).get("overall"),
        "best_overall": (report["best"] or {}).get("overall"),
        "changes": [{"round": c["round"], "patch": c["patch"], "state": c["state"], "rationale": c["rationale"][:200],
                     "reasons": (c.get("outcome") or {}).get("reasons")} for c in report["changes"]],
        "regression_ab": (report.get("regression_comparison") or {}).get("overall"),
        "holdout": {k: (report.get("holdout") or {}).get(k) for k in ("status", "reason", "paired_cases", "overall")},
        "remaining_recommendations": [r["title"] for r in report.get("remaining_recommendations", [])],
        "synthetic": report["synthetic"], "model_calls": report["usage"]["platform_models"]["calls"],
    }
    print(f"  optimize: {report['status']} ({report['stop_reason']}), {out['optimize']['baseline_overall']} -> "
          f"{out['optimize']['best_overall']} in {out['optimize']['seconds']}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--stages", default="roles,dataset,review,optimize")
    parser.add_argument("--cases", type=int, default=12)
    parser.add_argument("--out", default="real-model-check.json")
    args = parser.parse_args()
    stages = set(args.stages.split(","))
    out: dict[str, Any] = {"model": args.model, "base_url": args.base_url, "started": time.strftime("%Y-%m-%d %H:%M:%S")}

    api_port, agent_port = free_port(), free_port()
    serve(create_app(), api_port)
    serve(create_sample_agent(admin_token=ADMIN), agent_port)
    api = Api(f"http://127.0.0.1:{api_port}")
    api.token = api.call("POST", "/auth/signup", json={"email": "check@example.test", "password": uuid.uuid4().hex, "name": "check"})["token"]
    ws = api.call("POST", "/workspaces", json={"name": "real model check", "mode": "offline"})["id"]

    print("preflight", flush=True)
    provider = api.call("POST", f"/workspaces/{ws}/model-providers", json={
        "name": "real", "kind": "openai_compatible", "base_url": args.base_url, "default_model": args.model,
        "api_key": os.environ.get(args.api_key_env) if args.api_key_env else None})
    preflight = api.call("POST", f"/workspaces/{ws}/model-providers/{provider['id']}/verify")
    out["preflight"] = preflight
    for check in preflight["checks"]:
        print(f"  {check['name']:<18} ok={check['ok']}  {check['detail']}", flush=True)
    if not preflight["ok"]:
        Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return 2
    api.call("PUT", f"/workspaces/{ws}/model-role-policies/default", json={"provider_id": provider["id"], "timeout_s": 300})

    agent = api.call("POST", f"/workspaces/{ws}/agents", json={
        "name": "sample", "adapter": "local_config", "config": {"base_url": f"http://127.0.0.1:{agent_port}"},
        "credential": ADMIN, "access_level": "candidate_write", "allow_promote": True})
    api.call("POST", f"/workspaces/{ws}/agents/{agent['id']}/verify")
    for path in sorted(KB_DIR.glob("*.md")):
        api.call("POST", f"/workspaces/{ws}/documents", files={"file": (path.name, path.read_bytes())}, data={"kind": "kb"})

    dataset_id = None
    try:
        if "roles" in stages:
            print("roles", flush=True)
            stage_roles(ws, out)
        if stages & {"dataset", "review", "optimize"}:
            print("dataset", flush=True)
            dataset_id = stage_dataset(api, ws, agent["id"], out, args.cases)
        if "review" in stages and dataset_id:
            print("review", flush=True)
            stage_review(api, ws, agent["id"], dataset_id, out)
        if "optimize" in stages and dataset_id:
            print("optimize", flush=True)
            stage_optimize(api, ws, agent["id"], dataset_id, out)
    except ApiError as exc:
        out["fatal"] = str(exc)
        print(f"FATAL {exc}", flush=True)
    finally:
        out["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
