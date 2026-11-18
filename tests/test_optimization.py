"""Controlled optimization, paired A/B, sealed holdout, approvals and promotion (acceptance 8, 9, 10)."""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from conftest import (
    add_member,
    generate_dataset,
    new_user,
    setup_fake_model,
    setup_sample_agent,
    upload_kb,
)
from kaizeval.db import session_scope
from kaizeval.gateway import core as gateway_core
from kaizeval.sample_agent import BASELINE_CONFIG
from kaizeval.services import optimization
from kaizeval.services.datasets import HoldoutAccess, holdout_cases
from kaizeval.services.stats import paired_delta, sign_test_p


def _workspace(client, sample_agent, *, autonomous: bool = True, n_cases: int = 24):
    s = new_user(client)
    setup_fake_model(s)
    agent_id = setup_sample_agent(s, sample_agent)
    upload_kb(s)
    dataset_id = generate_dataset(s, agent_id, n_cases=n_cases)
    if autonomous:
        s.patch(f"/workspaces/{s.ws}", json={"settings": {"allow_autonomous_optimization": True}})
    return s, agent_id, dataset_id


def _admin(sample_agent, path):
    response = httpx.get(sample_agent["url"] + path, headers={"Authorization": f"Bearer {sample_agent['token']}"}, trust_env=False)
    response.raise_for_status()
    return response.json()


def _start(s, agent_id, dataset_id, mode="autonomous", **policy):
    return s.post("/ws/optimization-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": mode, "policy": policy})


@pytest.fixture(scope="module")
def optimized(client, sample_agent):
    """One full autonomous run, with a spy that records everything the optimizer role was shown."""
    s, agent_id, dataset_id = _workspace(client, sample_agent)
    with session_scope() as db:
        hidden = [c.turns[0]["content"] for c in holdout_cases(db, dataset_id, HoldoutAccess("holdout_validation", "test"))]
    seen: list[str] = []
    original = gateway_core.fake_complete

    def spy(task, inputs):
        if task in ("optimize", "root_cause"):
            seen.append(json.dumps(inputs))
        return original(task, inputs)

    gateway_core.fake_complete = spy
    try:
        started = _start(s, agent_id, dataset_id, max_rounds=6, stagnation_rounds=3)
    finally:
        gateway_core.fake_complete = original
    run = s.get(f"/ws/optimization-runs/{started['optimization_run']['id']}")
    return {"s": s, "agent_id": agent_id, "dataset_id": dataset_id, "run": run, "optimizer_inputs": seen, "hidden": hidden,
            "job": started["job"]}


