"""LLM Gateway: routing, preflight, metering, structured output, retries, offline policy (acceptance 1, 5, 11)."""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import select

from conftest import new_user, setup_fake_model
from kaizeval.db import session_scope
from kaizeval.gateway import Gateway, GatewayError
from kaizeval.models import ModelInvocation, ModelProvider, Secret


def _local_provider(s, mock_model, **extra):
    body = {"name": "local", "kind": "openai_compatible", "base_url": mock_model.url + "/v1",
            "default_model": "local-test-model", "params": {"max_retries": 2}, **extra}
    return s.post("/ws/model-providers", json=body)


def test_missing_model_fails_clearly(client):
    s = new_user(client)
    with session_scope() as db, pytest.raises(GatewayError) as err:
        Gateway(db, s.ws).generate("judge.primary", {"dimensions": ["correctness"]})
    assert err.value.code == "MODEL_NOT_CONFIGURED"


def test_preflight_is_required_before_ai_steps(client):
    s = new_user(client)
    provider = s.post("/ws/model-providers", json={"name": "fake", "kind": "fake"})
    s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
    with session_scope() as db, pytest.raises(GatewayError) as err:
        Gateway(db, s.ws).generate("judge.primary", {"dimensions": ["correctness"], "response": "x"})
    assert err.value.code == "PREFLIGHT_REQUIRED"
    result = s.post(f"/ws/model-providers/{provider['id']}/verify")
    assert result["ok"] and result["synthetic"] is True
    assert {c["name"] for c in result["checks"]} >= {"network_policy", "connectivity", "structured_output"}
    with session_scope() as db:
        judgment, meta = Gateway(db, s.ws).generate("judge.primary", {"dimensions": ["correctness"], "response": "x"})
    assert meta.synthetic is True and "correctness" in judgment.scores


def test_fake_provider_is_labelled_synthetic(client):
    s = new_user(client)
    setup_fake_model(s)
    providers = s.get("/ws/model-providers")["items"]
    assert providers[0]["synthetic"] is True
    roles = s.get("/ws/model-role-policies")["roles"]
    assert all(r["ready"] and r["synthetic"] and r["inherits_default"] for r in roles)
    assert len(roles) == 8


def test_real_http_model_path_with_role_routing_and_usage(client, mock_model):
    s = new_user(client)
    fake_id = setup_fake_model(s)
    provider = _local_provider(s, mock_model, api_key="sk-local-test-key-123456",
                               params={"price_per_1k_input": 0.5, "price_per_1k_output": 1.5})
    assert provider["has_credential"] is True and "api_key" not in provider and "sk-local" not in str(provider)
    assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
    s.put("/ws/model-role-policies/evaluator_primary", json={"provider_id": provider["id"], "model": "judge-model"})
    roles = {r["role"]: r for r in s.get("/ws/model-role-policies")["roles"]}
    assert roles["evaluator_primary"]["synthetic"] is False and roles["evaluator_primary"]["effective_provider_id"] == provider["id"]
    assert roles["optimizer"]["effective_provider_id"] == fake_id

    mock_model.requests.clear()
    with session_scope() as db:
        gateway = Gateway(db, s.ws, run_id="run-usage-1")
        judgment, meta = gateway.generate("judge.primary", {"dimensions": ["correctness"], "response": "hello"})
        _, fake_meta = gateway.generate("judge.secondary", {"dimensions": ["correctness"], "response": "hello"})
    assert judgment.scores == {"correctness": 0.9} and meta.synthetic is False and fake_meta.synthetic is True
    assert mock_model.requests[-1]["model"] == "judge-model"
    assert mock_model.headers[-1]["authorization"] == "Bearer sk-local-test-key-123456"
    # untrusted input travels inside the delimited block, never as an instruction
    assert "<input>" in mock_model.requests[-1]["messages"][1]["content"]

    usage = s.get("/ws/runs/run-usage-1/model-usage")
    primary = usage["by_role"]["evaluator_primary"]
    assert primary["calls"] == 1 and primary["prompt_tokens"] == 120 and primary["completion_tokens"] == 30
    assert primary["estimated_cost"] == pytest.approx(0.12 * 0.5 + 0.03 * 1.5)
    # the fake/local-style provider without a price reports usage but never an invented bill
    assert usage["by_role"]["evaluator_secondary"]["estimated_cost"] is None
    with session_scope() as db:
        secret = db.scalar(select(Secret).where(Secret.workspace_id == s.ws))
        assert "sk-local-test-key-123456" not in secret.ciphertext


