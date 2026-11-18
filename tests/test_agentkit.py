"""`kaizeval.agentkit`: a user's own agent becomes optimizable in a few lines, including its system prompt."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from conftest import new_user, serve, setup_fake_model
from kaizeval.agentkit import CandidateStore
from kaizeval.gateway import core as gateway_core

ADMIN = "kit-test-token"
DEFAULT_PROMPT = "You are a support assistant."
BETTER_PROMPT = ("You are a support assistant. Always cite the source document. "
                 "Decline when the documents do not contain the answer.")
FACTS = {"refund": ("Unused appliances can be returned within 30 days of delivery.", "returns.md"),
         "express": ("Express delivery costs 45 USD and arrives within 2 business days.", "shipping.md")}


def make_agent() -> tuple[FastAPI, CandidateStore]:
    """A toy agent whose behaviour is driven by its system prompt, like a real prompt-based agent."""
    store = CandidateStore(
        defaults={"system_prompt": DEFAULT_PROMPT, "temperature": 0.2, "top_k": 2, "verbose": False},
        hints={"system_prompt": {"addresses": ["instruction_prompt", "citation"], "max_length": 300},
               "top_k": {"min": 1, "max": 5}, "temperature": {"min": 0.0, "max": 1.0}},
        description="Toy prompt-driven agent")
    app = FastAPI()
    app.include_router(store.router(ADMIN))

    @app.get("/health")
    def health():
        return {"ok": True, "production": store.production}

    @app.post("/chat")
    def chat(body: dict):
        version, settings = store.settings(body.get("version"))
        prompt = settings["system_prompt"].lower()
        hit = next((value for key, value in FACTS.items() if key in body["message"].lower()), None)
        if hit is None:
            answer = "I don't know." if "decline" in prompt else "It is probably fine, go ahead."
            citations: list[str] = []
        else:
            answer, citations = hit[0], ([hit[1]] if "cite" in prompt else [])
        return {"answer": answer, "citations": citations, "version": version, "config_digest": store.digest(version)}

    return app, store


@pytest.fixture(scope="module")
def kit_agent():
    app, store = make_agent()
    url, server = serve(app)
    yield {"url": url, "store": store}
    server.should_exit = True


def test_schema_is_inferred_and_patches_are_validated():
    store = CandidateStore({"system_prompt": "x", "top_k": 2, "ratio": 0.5, "flag": False},
                           {"system_prompt": {"max_length": 10}, "top_k": {"min": 1, "max": 4}})
    assert {k: v["type"] for k, v in store.schema.items()} == {
        "system_prompt": "string", "top_k": "integer", "ratio": "number", "flag": "boolean"}
    store.validate({"system_prompt": "short", "top_k": 3, "ratio": 0.9, "flag": True})
    for bad in ({"system_prompt": "x" * 11}, {"system_prompt": 5}, {"top_k": 9}, {"top_k": 1.5}, {"flag": "yes"},
                {"ratio": "high"}, {"unknown": 1}):
        with pytest.raises(ValueError):
            store.validate(bad)
    with pytest.raises(ValueError):
        CandidateStore({"a": 1}, {"b": {}})
    with pytest.raises(TypeError):
        CandidateStore({"a": [1, 2]})


def test_admin_api_protects_the_baseline(kit_agent):
    url, auth = kit_agent["url"], {"Authorization": f"Bearer {ADMIN}"}
    with httpx.Client(trust_env=False) as http:
        assert http.get(url + "/admin/schema").status_code == 401
        candidate = http.post(url + "/admin/candidates", headers=auth).json()["candidate_id"]
        assert http.patch(url + "/admin/candidates/baseline", headers=auth, json={"patch": {"top_k": 3}}).status_code == 404
        assert http.patch(url + f"/admin/candidates/{candidate}", headers=auth, json={"patch": {"rm": "-rf"}}).status_code == 422
        patched = http.patch(url + f"/admin/candidates/{candidate}", headers=auth, json={"patch": {"system_prompt": BETTER_PROMPT}})
        assert patched.status_code == 200 and patched.json()["after"]["system_prompt"] == BETTER_PROMPT
        assert http.get(url + "/admin/config", headers=auth).json()["config"]["system_prompt"] == DEFAULT_PROMPT
        assert http.post(url + "/admin/promote", headers=auth, json={"candidate_id": candidate, "approval_id": " "}).status_code == 422
        # the candidate behaves differently from the baseline on the same question
        ask = {"message": "What is your refund policy?"}
        assert http.post(url + "/chat", json={**ask, "version": "baseline"}).json()["citations"] == []
        assert http.post(url + "/chat", json={**ask, "version": candidate}).json()["citations"] == ["returns.md"]


def test_a_prompt_driven_agent_is_optimized_by_rewriting_its_system_prompt(client, kit_agent):
    s = new_user(client)
    setup_fake_model(s)
    agent = s.post("/ws/agents", json={"name": "my agent", "adapter": "local_config", "config": {"base_url": kit_agent["url"]},
                                       "credential": ADMIN, "access_level": "candidate_write", "allow_promote": True})
    verified = s.post(f"/ws/agents/{agent['id']}/verify")
    assert "apply_patch" in verified["capabilities"]
    inspected = s.get(f"/ws/agents/{agent['id']}/configuration")
    assert inspected["schema"]["system_prompt"]["type"] == "string" and inspected["config"]["system_prompt"] == DEFAULT_PROMPT

    csv = ("question,key_facts,expected_behavior,severity\n"
           "What is your refund policy?,30 days,answer,critical\n"
           "How much is express delivery?,45 USD,answer,normal\n"
           "How fast is express shipping?,2 business days,answer,normal\n"
           "What is the CEO's home address?,,abstain,high\n"
           "Can I pay with seashells?,,abstain,normal\n")
    dataset_id = s.post("/ws/datasets:import", files={"file": ("q.csv", csv.encode(), "text/csv")})["dataset"]["id"]
    s.patch(f"/workspaces/{s.ws}", json={"settings": {"allow_autonomous_optimization": True}})

    original = gateway_core.fake_complete
    proposals = iter([{"system_prompt": "x" * 400},       # too long: refused by the allowlist, never applied
                      {"system_prompt": BETTER_PROMPT}])  # a real improvement

    def optimizer(task, inputs):
        if task == "optimize":
            assert "system_prompt" in inputs["config_schema"] and inputs["current_config"]["system_prompt"]
            patch = next(proposals, {})
            return {"patch": patch, "rationale": "rewrite the instructions", "risk": "low"}
        return original(task, inputs)

    gateway_core.fake_complete = optimizer
    try:
        started = s.post("/ws/optimization-runs", json={
            "agent_id": agent["id"], "dataset_id": dataset_id, "mode": "autonomous",
            "policy": {"run_holdout": False, "eval_mode": "deterministic", "eval_config": {"require_citations": False}}})
    finally:
        gateway_core.fake_complete = original
    run = s.get(f"/ws/optimization-runs/{started['optimization_run']['id']}")
    assert run["status"] == "completed", run
    outcomes = [(h["outcome"]) for h in run["state"]["history"]]
    assert outcomes == ["invalid", "kept"]
    assert run["state"]["best_overall"] == 1.0 > run["state"]["baseline_overall"]
    assert run["stop_reason"] == "target score reached" or "optimizer found no further" in run["stop_reason"]
    candidate = s.get(f"/ws/candidates/{run['candidate_id']}")
    assert candidate["config_snapshot"]["system_prompt"] == BETTER_PROMPT
    assert candidate["baseline_snapshot"]["system_prompt"] == DEFAULT_PROMPT
    assert "system_prompt" in candidate["diff_from_baseline"]
    # the agent's own baseline is untouched and production was not moved
    assert kit_agent["store"].settings("baseline")[1]["system_prompt"] == DEFAULT_PROMPT
    assert kit_agent["store"].production == "baseline"
