"""The files under examples/ must keep working exactly as documented."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from conftest import new_user, run_eval, serve, setup_sample_agent

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_example_import_files(client, sample_agent):
    s = new_user(client)
    agent_id = setup_sample_agent(s, sample_agent)
    approved = s.post("/ws/datasets:import", files={"file": ("approved-qa.csv", (EXAMPLES / "approved-qa.csv").read_bytes(), "text/csv")})
    assert approved["imported"] == 6 and approved["skipped"] == 0
    dataset_id = approved["dataset"]["id"]
    more = s.post("/ws/datasets:import", data={"dataset_id": dataset_id, "approved": "false"},
                  files={"file": ("conversations.jsonl", (EXAMPLES / "conversations.jsonl").read_bytes(), "application/json")})
    assert more["imported"] == 4
    cases = s.get(f"/ws/datasets/{dataset_id}/cases")["items"]
    assert sum(1 for c in cases if c["expected"]["behavior"] == "abstain") == 3
    assert any(len(c["turns"]) == 2 for c in cases)
    run = run_eval(s, agent_id, dataset_id, mode="deterministic")
    assert run["status"] == "completed" and run["summary"]["cases"] == 10 and run["summary"]["scored"] >= 9


def test_minimal_agent_example_is_evaluable(client):
    spec = importlib.util.spec_from_file_location("minimal_agent", EXAMPLES / "minimal_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    url, server = serve(module.app)
    try:
        s = new_user(client)
        agent = s.post("/ws/agents", json={"name": "minimal", "adapter": "http_chat", "config": {"url": url + "/chat"}})
        verified = s.post(f"/ws/agents/{agent['id']}/verify")
        assert verified["capabilities"] == ["invoke"] and verified["healthcheck"]["ok"] is True
        csv = b"question,key_facts,expected_behavior\nWhat is the refund window?,30 days,answer\nWhat is the CEO's salary?,,abstain\n"
        dataset_id = s.post("/ws/datasets:import", files={"file": ("q.csv", csv, "text/csv")})["dataset"]["id"]
        run = run_eval(s, agent["id"], dataset_id, mode="deterministic", config={"require_citations": False})
        assert run["status"] == "completed" and run["summary"]["overall"] == 1.0 and run["summary"]["pass_rate"] == 1.0
    finally:
        server.should_exit = True


def test_api_quickstart_script_runs_against_a_live_server(sample_agent):
    """The documented API walkthrough must work as written, over real HTTP."""
    from kaizeval.app import create_app
    from kaizeval.db import upgrade_db

    upgrade_db()
    url, server = serve(create_app())
    spec = importlib.util.spec_from_file_location("api_quickstart", EXAMPLES / "api_quickstart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines: list[str] = []
    try:
        out = module.main(["--url", url, "--agent-url", sample_agent["url"] + "/chat"], log=lines.append)
    finally:
        server.should_exit = True
    assert out["run"]["status"] == "completed" and out["run"]["summary"]["cases"] == 6
    assert out["run"]["synthetic"] is False  # deterministic mode uses no model at all
    assert out["recommendations"] and all(r["auto_apply_available"] is False for r in out["recommendations"])
    assert out["report"].startswith("# Kaizeval evaluation report")
    calls = [line for line in lines if "->" in line and line.strip().split()[0] in ("GET", "POST")]
    assert len(calls) >= 10 and all(line.rstrip().endswith(("200", "201", "202")) for line in calls)


def test_model_backed_example_agent_speaks_the_candidate_protocol(client, mock_model, monkeypatch):
    """examples/llm_agent.py: a model-backed agent that Kaizeval can verify, inspect and patch."""
    monkeypatch.setenv("LLM_BASE_URL", mock_model.url + "/v1")
    monkeypatch.setenv("LLM_MODEL", "mock")
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "example-admin-token")
    spec = importlib.util.spec_from_file_location("llm_agent", EXAMPLES / "llm_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    url, server = serve(module.create_app())
    try:
        s = new_user(client)
        agent = s.post("/ws/agents", json={"name": "llm agent", "adapter": "local_config", "config": {"base_url": url},
                                           "credential": "example-admin-token", "access_level": "candidate_write"})
        verified = s.post(f"/ws/agents/{agent['id']}/verify")
        assert {"invoke", "inspect", "create_candidate", "apply_patch", "restore_candidate"} <= set(verified["capabilities"])
        inspected = s.get(f"/ws/agents/{agent['id']}/configuration")
        assert inspected["schema"]["system_prompt"]["type"] == "string" and inspected["config"]["top_k"] == 1
        before = len(mock_model.requests)
        csv = b"question,key_facts\nWhat is the refund window?,30 days\n"
        dataset_id = s.post("/ws/datasets:import", files={"file": ("q.csv", csv, "text/csv")})["dataset"]["id"]
        run = run_eval(s, agent["id"], dataset_id, mode="deterministic")
        assert run["status"] == "completed" and run["summary"]["agent_errors"] == 0
        sent = mock_model.requests[before:]
        assert len(sent) == 1 and sent[0]["messages"][0]["content"] == module.BASELINE["system_prompt"]
        assert "Refund window" in sent[0]["messages"][-1]["content"]  # the retrieved passage reached the model
    finally:
        server.should_exit = True
