"""Customer journey through the API: workspace, agent, spec, dataset, evaluation, recommendations (acceptance 1, 3, 4, 6, 7, 10, 11)."""

from __future__ import annotations

from sqlalchemy import select

from conftest import (
    add_member,
    generate_dataset,
    new_user,
    run_eval,
    setup_fake_model,
    setup_sample_agent,
    upload_kb,
)
from kaizeval.db import session_scope
from kaizeval.models import EvaluationRun, Secret
from kaizeval.services import diagnostics
from kaizeval.services.datasets import HoldoutAccess, holdout_cases
from kaizeval.services.evaluation import execute_run

# --- identity, roles and isolation --------------------------------------------------------


def test_signup_login_and_system_info(client):
    s = new_user(client)
    me = s.get("/auth/me")
    assert me["user"]["email"] == s.email and "password_hash" not in me["user"]
    assert me["user"]["created_at"].endswith("+00:00")  # timestamps always carry an explicit UTC offset
    assert me["workspaces"][0]["role"] == "owner" and me["workspaces"][0]["effective_offline"] is True
    assert client.post("/api/v1/auth/login", json={"email": s.email, "password": "wrong-password"}).status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401
    system = client.get("/api/v1/system").json()
    assert system["telemetry"] == "none" and system["instance_offline"] is True and len(system["model_roles"]) == 8
    assert client.get("/healthz").json()["status"] == "ok" and client.get("/readyz").status_code == 200


def test_roles_are_enforced(client, sample_agent):
    owner = new_user(client)
    viewer, reviewer, editor = (add_member(owner, role) for role in ("viewer", "reviewer", "editor"))
    assert viewer.get("/ws/agents")["items"] == []
    denied = viewer.post("/ws/agents", expect=403, json={"name": "a", "adapter": "http_chat", "config": {"url": sample_agent["url"]}})
    assert denied["error"]["code"] == "ACCESS_DENIED"
    reviewer.post("/ws/model-providers", expect=403, json={"name": "fake", "kind": "fake"})
    editor.post("/ws/model-providers", expect=403, json={"name": "fake", "kind": "fake"})  # secrets/egress: owner only
    # an editor may register a black-box agent, but only an owner may grant write or promotion scopes
    editor.post("/ws/agents", json={"name": "bb", "adapter": "http_chat", "config": {"url": sample_agent["url"] + "/chat"}})
    editor.post("/ws/agents", expect=403, json={"name": "w", "adapter": "local_config", "config": {"base_url": sample_agent["url"]},
                                                 "access_level": "candidate_write"})
    viewer.get("/ws/audit", expect=403)
    assert reviewer.get("/ws/audit")["total"] >= 1
    owner.put("/ws/members", expect=409, json={"email": owner.email, "role": "viewer"})  # last owner is protected


def test_workspaces_are_isolated(client, sample_agent):
    a, b = new_user(client), new_user(client)
    setup_fake_model(a)
    agent_id = setup_sample_agent(a, sample_agent)
    document_id = upload_kb(a)[0]
    dataset_id = generate_dataset(a, agent_id, n_cases=8)
    run = run_eval(a, agent_id, dataset_id, mode="deterministic")
    # b is not a member of a's workspace: the workspace itself is invisible
    b.get(f"/workspaces/{a.ws}/agents", expect=404)
    # b cannot reach a's objects through b's own workspace either
    for path in (f"/ws/agents/{agent_id}", f"/ws/documents/{document_id}/chunks", f"/ws/datasets/{dataset_id}",
                 f"/ws/datasets/{dataset_id}/cases", f"/ws/evaluation-runs/{run['id']}", f"/ws/reports/{run['id']}"):
        b.get(path, expect=404)
    b.post("/ws/evaluation-runs", expect=404, json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "deterministic"})
    assert b.get("/ws/agents")["items"] == [] and b.get("/ws/datasets")["items"] == []


