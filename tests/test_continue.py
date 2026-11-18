"""After the loop stops, the owner reviews the report and either continues from the candidate or releases it."""

from __future__ import annotations

import httpx
import pytest

from conftest import (
    add_member,
    generate_dataset,
    new_user,
    serve,
    setup_fake_model,
    setup_sample_agent,
    upload_kb,
)
from kaizeval.gateway import core as gateway_core
from kaizeval.sample_agent import BASELINE_CONFIG, create_sample_agent


def _workspace(client, agent, n_cases=24):
    s = new_user(client)
    setup_fake_model(s)
    agent_id = setup_sample_agent(s, agent)
    upload_kb(s)
    dataset_id = generate_dataset(s, agent_id, n_cases=n_cases)
    s.patch(f"/workspaces/{s.ws}", json={"settings": {"allow_autonomous_optimization": True}})
    return s, agent_id, dataset_id


def _start(s, agent_id, dataset_id, **policy):
    started = s.post("/ws/optimization-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous",
                                                    "policy": policy})
    return s.get(f"/ws/optimization-runs/{started['optimization_run']['id']}")


def _continue(s, run_id, expect=202, **policy):
    out = s.post(f"/ws/optimization-runs/{run_id}/continue", json={"policy": policy}, expect=expect)
    return out if expect != 202 else s.get(f"/ws/optimization-runs/{run_id}")


def _evaluations(s, run_id, kind):
    return [r for r in s.get("/ws/evaluation-runs", params={"limit": 200})["items"]
            if r["optimization_run_id"] == run_id and r["kind"] == kind]


def _reset_production(agent):
    httpx.post(agent["url"] + "/admin/promote", headers={"Authorization": f"Bearer {agent['token']}"}, trust_env=False,
               json={"candidate_id": "baseline", "approval_id": "test-cleanup"}).raise_for_status()


def test_a_stopped_run_offers_the_choice_and_continues_from_its_candidate(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent)
    first = _start(s, agent_id, dataset_id, max_rounds=1, run_holdout=False)
    assert first["status"] == "completed" and first["stop_reason"] == "maximum number of rounds reached"
    assert [h["outcome"] for h in first["state"]["history"]] == ["kept"]
    candidate = s.get(f"/ws/candidates/{first['candidate_id']}")
    assert candidate["status"] == "awaiting_decision"
    kept_patch = first["state"]["history"][0]["patch"]

    # the report says what the owner can do now
    report = s.get(f"/ws/reports/{first['id']}")
    assert report["next_decision"]["options"] == ["continue", "release", "reject"] and report["continuations"] == []
    assert "## Your decision" in s.get(f"/ws/reports/{first['id']}", params={"format": "md"})

    # continue: one more cycle, counted from the continuation, starting from the candidate
    second = _continue(s, first["id"], max_rounds=1)
    assert second["id"] == first["id"] and second["candidate_id"] == first["candidate_id"]
    assert second["status"] == "completed" and second["stop_reason"] == "maximum number of rounds reached"
    assert second["state"]["round"] == 2 and len(second["state"]["history"]) == 2
    assert second["state"]["history"][0] == first["state"]["history"][0]
    assert second["state"]["history"][1]["patch"] != kept_patch  # a new idea, not the first one again
    assert second["state"]["best_overall"] >= first["state"]["best_overall"]
    continued = second["state"]["continuations"]
    assert len(continued) == 1 and continued[0]["after_round"] == 1
    assert continued[0]["previous_stop_reason"] == "maximum number of rounds reached"
    assert continued[0]["previous_best_overall"] == first["state"]["best_overall"]
    assert continued[0]["baseline_changed_by_owner"] is False
    # both versions were measured again before the new cycle
    assert continued[0]["baseline_overall"] == first["state"]["baseline_overall"]
    assert continued[0]["best_overall"] == first["state"]["best_overall"]
    assert len(_evaluations(s, first["id"], "baseline")) == 2
    assert second["baseline_run_id"] != first["baseline_run_id"]
    assert any(e["decision"] == "continued" for e in second["experiments"])
    # the first kept change is still part of the candidate; the baseline is still untouched
    after = s.get(f"/ws/candidates/{first['candidate_id']}")
    assert all(after["config_snapshot"][key] == value for key, value in kept_patch.items())
    assert after["baseline_snapshot"] == BASELINE_CONFIG and after["status"] == "awaiting_decision"
    report = s.get(f"/ws/reports/{first['id']}")
    assert len(report["continuations"]) == 1 and "previous_holdout" not in report["continuations"][0]
    assert "Continued after round 1" in s.get(f"/ws/reports/{first['id']}", params={"format": "md"})
    actions = [e["action"] for e in s.get("/ws/audit", params={"limit": 200})["items"]]
    assert "optimization.continued" in actions

    # without an explicit limit it goes on until three cycles in a row change nothing, or no idea is left
    third = _continue(s, first["id"], max_rounds=None)
    assert third["status"] == "completed" and len(third["state"]["continuations"]) == 2
    assert third["stop_reason"] in ("the score did not change in 3 consecutive cycles",
                                    "the optimizer found no further safe change", "target score reached")


