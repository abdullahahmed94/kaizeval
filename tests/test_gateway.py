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


def test_fields_a_hosted_endpoint_refuses_are_left_out_and_remembered(client, mock_model):
    """Hosted endpoints differ: one refuses `response_format: json_object`, a model that reasons before
    answering refuses a temperature. The first call finds out; later calls do not pay for it again."""
    s = new_user(client)
    provider = _local_provider(s, mock_model)
    mock_model.reject_response_format = mock_model.reject_temperature = True
    try:
        before = len(mock_model.requests)
        assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
        first = mock_model.requests[before:]
        assert len(first) == 3 and "temperature" in first[0] and "response_format" in first[0]
        assert "temperature" not in first[2] and "response_format" not in first[2] and first[2]["max_tokens"] >= 1024
        s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
        before = len(mock_model.requests)
        with session_scope() as db:
            judgment, _ = Gateway(db, s.ws).generate("judge.primary", {"dimensions": ["relevance"]})
        later = mock_model.requests[before:]
        assert judgment.scores == {"relevance": 0.9} and len(later) == 1
        assert "temperature" not in later[0] and "response_format" not in later[0]
    finally:
        mock_model.reject_response_format = mock_model.reject_temperature = False


def test_only_the_refused_field_is_dropped(client, mock_model):
    """A model that refuses only the temperature keeps JSON mode, and the other way round."""
    s = new_user(client)
    provider = _local_provider(s, mock_model)
    mock_model.reject_temperature = True
    try:
        before = len(mock_model.requests)
        assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
        sent = mock_model.requests[before:]
        assert len(sent) == 2 and "temperature" not in sent[1] and sent[1]["response_format"] == {"type": "json_object"}
    finally:
        mock_model.reject_temperature = False
    # a provider can also be told not to send a temperature at all
    quiet = _local_provider(s, mock_model, params={"send_temperature": False})
    before = len(mock_model.requests)
    assert s.post(f"/ws/model-providers/{quiet['id']}/verify")["ok"] is True
    assert len(mock_model.requests) == before + 1 and "temperature" not in mock_model.requests[-1]


def test_an_answer_cut_at_the_output_limit_is_reported_as_such(client, mock_model):
    """A reasoning model can spend its whole output allowance before answering. That is not "invalid JSON",
    and asking again with the same limit cannot help."""
    s = new_user(client)
    provider = _local_provider(s, mock_model)
    assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
    s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"], "max_tokens": 300})
    mock_model.finish_reason = "length"
    try:
        mock_model.script[:] = ['{"scores": {"relev']
        before = len(mock_model.requests)
        with session_scope() as db, pytest.raises(GatewayError) as err:
            Gateway(db, s.ws, run_id="run-cut").generate("judge.primary", {"dimensions": ["relevance"]})
        assert err.value.code == "OUTPUT_TRUNCATED" and "300 tokens" in err.value.message
        assert "model.max_tokens" in err.value.message and len(mock_model.requests) == before + 1  # not retried
        # the same during preflight: the check explains what happened
        mock_model.script[:] = [""]
        checks = {c["name"]: c for c in s.post(f"/ws/model-providers/{provider['id']}/verify")["checks"]}
        assert checks["structured_output"]["ok"] is False and "output limit" in checks["structured_output"]["detail"]
        # a complete answer that merely ends at the limit is still accepted
        assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
    finally:
        mock_model.finish_reason = None
        mock_model.script[:] = []
    assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True


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