def test_autonomous_mode_is_off_by_default(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, autonomous=False, n_cases=8)
    blocked = s.post("/ws/optimization-runs", expect=409, json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous"})
    assert blocked["error"]["code"] == "POLICY_BLOCKED"
    editor = add_member(s, "editor")
    editor.post("/ws/optimization-runs", expect=403, json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous"})


def test_optimization_improves_candidate_and_leaves_baseline_untouched(optimized, sample_agent):
    run = optimized["run"]
    assert run["status"] == "completed" and optimized["job"]["status"] == "completed" and run["stop_reason"]
    history = run["state"]["history"]
    assert any(h["outcome"] == "kept" for h in history)
    assert run["state"]["best_overall"] > run["state"]["baseline_overall"]
    # Baseline A is byte-for-byte what it was; only Candidate B changed.
    assert _admin(sample_agent, "/admin/config?version=baseline")["config"] == BASELINE_CONFIG
    candidate = optimized["s"].get(f"/ws/candidates/{run['candidate_id']}")
    assert candidate["baseline_snapshot"] == BASELINE_CONFIG and candidate["config_snapshot"] != BASELINE_CONFIG
    assert candidate["status"] == "awaiting_decision" and candidate["diff_from_baseline"].startswith("- ")
    live = _admin(sample_agent, f"/admin/config?version={candidate['external_id']}")["config"]
    assert live == candidate["config_snapshot"]
    # the production pointer was not moved by optimization
    assert _admin(sample_agent, "/admin/production")["production"] == "baseline"
    for patch in candidate["patches"]:
        assert patch["state"] in ("kept", "rolled_back") and patch["diff"] and patch["outcome"]["evaluation_run_id"]
        assert set(patch["patch"]) <= set(BASELINE_CONFIG)


def test_every_round_reruns_full_regression_and_is_paired(optimized):
    s, run = optimized["s"], optimized["run"]
    regression_count = s.get(f"/ws/datasets/{optimized['dataset_id']}")["counts"]["regression"]
    rounds = [e for e in run["experiments"] if e["kind"] == "regression"]
    assert len(rounds) == len([h for h in run["state"]["history"] if h["outcome"] in ("kept", "rolled_back")])
    for entry in rounds:
        experiment = s.get(f"/ws/experiments/{entry['id']}")
        comparison = experiment["comparison"]
        assert comparison["paired_cases"] == regression_count and comparison["unpaired_cases"] == 0
        assert comparison["conditions"] == {"same_dataset_version": True, "same_mode": True,
                                            "dataset_version": comparison["conditions"]["dataset_version"]}
        overall = comparison["overall"]
        assert overall["n"] == regression_count and overall["verdict"] in ("improved", "regressed", "inconclusive")
        assert overall["delta_percentage_points"] == pytest.approx((overall["candidate_mean"] - overall["baseline_mean"]) * 100, abs=0.02)
        assert experiment["decision"] in ("kept", "rolled_back")
    runs = s.get("/ws/evaluation-runs", params={"limit": 200})["items"]
    candidate_runs = [r for r in runs if r["optimization_run_id"] == run["id"] and r["kind"] == "candidate"]
    assert all(r["summary"]["cases"] == regression_count for r in candidate_runs)


def test_holdout_stays_sealed_from_the_optimizer(optimized):
    run = optimized["run"]
    assert optimized["optimizer_inputs"], "the optimizer role was never called"
    assert len(optimized["hidden"]) >= 3
    blob = "\n".join(optimized["optimizer_inputs"])
    for question in optimized["hidden"]:
        assert question not in blob
    holdout = run["holdout"]
    assert holdout["status"] == "completed" and holdout["paired_cases"] == len(optimized["hidden"])
    assert holdout["overall"]["verdict"] in ("improved", "regressed", "inconclusive")
    serialized = json.dumps(holdout)
    assert "case_id" not in serialized and "regressed_case_ids" not in serialized
    assert all(question not in serialized for question in optimized["hidden"])
    # source-level guard: the optimizer module has no way to obtain a holdout grant
    source = inspect.getsource(optimization)
    assert "HoldoutAccess" not in source and "holdout_cases" not in source and "cases_for_run" not in source
    # holdout runs produce no findings or recommendations
    s = optimized["s"]
    holdout_runs = [r for r in s.get("/ws/evaluation-runs", params={"limit": 200})["items"] if r["split"] == "holdout"]
    assert len(holdout_runs) == 2 and {r["agent_version"] for r in holdout_runs} >= {"baseline"}
    for item in holdout_runs:
        assert s.get(f"/ws/evaluation-runs/{item['id']}/findings")["total"] == 0
        assert s.get("/ws/recommendations", params={"run_id": item["id"]})["items"] == []
    viewer = add_member(s, "viewer")
    viewer.get(f"/ws/evaluation-runs/{holdout_runs[0]['id']}/results", params={"acknowledge": True}, expect=403)
    sealed = s.get(f"/ws/evaluation-runs/{holdout_runs[0]['id']}/results", expect=403)
    assert sealed["error"]["code"] == "HOLDOUT_SEALED"


def test_report_shows_deltas_usage_and_caveats(optimized):
    s, run = optimized["s"], optimized["run"]
    report = s.get(f"/ws/reports/{run['id']}")
    assert report["banner"].startswith("SYNTHETIC RESULT") and report["synthetic"] is True
    comparison = report["regression_comparison"]["overall"]
    assert comparison["delta_percentage_points"] > 0 and comparison["relative_change_pct"] is not None and comparison["ci95"]
    assert report["baseline"]["by_category"] and report["best"]["by_category"]
    assert report["usage"]["platform_models"]["by_role"]["optimizer"]["calls"] >= 1
    assert report["usage"]["target_agent"]["calls"] > 0 and report["usage"]["target_agent"]["estimated_cost"] > 0
    assert "never automatic" in report["release_decision"]["note"]
    assert any("Nothing was changed in production" in c for c in report["caveats"])
    markdown = s.get(f"/ws/reports/{run['id']}", params={"format": "md"})
    assert "percentage points" in markdown and "Independent holdout validation" in markdown
    assert s.get(f"/ws/runs/{run['id']}/model-usage")["calls"] == report["usage"]["platform_models"]["calls"]


def test_promotion_needs_explicit_approval_and_can_be_rolled_back(optimized, sample_agent):
    s, run = optimized["s"], optimized["run"]
    candidate_id = run["candidate_id"]
    refused = s.post(f"/ws/candidates/{candidate_id}/promote", expect=409)
    assert refused["error"]["code"] == "APPROVAL_REQUIRED"
    assert _admin(sample_agent, "/admin/production")["production"] == "baseline"
    add_member(s, "editor").post(f"/ws/candidates/{candidate_id}/approve", expect=403, json={"decision": "approved"})
    approval = s.post(f"/ws/candidates/{candidate_id}/approve", json={"decision": "approved", "note": "validated on holdout"})
    assert approval["approval"]["consumed"] is False
    assert [a["id"] for a in s.get(f"/ws/candidates/{candidate_id}")["approvals"]] == [approval["approval"]["id"]]
    assert _admin(sample_agent, "/admin/production")["production"] == "baseline"  # approval alone deploys nothing
    promoted = s.post(f"/ws/candidates/{candidate_id}/promote")
    assert promoted["candidate"]["status"] == "promoted"
    production = _admin(sample_agent, "/admin/production")
    assert production["production"] == promoted["candidate"]["external_id"]
    assert production["promotions"][-1]["approval_id"] == approval["approval"]["id"]
    s.post(f"/ws/candidates/{candidate_id}/promote", expect=409)  # an approval cannot be reused
    rolled = s.post(f"/ws/candidates/{candidate_id}/rollback", json={"note": "test"})
    assert rolled["candidate"]["status"] == "rolled_back"
    assert _admin(sample_agent, "/admin/production")["production"] == "baseline"
    actions = [e["action"] for e in s.get("/ws/audit", params={"limit": 200})["items"]]
    for expected in ("candidate.created", "candidate.patched", "holdout.validated", "promotion.approved",
                     "candidate.promoted", "candidate.production_rolled_back", "optimization.completed"):
        assert expected in actions


def test_spent_holdout_must_be_rotated(optimized):
    s, agent_id, dataset_id = optimized["s"], optimized["agent_id"], optimized["dataset_id"]
    assert s.get(f"/ws/datasets/{dataset_id}")["holdout_exposed"] is True
    spent = s.post("/ws/optimization-runs", expect=409, json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous"})
    assert spent["error"]["code"] == "HOLDOUT_SPENT"
    before = s.get(f"/ws/datasets/{dataset_id}")
    job = s.post(f"/ws/datasets/{dataset_id}/holdout:rotate", json={})
    assert job["status"] == "completed" and job["result"]["new_holdout_cases"] >= 2
    after = s.get(f"/ws/datasets/{dataset_id}")
    assert after["holdout_generation"] == 2 and after["holdout_exposed"] is False and after["version"] == before["version"] + 1
    assert after["counts"]["regression"] == before["counts"]["regression"] + before["counts"]["holdout"]
    with session_scope() as db:
        fresh = {c.turns[0]["content"] for c in holdout_cases(db, dataset_id, HoldoutAccess("holdout_validation", "test"))}
    assert fresh.isdisjoint(optimized["hidden"])
    again = _start(s, agent_id, dataset_id, max_rounds=1)
    assert again["optimization_run"]["status"] == "completed"


def test_rounds_and_budgets_bound_the_loop(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=10)
    one = _start(s, agent_id, dataset_id, max_rounds=1, run_holdout=False)["optimization_run"]
    assert one["status"] == "completed" and one["stop_reason"] == "maximum number of rounds reached" and one["state"]["round"] == 1
    assert one["holdout"]["status"] == "skipped"
    capped = _start(s, agent_id, dataset_id, run_holdout=False, limits={"max_model_calls": 25})["optimization_run"]
    assert capped["status"] == "completed" and "budget" in capped["stop_reason"]
    target = _start(s, agent_id, dataset_id, run_holdout=False, target_score=0.01)["optimization_run"]
    assert target["stop_reason"] == "target score reached" and target["state"]["round"] == 0
    assert s.get(f"/ws/candidates/{target['candidate_id']}")["status"] == "rejected"


def test_failing_gates_roll_the_candidate_back(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=10)
    run = _start(s, agent_id, dataset_id, run_holdout=False, min_improvement=0.99, stagnation_rounds=2)["optimization_run"]
    assert run["status"] == "completed" and run["stop_reason"] == "the score did not change in 2 consecutive cycles"
    history = run["state"]["history"]
    assert history and all(h["outcome"] == "rolled_back" and h["reasons"] for h in history)
    candidate = s.get(f"/ws/candidates/{run['candidate_id']}")
    assert candidate["config_snapshot"] == BASELINE_CONFIG and candidate["status"] == "rejected"
    assert _admin(sample_agent, f"/admin/config?version={candidate['external_id']}")["config"] == BASELINE_CONFIG
    with pytest.raises(optimization.OptimizationError):
        optimization.validate_patch({"system_shell": "rm -rf /"}, {"top_k": {"type": "integer", "min": 1, "max": 6}})
    with pytest.raises(optimization.OptimizationError):
        optimization.validate_patch({"top_k": 99}, {"top_k": {"type": "integer", "min": 1, "max": 6}})


def test_human_guided_flow_pauses_for_each_change(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, autonomous=False, n_cases=10)
    started = _start(s, agent_id, dataset_id, mode="human_guided", run_holdout=False, max_rounds=3)
    run = s.get(f"/ws/optimization-runs/{started['optimization_run']['id']}")
    assert run["status"] == "review_required" and started["job"]["status"] == "review_required"
    first = run["patches"][0]
    assert first["state"] == "proposed" and first["diff"] and first["rationale"]
    candidate = s.get(f"/ws/candidates/{run['candidate_id']}")
    assert candidate["config_snapshot"] == BASELINE_CONFIG  # nothing is applied before a decision
    add_member(s, "viewer").post(f"/ws/candidate-patches/{first['id']}/decision", expect=403, json={"decision": "approved"})
    assert s.post(f"/ws/candidate-patches/{first['id']}/decision", expect=422,
                  json={"decision": "rejected"})["error"]["code"] == "REASON_REQUIRED"
    rejected = s.post(f"/ws/candidate-patches/{first['id']}/decision", json={"decision": "rejected", "note": "too risky"})
    assert rejected["patch"]["state"] == "rejected" and rejected["optimization_run"]["status"] == "review_required"
    run = s.get(f"/ws/optimization-runs/{run['id']}")
    second = run["patches"][-1]
    assert second["id"] != first["id"] and second["state"] == "proposed"
    approved = s.post(f"/ws/candidate-patches/{second['id']}/decision", json={"decision": "approved"})
    assert approved["patch"]["state"] in ("kept", "rolled_back") and approved["patch"]["decided_by"]
    s.post(f"/ws/candidate-patches/{second['id']}/decision", expect=409, json={"decision": "approved"})
    stopped = s.post(f"/ws/optimization-runs/{run['id']}/stop")
    assert stopped["status"] in ("cancelled", "completed")
    if stopped["status"] == "cancelled":
        assert stopped["finished_at"] and stopped["holdout"]["status"] == "skipped"
        assert not [p for p in stopped["patches"] if p["state"] == "proposed"]
        assert s.get(f"/ws/candidates/{run['candidate_id']}")["status"] in ("awaiting_decision", "rejected")


def test_paired_statistics():
    improved = paired_delta([0.5] * 30, [0.8] * 30)
    assert improved["verdict"] == "improved" and improved["delta_percentage_points"] == 30.0
    assert improved["relative_change_pct"] == 60.0 and improved["wins"] == 30 and improved["sign_test_p"] < 0.001
    noisy = paired_delta([0.5, 0.9, 0.4, 0.8], [0.6, 0.8, 0.5, 0.7])
    assert noisy["verdict"] == "inconclusive" and "noise" in noisy["interpretation"]
    assert paired_delta([0.9] * 20, [0.6] * 20)["verdict"] == "regressed"
    assert paired_delta([0.0, 0.0], [0.5, 0.5])["relative_change_pct"] is None  # no valid relative change from zero
    assert paired_delta([], [])["verdict"] == "no_data"
    assert sign_test_p(0, 0) is None and sign_test_p(5, 5) == 1.0