def test_invalid_output_is_retried_and_recorded(client, mock_model):
    s = new_user(client)
    provider = _local_provider(s, mock_model)
    s.post(f"/ws/model-providers/{provider['id']}/verify")
    s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
    mock_model.script[:] = ["this is not json", 500]
    with session_scope() as db:
        judgment, _ = Gateway(db, s.ws, run_id="run-retry").generate("judge.primary", {"dimensions": ["relevance"]})
        statuses = [i.status for i in db.scalars(select(ModelInvocation).where(ModelInvocation.run_id == "run-retry")
                                                 .order_by(ModelInvocation.created_at))]
    assert judgment.scores == {"relevance": 0.9}
    assert sorted(statuses) == ["error", "invalid_output", "ok"]


def test_auth_error_is_not_retried_and_never_falls_back(client, mock_model):
    s = new_user(client)
    setup_fake_model(s)  # a working default exists, and must NOT be used as a silent fallback
    provider = _local_provider(s, mock_model)
    s.post(f"/ws/model-providers/{provider['id']}/verify")
    s.put("/ws/model-role-policies/arbiter", json={"provider_id": provider["id"]})
    mock_model.script[:] = [401]
    before = len(mock_model.requests)
    with session_scope() as db, pytest.raises(GatewayError) as err:
        Gateway(db, s.ws).generate("arbiter", {"dimensions": ["correctness"], "judgments": []})
    assert err.value.code == "AUTH" and len(mock_model.requests) == before + 1


def test_response_format_fallback(client, mock_model):
    s = new_user(client)
    provider = _local_provider(s, mock_model)
    mock_model.reject_response_format = True
    try:
        assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
    finally:
        mock_model.reject_response_format = False


def test_unreachable_local_model_fails_visibly(client):
    s = new_user(client)
    provider = s.post("/ws/model-providers", json={"name": "down", "kind": "openai_compatible",
                                                   "base_url": "http://127.0.0.1:9/v1", "params": {"max_retries": 0}})
    result = s.post(f"/ws/model-providers/{provider['id']}/verify")
    assert result["ok"] is False
    assert any(c["name"] == "connectivity" and not c["ok"] for c in result["checks"])


def test_budget_limit_stops_calls(client):
    s = new_user(client)
    setup_fake_model(s)
    with session_scope() as db:
        gateway = Gateway(db, s.ws, run_id="budget-run", limits={"max_model_calls": 2})
        gateway.generate("judge.primary", {"dimensions": ["correctness"]})
        gateway.generate("judge.primary", {"dimensions": ["correctness"]})
        with pytest.raises(GatewayError) as err:
            gateway.generate("judge.primary", {"dimensions": ["correctness"]})
    assert err.value.code == "BUDGET_EXCEEDED"


def test_offline_refuses_external_provider_everywhere(client, monkeypatch):
    s = new_user(client)
    blocked = s.post("/ws/model-providers", expect=409, json={
        "name": "hosted", "kind": "openai_compatible", "base_url": "https://api.example-llm.com/v1", "allow_egress": True})
    assert blocked["error"]["code"] == "EGRESS_BLOCKED"
    s.post("/workspaces", expect=409, json={"name": "c", "mode": "connected"})
    s.patch(f"/workspaces/{s.ws}", expect=409, json={"mode": "connected"})

    # Even if such a provider row existed (for example from an older configuration), nothing is sent.
    def no_dns(*a, **k):
        raise AssertionError("no DNS lookup may happen in offline mode")

    with session_scope() as db:
        provider = ModelProvider(workspace_id=s.ws, name="legacy", kind="openai_compatible", allow_egress=True,
                                 base_url="https://api.example-llm.com/v1", last_preflight={"ok": True})
        db.add(provider)
        db.flush()
        provider_id = provider.id
    s.put("/ws/model-role-policies/default", json={"provider_id": provider_id})
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with session_scope() as db, pytest.raises(GatewayError) as err:
        Gateway(db, s.ws).generate("judge.primary", {"dimensions": ["correctness"]})
    assert err.value.code == "EGRESS_BLOCKED"
    monkeypatch.undo()
    assert s.post(f"/ws/model-providers/{provider_id}/verify")["ok"] is False