def test_an_approval_given_before_continuing_cannot_release_the_changed_candidate(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=12)
    run = _start(s, agent_id, dataset_id, max_rounds=1, run_holdout=False)
    candidate_id = run["candidate_id"]
    assert s.get(f"/ws/candidates/{candidate_id}")["status"] == "awaiting_decision"
    s.post(f"/ws/candidates/{candidate_id}/approve", json={"decision": "approved", "note": "looks fine"})
    _continue(s, run["id"], max_rounds=1)
    # the candidate may have changed since that approval, so it does not count any more
    refused = s.post(f"/ws/candidates/{candidate_id}/promote", expect=409)
    assert refused["error"]["code"] == "APPROVAL_REQUIRED"
    approvals = s.get(f"/ws/candidates/{candidate_id}")["approvals"]
    assert approvals[0]["consumed"] is True and "void" in approvals[0]["note"]
    try:
        s.post(f"/ws/candidates/{candidate_id}/approve", json={"decision": "approved", "note": "reviewed again"})
        assert s.post(f"/ws/candidates/{candidate_id}/promote")["candidate"]["status"] == "promoted"
        # a released candidate is final: improving further is a new optimization
        assert _continue(s, run["id"], expect=409)["error"]["code"] == "NOT_CONTINUABLE"
    finally:
        _reset_production(sample_agent)


def test_what_cannot_be_continued(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=12)
    run = _start(s, agent_id, dataset_id, max_rounds=1)  # with the final holdout check
    assert run["holdout"]["status"] == "completed"
    # settings that define the measurement cannot be changed half-way
    assert _continue(s, run["id"], expect=409, eval_mode="dual")["error"]["code"] == "INVALID_POLICY"
    # the holdout informed the stop, so it cannot confirm more tuning
    spent = _continue(s, run["id"], expect=409)
    assert spent["error"]["code"] == "HOLDOUT_SPENT" and "rotate" in spent["error"]["message"]
    # an editor may not continue an autonomous run
    add_member(s, "editor").post(f"/ws/optimization-runs/{run['id']}/continue", json={}, expect=403)
    # after a rotation it continues, and validates on the fresh holdout at the next stop
    assert s.post(f"/ws/datasets/{dataset_id}/holdout:rotate", json={})["status"] == "completed"
    again = _continue(s, run["id"], max_rounds=1)
    assert again["status"] == "completed" and again["state"]["continuations"][0]["previous_holdout"]["status"] == "completed"
    assert again["holdout"]["status"] == "completed" and again["holdout"]["generation"] == 2
    # the old holdout joined the regression set, and the report says the scores are not comparable any more
    regression = s.get(f"/ws/datasets/{dataset_id}")["counts"]["regression"]
    assert again["state"]["continuations"][0]["test_set_changed"] is True
    assert again["state"]["continuations"][0]["regression_cases"] == regression
    assert f"grown to {regression} cases" in s.get(f"/ws/reports/{run['id']}", params={"format": "md"})
    # ... or without the final check
    assert _continue(s, run["id"], max_rounds=1, run_holdout=False)["holdout"]["status"] == "skipped"
    # a rejected candidate is closed
    s.post(f"/ws/candidates/{run['candidate_id']}/approve", json={"decision": "rejected", "note": "not good enough"})
    assert _continue(s, run["id"], expect=409)["error"]["code"] == "NOT_CONTINUABLE"
    assert s.get(f"/ws/reports/{run['id']}")["next_decision"] is None


