"""The loop keeps going until the target is reached or two cycles in a row leave the score unchanged."""

from __future__ import annotations

from conftest import generate_dataset, new_user, setup_fake_model, setup_sample_agent, upload_kb
from kaizeval.gateway import core as gateway_core
from kaizeval.services.optimization import DEFAULT_POLICY


def _workspace(client, sample_agent, n_cases=24):
    s = new_user(client)
    setup_fake_model(s)
    agent_id = setup_sample_agent(s, sample_agent)
    upload_kb(s)
    dataset_id = generate_dataset(s, agent_id, n_cases=n_cases)
    s.patch(f"/workspaces/{s.ws}", json={"settings": {"allow_autonomous_optimization": True}})
    return s, agent_id, dataset_id


def test_default_policy_has_no_fixed_number_of_rounds():
    assert DEFAULT_POLICY["max_rounds"] is None and DEFAULT_POLICY["stagnation_rounds"] == 2


def test_loop_runs_until_two_cycles_leave_the_score_unchanged(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent)
    started = s.post("/ws/optimization-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous",
                                                    "policy": {"run_holdout": False}})
    run = s.get(f"/ws/optimization-runs/{started['optimization_run']['id']}")
    assert run["status"] == "completed" and run["policy"]["max_rounds"] is None
    history = run["state"]["history"]
    # No round limit ended it. It kept going while cycles improved the score and stopped only when
    # two cycles in a row changed nothing, or when the optimizer had no change left to propose.
    assert run["stop_reason"] in ("the score did not change in 2 consecutive cycles",
                                  "the optimizer found no further safe change")
    if run["stop_reason"].startswith("the score did not change"):
        assert all(h["outcome"] != "kept" for h in history[-2:])
    else:
        assert run["state"]["stagnation"] < 2
    assert sum(1 for h in history if h["outcome"] == "kept") >= 2
    assert all(later["overall"] >= earlier["overall"] for earlier, later in zip(
        [h for h in history if h["outcome"] == "kept"], [h for h in history if h["outcome"] == "kept"][1:], strict=False))


    # it leaves with recommendations for what still fails, and a report
    report = s.get(f"/ws/reports/{run['id']}")
    remaining = report["remaining_recommendations"]
    assert isinstance(remaining, list) and report["best"]["run_id"] == run["best_run_id"]
    assert s.get("/ws/recommendations", params={"run_id": run["best_run_id"]})["items"] == [] or remaining
    for rec in remaining:
        assert rec["title"] and rec["impact"]["cases_affected"] >= 1
    markdown = s.get(f"/ws/reports/{run['id']}", params={"format": "md"})
    assert "## Recommendations for what still fails" in markdown


def test_repeating_a_failed_change_counts_as_an_unchanged_cycle(client, sample_agent):
    """A proposal that was already tried and not kept is not re-tested; it is a cycle without progress."""
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=10)
    original = gateway_core.fake_complete

    def stubborn(task, inputs):
        if task == "optimize":
            return {"patch": {"cite_sources": True}, "rationale": "always the same idea", "risk": "low"}
        return original(task, inputs)

    gateway_core.fake_complete = stubborn
    try:
        # an impossible improvement threshold makes every change fail its gate
        started = s.post("/ws/optimization-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous",
                                                        "policy": {"run_holdout": False, "min_improvement": 0.99}})
    finally:
        gateway_core.fake_complete = original
    run = s.get(f"/ws/optimization-runs/{started['optimization_run']['id']}")
    assert run["stop_reason"] == "the score did not change in 2 consecutive cycles"
    assert [h["outcome"] for h in run["state"]["history"]] == ["rolled_back", "repeated"]
    candidate_runs = [r for r in s.get("/ws/evaluation-runs", params={"limit": 50})["items"]
                      if r["optimization_run_id"] == run["id"] and r["kind"] == "candidate"]
    assert len(candidate_runs) == 1  # the repeated change was not evaluated again
    assert s.get(f"/ws/candidates/{run['candidate_id']}")["status"] == "rejected"


def test_an_explicit_round_limit_still_applies(client, sample_agent):
    s, agent_id, dataset_id = _workspace(client, sample_agent, n_cases=10)
    started = s.post("/ws/optimization-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous",
                                                    "policy": {"run_holdout": False, "max_rounds": 1}})
    run = started["optimization_run"]
    assert run["stop_reason"] == "maximum number of rounds reached" and run["state"]["round"] == 1