def test_credentials_are_never_returned(client, sample_agent):
    s = new_user(client)
    agent_id = setup_sample_agent(s, sample_agent)
    agent = s.get(f"/ws/agents/{agent_id}")
    assert agent["has_credential"] is True and sample_agent["token"] not in str(agent)
    assert sample_agent["token"] not in str(s.get("/ws/audit"))
    with session_scope() as db:
        assert all(sample_agent["token"] not in row.ciphertext for row in db.scalars(select(Secret).where(Secret.workspace_id == s.ws)))


# --- agent connection and capabilities ----------------------------------------------------


def test_agent_capabilities_reflect_adapter_and_grant(client, sample_agent):
    s = new_user(client)
    write_id = setup_sample_agent(s, sample_agent)
    caps = s.get(f"/ws/agents/{write_id}/capabilities")
    assert caps["modes"] == {"recommendations_only": True, "human_guided_optimization": True,
                             "autonomous_optimization": False, "promotion": True}
    fresh = s.post("/ws/agents", json={"name": "unverified", "adapter": "http_chat", "config": {"url": sample_agent["url"] + "/chat"}})
    assert all(r["reason"].startswith("NOT_VERIFIED") for r in s.get(f"/ws/agents/{fresh['id']}/capabilities")["matrix"])
    read_id = setup_sample_agent(s, sample_agent, access_level="read_only", allow_promote=False)
    read_caps = s.get(f"/ws/agents/{read_id}/capabilities")
    assert read_caps["capabilities"] == ["inspect", "invoke"] and read_caps["modes"]["human_guided_optimization"] is False
    reason = next(r["reason"] for r in read_caps["matrix"] if r["capability"] == "apply_patch")
    assert reason.startswith("INSUFFICIENT_PERMISSION")
    assert s.get(f"/ws/agents/{read_id}/configuration")["config"]["top_k"] == 1

    for style, path in (("simple", "/chat"), ("openai", "/v1/chat/completions")):
        black = s.post("/ws/agents", json={"name": f"bb-{style}", "adapter": "http_chat",
                                           "config": {"url": sample_agent["url"] + path, "style": style}})
        verified = s.post(f"/ws/agents/{black['id']}/verify")
        assert verified["capabilities"] == ["invoke"]
        matrix = {r["capability"]: r for r in verified["matrix"]}
        assert matrix["apply_patch"]["reason"].startswith("UNSUPPORTED_CAPABILITY")
        s.get(f"/ws/agents/{black['id']}/configuration", expect=409)
    refused = s.post("/ws/agents", expect=409, json={"name": "x", "adapter": "http_chat", "access_level": "candidate_write",
                                                      "config": {"url": sample_agent["url"] + "/chat"}})
    assert refused["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    unreachable = s.post("/ws/agents", json={"name": "down", "adapter": "http_chat", "config": {"url": "http://127.0.0.1:9/chat"}})
    assert s.post(f"/ws/agents/{unreachable['id']}/verify", expect=502)["error"]["code"] == "CONNECTION_ERROR"
    s.post("/ws/agents", expect=409, json={"name": "ext", "adapter": "http_chat", "config": {"url": "https://agent.example.com/chat"}})


# --- specification ------------------------------------------------------------------------


def test_specification_review_edit_skip_and_versions(ready):
    s = ready["s"]
    job = s.post("/ws/specifications:build", json={"agent_id": ready["agent_id"], "infer": True,
                                                   "requirements_text": "Always cite the source document\nNever reveal customer data"})
    assert job["status"] == "completed"
    spec = s.get(f"/ws/specifications/{job['result']['specification_id']}")
    sources = {o["source"] for o in spec["content"]["objectives"]}
    assert sources == {"requirement", "inferred"} and spec["source"] == "mixed" and spec["status"] == "draft"
    assert spec["content"]["provisional"] is True and spec["synthetic"] is True and spec["content"]["uncertainty"]
    inferred = [o for o in spec["content"]["objectives"] if o["source"] == "inferred"]
    assert all(o["evidence_refs"] and o["confidence"] < 0.9 for o in inferred)

    skipped = s.post(f"/ws/specifications/{spec['id']}/review", json={"decision": "skip"})
    assert skipped["status"] == "skipped" and skipped["review"]["decision"] == "skip" and skipped["content"]["provisional"] is True

    edited = s.patch(f"/ws/specifications/{spec['id']}", json={"content": {"purpose": "Answer support questions"}, "approve": True})
    assert edited["version"] == 2 and edited["status"] == "reviewed" and edited["content"]["provisional"] is False
    history = s.get(f"/ws/specifications/{edited['id']}")["versions"]
    assert [v["version"] for v in history] == [1, 2] and history[0]["status"] == "skipped"


def test_requirements_work_without_a_model_but_inference_does_not(client):
    s = new_user(client)
    job = s.post("/ws/specifications:build", json={"requirements_text": "Answer in the user's language; Cite sources", "infer": False})
    spec = s.get(f"/ws/specifications/{job['result']['specification_id']}")
    assert spec["source"] == "business" and len(spec["content"]["objectives"]) == 2 and spec["synthetic"] is False
    failed = s.post("/ws/specifications:build", json={"infer": True})
    assert failed["status"] == "failed" and "MODEL_NOT_CONFIGURED" in failed["error"]


# --- datasets -----------------------------------------------------------------------------


def test_dataset_generation_split_and_coverage(ready):
    s, dataset_id = ready["s"], ready["dataset_id"]
    dataset = s.get(f"/ws/datasets/{dataset_id}")
    assert dataset["status"] == "ready" and dataset["synthetic"] is True
    assert dataset["counts"]["regression"] >= 10 and dataset["counts"]["holdout"] >= 3
    cases = s.get(f"/ws/datasets/{dataset_id}/cases", params={"limit": 200})["items"]
    categories = {c["category"] for c in cases}
    assert {"knowledge", "citation", "unanswerable"} <= categories
    assert any(len(c["turns"]) == 2 for c in cases)
    for case in cases:
        assert case["split"] == "regression" and case["provenance"]["origin"] == "generated"
        if case["category"] == "unanswerable":
            assert case["expected"]["behavior"] == "abstain" and case["evidence_refs"] == []
        else:
            ref = case["evidence_refs"][0]
            assert ref["filename"].endswith(".md") and len(ref["sha256"]) == 64 and ref["quote"] and ref["section"]
            assert case["answer_status"] in ("draft", "unverified", "needs_human_review")
            if case["answer_status"] == "draft":
                chunk = s.get(f"/ws/evidence/{ref['chunk_id']}")
                assert all(fact in chunk["text"] for fact in case["expected"]["key_facts"])
    coverage = s.get(f"/ws/datasets/{dataset_id}/coverage")
    assert coverage["holdout"]["sealed"] is True and coverage["holdout"]["count"] == dataset["counts"]["holdout"]
    assert "by_document" in coverage["regression"] and coverage["limits"]
    assert "question" not in str(coverage["holdout"])


def test_holdout_is_sealed(ready):
    s, dataset_id = ready["s"], ready["dataset_id"]
    sealed = s.get(f"/ws/datasets/{dataset_id}/cases", params={"split": "holdout"}, expect=403)
    assert sealed["error"]["code"] == "HOLDOUT_SEALED"
    add_member(s, "viewer").get(f"/ws/datasets/{dataset_id}/cases", params={"split": "holdout", "acknowledge": True}, expect=403)
    assert s.get(f"/ws/datasets/{dataset_id}")["holdout_exposed"] is False
    try:
        with session_scope() as db:
            holdout_cases(db, dataset_id, None)  # type: ignore[arg-type]
        raise AssertionError("holdout was readable without a grant")
    except PermissionError:
        pass
    with session_scope() as db:
        assert holdout_cases(db, dataset_id, HoldoutAccess("holdout_validation", "test"))


def test_import_approved_cases_and_review(client, sample_agent):
    s = new_user(client)
    agent_id = setup_sample_agent(s, sample_agent)
    csv = ("question,expected_answer,key_facts,severity,expected_behavior\n"
           "How long is the refund window?,30 days from delivery,30 days,critical,answer\n"
           "What is the CEO's phone number?,,,high,abstain\n"
           ",missing question,,,\n")
    out = s.post("/ws/datasets:import", files={"file": ("approved.csv", csv.encode(), "text/csv")}, data={"name": "Business Q&A"})
    assert out["imported"] == 2 and out["skipped"] == 1 and out["regression"] == 2
    cases = s.get(f"/ws/datasets/{out['dataset']['id']}/cases")["items"]
    assert all(c["answer_status"] == "approved" and c["provenance"]["origin"] == "imported" for c in cases)
    run = run_eval(s, agent_id, out["dataset"]["id"], mode="deterministic")  # no model configured at all
    assert run["status"] == "completed" and run["synthetic"] is False and run["summary"]["scored"] == 2
    assert run["summary"]["reference_basis"] == {"approved": 2}
    reviewed = s.patch(f"/ws/datasets/{out['dataset']['id']}/cases/{cases[0]['id']}",
                       json={"answer_status": "needs_human_review", "severity": "high"})
    assert reviewed["answer_status"] == "needs_human_review"
    jsonl = b'{"question": "What does express delivery cost?", "key_facts": ["45 USD"]}\n'
    more = s.post("/ws/datasets:import", files={"file": ("more.jsonl", jsonl, "application/json")},
                  data={"dataset_id": out["dataset"]["id"], "approved": "false"})
    assert more["imported"] == 1
    s.post("/ws/datasets:import", files={"file": ("x.xml", b"<a/>", "text/xml")}, expect=422)


def test_changed_document_marks_cases_stale(client, sample_agent):
    s = new_user(client)
    setup_fake_model(s)
    agent_id = setup_sample_agent(s, sample_agent)
    body = (b"# Fees\n\n## Late fee\n\nA late fee of 15 USD applies after 10 days of delay on any invoice. "
            b"The fee is charged once per invoice and is never compounded.\n")
    s.post("/ws/documents", files={"file": ("fees.md", body, "text/markdown")})
    dataset_id = generate_dataset(s, agent_id, n_cases=4, unanswerable_share=0.0)
    assert s.get(f"/ws/datasets/{dataset_id}/coverage")["regression"]["stale"] == 0
    s.post("/ws/documents", files={"file": ("fees.md", body.replace(b"15 USD", b"25 USD"), "text/markdown")})
    assert s.get(f"/ws/datasets/{dataset_id}/coverage")["regression"]["stale"] >= 1
    retired = s.post(f"/ws/datasets/{dataset_id}/stale:retire")
    assert retired["retired"] >= 1 and retired["dataset"]["counts"].get("retired", 0) >= 1


# --- evaluation ---------------------------------------------------------------------------


def test_model_modes_fail_clearly_without_a_model(client, sample_agent):
    s = new_user(client)
    agent_id = setup_sample_agent(s, sample_agent)
    csv = b"question,key_facts\nHow long is the refund window?,30 days\n"
    dataset_id = s.post("/ws/datasets:import", files={"file": ("a.csv", csv, "text/csv")})["dataset"]["id"]
    before = len(sample_agent["state"].sessions)
    out = s.post("/ws/evaluation-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "single"})
    assert out["job"]["status"] == "failed" and "MODEL_NOT_CONFIGURED" in out["job"]["error"]
    assert "Deterministic mode can run without a model" in out["job"]["error"]
    assert len(sample_agent["state"].sessions) == before  # the agent was never invoked
    assert s.post("/ws/datasets:generate", json={"name": "x", "n_cases": 5})["job"]["status"] == "failed"


def test_single_evaluation_with_evidence_and_separate_costs(ready):
    s = ready["s"]
    run = run_eval(s, ready["agent_id"], ready["dataset_id"], mode="single")
    summary = run["summary"]
    assert run["status"] == "completed" and run["synthetic"] is True and run["agent_version"] == "baseline"
    assert run["dataset_version"] >= 1 and len(run["agent_config_digest"]) == 64
    assert "SYNTHETIC RUN" in summary["caveats"][0] and any("not a measure of real-world accuracy" in c for c in summary["caveats"])
    assert set(summary["by_category"]) >= {"knowledge", "citation", "unanswerable"}
    assert set(summary["by_dimension"]) >= {"correctness", "citation_accuracy", "abstention", "groundedness"}
    assert summary["by_dimension"]["correctness"]["methods"] == {"deterministic": summary["by_dimension"]["correctness"]["n"]}
    assert "model_judge" in summary["by_dimension"]["groundedness"]["methods"]
    # platform-model usage and target-agent cost are reported separately
    assert summary["model_usage"]["by_role"]["evaluator_primary"]["calls"] > 0
    assert summary["model_usage"]["estimated_cost"] is None and "No per-token price" in summary["model_usage"]["cost_note"]
    assert summary["agent"]["calls"] >= summary["cases"] and summary["agent"]["estimated_cost"] > 0
    assert summary["latency_ms"]["p95"] >= summary["latency_ms"]["p50"] >= 0

    results = s.get(f"/ws/evaluation-runs/{run['id']}/results", params={"limit": 200})["items"]
    assert len(results) == summary["cases"]
    answered = next(r for r in results if r["case"]["category"] == "knowledge")
    assert answered["case"]["evidence_refs"][0]["quote"] and answered["response"]["text"]
    assert answered["final"]["scores"]["citation_accuracy"]["method"] == "deterministic"
    assert answered["judgments"][0]["role"] == "evaluator_primary" and answered["judgments"][0]["synthetic"] is True
    unanswerable = next(r for r in results if r["case"]["category"] == "unanswerable")
    assert unanswerable["verdict"] == "fail" and unanswerable["final"]["scores"]["abstention"]["score"] == 0.0

    report = s.get(f"/ws/reports/{run['id']}")
    assert report["banner"].startswith("SYNTHETIC RESULT") and report["usage"]["target_agent"]["calls"] > 0
    assert "| Category |" in s.get(f"/ws/reports/{run['id']}", params={"format": "md"})
    # the run total also includes the root-cause call made after scoring
    assert s.get(f"/ws/runs/{run['id']}/model-usage")["calls"] >= summary["model_usage"]["calls"]


def test_dual_judges_are_independent_and_arbitrated(ready):
    s = ready["s"]
    run = run_eval(s, ready["agent_id"], ready["dataset_id"], mode="dual")
    agreement = run["summary"]["evaluator_agreement"]
    assert agreement["compared"] > 0 and agreement["arbitrated"] > 0 and 0 <= agreement["rate"] <= 1
    assert any("not ground truth" in c for c in run["summary"]["caveats"])
    usage = run["summary"]["model_usage"]["by_role"]
    assert usage["evaluator_primary"]["calls"] == usage["evaluator_secondary"]["calls"] > 0
    assert 0 < usage["arbiter"]["calls"] <= usage["evaluator_primary"]["calls"]
    results = s.get(f"/ws/evaluation-runs/{run['id']}/results", params={"limit": 200})["items"]
    arbitrated = next(r for r in results if r["final"].get("judge", {}).get("arbitrated"))
    assert [j["task"] for j in arbitrated["judgments"]] == ["judge.primary", "judge.secondary", "arbiter"]
    agreed = next(r for r in results if r["final"].get("judge", {}).get("agreement") is True)
    assert [j["task"] for j in agreed["judgments"]] == ["judge.primary", "judge.secondary"]
    assert run["summary"]["needs_human_review"] >= 0


def test_real_model_route_is_not_flagged_synthetic(client, sample_agent, mock_model):
    s = new_user(client)
    provider = s.post("/ws/model-providers", json={"name": "local", "kind": "openai_compatible", "base_url": mock_model.url + "/v1"})
    s.post(f"/ws/model-providers/{provider['id']}/verify")
    s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
    agent_id = setup_sample_agent(s, sample_agent)
    csv = b"question,expected_answer\nHow long is the refund window?,30 days from delivery\nWhat is the CEO's shoe size?,\n"
    dataset_id = s.post("/ws/datasets:import", files={"file": ("a.csv", csv, "text/csv")})["dataset"]["id"]
    run = run_eval(s, agent_id, dataset_id, mode="dual")
    assert run["status"] == "completed" and run["synthetic"] is False
    assert not any("SYNTHETIC" in c for c in run["summary"]["caveats"])
    assert any("estimates" in c for c in run["summary"]["caveats"])
    assert run["summary"]["by_dimension"]["correctness"]["methods"] == {"model_judge": 2}


def test_run_can_be_cancelled_midway(ready):
    s = ready["s"]

    class Ctx:
        calls = 0

        def progress(self, **_):
            pass

        def cancelled(self):
            self.calls += 1
            return self.calls > 3

    from kaizeval.jobs import JobCancelled
    from kaizeval.services.evaluation import create_run

    with session_scope() as db:
        run = create_run(db, workspace_id=s.ws, agent_id=ready["agent_id"], dataset_id=ready["dataset_id"],
                         split="regression", mode="deterministic", kind="baseline")
        db.commit()
        try:
            execute_run(db, run.id, ctx=Ctx())  # type: ignore[arg-type]
            raise AssertionError("run was not cancelled")
        except JobCancelled:
            pass
        run_id = run.id
    run = s.get(f"/ws/evaluation-runs/{run_id}")
    assert run["status"] == "cancelled"
    assert s.get(f"/ws/evaluation-runs/{run_id}/results")["total"] == 3


def test_agent_call_budget_stops_the_run(ready):
    s = ready["s"]
    out = s.post("/ws/evaluation-runs", json={"agent_id": ready["agent_id"], "dataset_id": ready["dataset_id"],
                                              "mode": "deterministic", "config": {"limits": {"max_agent_calls": 4}}})
    run = s.get(f"/ws/evaluation-runs/{out['run']['id']}")
    assert run["status"] == "failed" and "BUDGET_EXCEEDED" in run["error"] and run["summary"]["partial"] is True
    assert run["summary"]["agent"]["calls"] <= 4


def test_idempotent_run_creation(ready):
    s = ready["s"]
    body = {"agent_id": ready["agent_id"], "dataset_id": ready["dataset_id"], "mode": "deterministic"}
    first = s.post("/ws/evaluation-runs", json=body, headers={"Idempotency-Key": "same-key-1"})
    again = s.post("/ws/evaluation-runs", json=body, headers={"Idempotency-Key": "same-key-1"})
    assert first["run"]["id"] == again["run"]["id"] and first["job"]["id"] == again["job"]["id"]


# --- diagnostics and recommendations ------------------------------------------------------


def test_findings_and_recommendations(ready):
    s = ready["s"]
    run = run_eval(s, ready["agent_id"], ready["dataset_id"], mode="single")
    findings = s.get(f"/ws/evaluation-runs/{run['id']}/findings", params={"limit": 200})["items"]
    categories = {f["category"] for f in findings}
    assert {"citation", "instruction_prompt"} <= categories
    for finding in findings:
        assert finding["observation"] and finding["hypothesis"] and 0 < finding["confidence"] < 1
        assert finding["evidence"]["question"] and finding["source"] in ("rule", "rule+model")
    recs = s.get("/ws/recommendations", params={"run_id": run["id"]})["items"]
    assert recs and recs[0]["priority"] >= recs[-1]["priority"]
    by_category = {r["category"]: r for r in recs}
    citation = by_category["citation"]
    assert citation["auto_apply_available"] is True and citation["suggested_patch"] == {"cite_sources": True}
    assert citation["required_capability"] == "apply_patch" and citation["impact"]["cases_affected"] > 0
    assert "not a promised gain" in citation["impact"]["note"] and citation["state"] == "proposed"

    s.patch(f"/ws/recommendations/{citation['id']}", expect=422, json={"state": "rejected"})
    rejected = s.patch(f"/ws/recommendations/{citation['id']}", json={"state": "rejected", "reason": "handled elsewhere"})
    assert rejected["state"] == "rejected" and rejected["state_reason"] == "handled elsewhere"
    deferred = s.patch(f"/ws/recommendations/{by_category['instruction_prompt']['id']}", json={"state": "deferred"})
    assert deferred["state"] == "deferred"
    rebuilt = {r["category"]: r for r in s.post("/ws/recommendations", json={"run_id": run["id"]})["items"]}
    assert rebuilt["citation"]["state"] == "rejected"  # decisions survive a rebuild


def test_black_box_agent_gets_recommendations_only(ready, sample_agent):
    s = ready["s"]
    black = s.post("/ws/agents", json={"name": "black-box", "adapter": "http_chat",
                                       "config": {"url": sample_agent["url"] + "/chat"}})
    s.post(f"/ws/agents/{black['id']}/verify")
    run = run_eval(s, black["id"], ready["dataset_id"], mode="single")
    assert run["status"] == "completed" and run["agent_config_digest"] is None
    recs = s.get("/ws/recommendations", params={"run_id": run["id"]})["items"]
    assert recs and all(r["auto_apply_available"] is False and r["suggested_patch"] is None for r in recs)
    assert all(r["auto_apply_blocker"].startswith("UNSUPPORTED_CAPABILITY") for r in recs)
    refused = s.post("/ws/optimization-runs", expect=409, json={"agent_id": black["id"], "dataset_id": ready["dataset_id"]})
    assert refused["error"]["code"] == "UNSUPPORTED_CAPABILITY" and "Only recommendations" in refused["error"]["message"]
    s.post("/ws/evaluation-runs", expect=404, json={"agent_id": black["id"], "dataset_id": ready["dataset_id"],
                                                     "agent_version": "cand-doesnotexist"})


def test_diagnostics_refuse_holdout_runs(ready):
    s = ready["s"]
    with session_scope() as db:
        run = EvaluationRun(workspace_id=s.ws, agent_id=ready["agent_id"], dataset_id=ready["dataset_id"], split="holdout")
        for fn in (lambda: diagnostics.diagnose(db, run), lambda: diagnostics.recommend(db, run)):
            try:
                fn()
                raise AssertionError("holdout diagnostics were produced")
            except PermissionError:
                pass
        db.rollback()


def test_orphaned_jobs_and_runs_are_recovered(ready):
    """A run whose worker died must not stay 'running' forever."""
    from datetime import timedelta

    from kaizeval.jobs import Worker
    from kaizeval.models import Job, utcnow
    from kaizeval.services.evaluation import create_run

    s = ready["s"]
    with session_scope() as db:
        run = create_run(db, workspace_id=s.ws, agent_id=ready["agent_id"], dataset_id=ready["dataset_id"],
                         split="regression", mode="deterministic", kind="baseline")
        run.status = "running"
        job = Job(workspace_id=s.ws, kind="evaluation.run", payload={"run_id": run.id}, status="running", started_at=utcnow())
        db.add(job)
        db.flush()
        run_id, job_id = run.id, job.id
    worker = Worker()
    # external workers leave a recently started job alone (another worker may be executing it)
    assert worker.recover_orphans(only_stale=True) == 0
    assert s.get(f"/ws/jobs/{job_id}")["status"] == "running"
    with session_scope() as db:
        db.get(Job, job_id).started_at = utcnow() - timedelta(days=2)
    assert worker.recover_orphans(only_stale=True) == 1
    assert s.get(f"/ws/jobs/{job_id}")["status"] == "failed"
    failed = s.get(f"/ws/evaluation-runs/{run_id}")
    assert failed["status"] == "failed" and "worker stopped" in failed["error"]


# --- choosing how many testers and reviewers ----------------------------------------------


def test_panel_of_reviewers_and_parallel_testers(ready):
    from kaizeval.services.evaluation import reviewer_count, tester_count

    assert [reviewer_count(m, {"reviewers": 5}) for m in ("deterministic", "single", "dual", "panel")] == [0, 1, 2, 5]
    assert reviewer_count("panel", {"reviewers": 99}) == 7 and reviewer_count("panel", {}) == 3
    assert tester_count({"testers": 99}) == 8 and tester_count({}) == 1

    s = ready["s"]
    run = run_eval(s, ready["agent_id"], ready["dataset_id"], mode="panel", config={"reviewers": 3, "testers": 4})
    assert run["status"] == "completed" and run["summary"]["reviewers"] == 3 and run["summary"]["testers"] == 4
    assert run["config"]["reviewers"] == 3 and run["config"]["testers"] == 4
    usage = run["summary"]["model_usage"]["by_role"]
    # three reviewers per judged answer: two through the primary role, one through the secondary
    assert usage["evaluator_primary"]["calls"] == 2 * usage["evaluator_secondary"]["calls"] > 0
    results = s.get(f"/ws/evaluation-runs/{run['id']}/results", params={"limit": 200})["items"]
    judged = next(r for r in results if len(r["judgments"]) >= 3)
    assert [j["reviewer"] for j in judged["judgments"][:3]] == [1, 2, 3]
    assert [j["task"] for j in judged["judgments"][:3]] == ["judge.primary", "judge.secondary", "judge.primary"]
    assert judged["final"]["judge"]["reviewers"] == 3
    assert run["summary"]["evaluator_agreement"]["compared"] > 0
    # running the testers in parallel must not change any result
    sequential = run_eval(s, ready["agent_id"], ready["dataset_id"], mode="panel", config={"reviewers": 3, "testers": 1})
    assert sequential["summary"]["overall"] == run["summary"]["overall"]
    assert sequential["summary"]["by_category"] == run["summary"]["by_category"]
    assert len(results) == sequential["summary"]["cases"]


def test_several_independent_testers_write_the_tests(client, sample_agent):
    s = new_user(client)
    setup_fake_model(s)
    agent_id = setup_sample_agent(s, sample_agent)
    upload_kb(s)
    dataset_id = generate_dataset(s, agent_id, n_cases=24, testers=3)
    cases = s.get(f"/ws/datasets/{dataset_id}/cases", params={"limit": 200})["items"]
    written = [c for c in cases if c["category"] != "unanswerable"]
    assert {c["provenance"]["tester"] for c in written} == {1, 2, 3}
    assert len({c["provenance"]["persona"] for c in written}) == 3
    questions = [c["turns"][0]["content"] for c in cases]
    assert len(questions) == len(set(questions))
    # each tester worked on its own passages
    by_tester: dict[int, set[str]] = {}
    for case in written:
        by_tester.setdefault(case["provenance"]["tester"], set()).add(case["evidence_refs"][0]["chunk_id"])
    assert not (by_tester[1] & by_tester[2]) and not (by_tester[2] & by_tester[3])
    assert set(s.get(f"/ws/datasets/{dataset_id}/coverage")["regression"]["by_tester"]) >= {"1", "2", "3"}
    s.post("/ws/datasets:generate", expect=422, json={"name": "x", "n_cases": 10, "testers": 9})