def test_continuing_when_nothing_was_kept(client, sample_agent):
    """No change passed the gates, so there is nothing to release, but the loop can still be continued."""
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=10)
    original = gateway_core.fake_complete

    def stubborn(task, inputs):
        if task == "optimize":
            return {"patch": {"cite_sources": True}, "rationale": "always the same idea", "risk": "low"}
        return original(task, inputs)

    gateway_core.fake_complete = stubborn
    try:
        run = _start(s, agent_id, dataset_id, run_holdout=False, min_improvement=0.99)
    finally:
        gateway_core.fake_complete = original
    assert run["stop_reason"] == "the score did not change in 3 consecutive cycles"
    assert s.get(f"/ws/candidates/{run['candidate_id']}")["status"] == "rejected"
    decision = s.get(f"/ws/reports/{run['id']}")["next_decision"]
    assert decision["options"] == ["continue"] and "nothing to release" in decision["note"]
    assert len(_evaluations(s, run["id"], "candidate")) == 1

    again = _continue(s, run["id"], min_improvement=0.005, max_rounds=2)
    assert again["status"] == "completed" and again["state"]["round"] == 5
    # the candidate equals the baseline, so one measurement was enough before the new cycles
    assert len(_evaluations(s, run["id"], "baseline")) == 2
    assert any(h["outcome"] == "kept" for h in again["state"]["history"][3:])
    assert s.get(f"/ws/candidates/{run['candidate_id']}")["status"] == "awaiting_decision"


def test_continue_after_the_owner_changed_and_restarted_the_agent(client):
    """The owner applied a recommendation by hand (a new default) and restarted the agent, which forgot its
    candidate copies. Continuing rebuilds the candidate on the new baseline and measures both again."""
    token = "own-agent-token"
    app = create_sample_agent(admin_token=token)
    url, server = serve(app)
    store = app.state.agent_state.store
    try:
        s, agent_id, dataset_id = _workspace(client, {"url": url, "token": token}, n_cases=12)
        first = _start(s, agent_id, dataset_id, max_rounds=1, run_holdout=False)
        assert [h["outcome"] for h in first["state"]["history"]] == ["kept"]
        kept_patch = first["state"]["history"][0]["patch"]
        before = s.get(f"/ws/candidates/{first['candidate_id']}")
        manual = next(key for key in ("max_sentences", "top_k") if key not in kept_patch)
        new_value = BASELINE_CONFIG[manual] + 1

        store._baseline[manual] = new_value      # the owner's own change to the agent
        store.candidates.clear()                 # a restart without a state file

        second = _continue(s, first["id"], max_rounds=1)
        assert second["status"] == "completed", second["error"]
        after = s.get(f"/ws/candidates/{first['candidate_id']}")
        assert after["external_id"] != before["external_id"] and after["external_id"] in store.candidates
        assert after["baseline_snapshot"][manual] == new_value and after["baseline_digest"] != before["baseline_digest"]
        # the candidate carries the owner's change and the change the loop had kept
        assert after["config_snapshot"][manual] == new_value or manual in second["state"]["history"][-1]["patch"]
        assert all(after["config_snapshot"][key] == value for key, value in kept_patch.items())
        assert store.candidates[after["external_id"]] == after["config_snapshot"]
        assert second["state"]["continuations"][0]["baseline_changed_by_owner"] is True
        assert store._baseline == {**BASELINE_CONFIG, manual: new_value}  # only the owner changed the baseline
        markdown = s.get(f"/ws/reports/{first['id']}", params={"format": "md"})
        assert "the baseline had been changed by its owner" in markdown
    finally:
        server.should_exit = True


def test_only_a_stopped_run_can_be_continued(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=10)
    s.patch(f"/workspaces/{s.ws}", json={"settings": {"allow_autonomous_optimization": False}})
    started = s.post("/ws/optimization-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "human_guided",
                                                    "policy": {"run_holdout": False, "max_rounds": 1}})
    run_id = started["optimization_run"]["id"]
    assert s.get(f"/ws/optimization-runs/{run_id}")["status"] == "review_required"
    assert _continue(s, run_id, expect=409)["error"]["code"] == "NOT_CONTINUABLE"
    s.post(f"/ws/optimization-runs/{run_id}/continue", json={}, expect=409)
    with pytest.raises(AssertionError):
        s.post("/ws/optimization-runs/does-not-exist/continue", json={})
