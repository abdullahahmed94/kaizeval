"""Model providers, role policies, usage, and target-agent connections."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import audit
from ..connectors import (
    ADAPTER_INFO,
    ADAPTERS,
    ALL_CAPABILITIES,
    AdapterError,
    build_adapter,
    effective_capabilities,
)
from ..connectors.base import ACCESS_GRANTS
from ..gateway import PROVIDER_KINDS, ROLES, Gateway, egress_policy
from ..models import Agent, ModelProvider, ModelRolePolicy, Secret, utcnow
from ..netguard import EgressBlocked, check_url
from ..security import encrypt_secret
from .deps import Ctx, fail, scoped, ser, workspace_ctx

router = APIRouter(prefix="/workspaces/{workspace_id}")


# --- model providers ----------------------------------------------------------------------


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)
    default_model: str = Field(default="", max_length=200)
    allow_egress: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class RolePolicyBody(BaseModel):
    provider_id: str
    model: str = Field(default="", max_length=200)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2000, ge=16, le=64000)
    timeout_s: float = Field(default=120.0, gt=0, le=1800)


def _store_secret(ctx: Ctx, name: str, value: str | None) -> str | None:
    if not value:
        return None
    secret = Secret(workspace_id=ctx.ws, name=name, ciphertext=encrypt_secret(value))
    ctx.db.add(secret)
    ctx.db.flush()
    return secret.id


@router.post("/model-providers", status_code=201)
def create_provider(body: ProviderCreate, ctx: Ctx = Depends(workspace_ctx("owner"))) -> dict[str, Any]:
    if body.kind not in PROVIDER_KINDS:
        raise fail(422, "UNKNOWN_PROVIDER_KIND", f"kind must be one of {PROVIDER_KINDS}")
    if body.kind == "openai_compatible":
        if not body.base_url:
            raise fail(422, "BASE_URL_REQUIRED", "an OpenAI-compatible provider needs a base URL, "
                       "for example http://localhost:11434/v1")
        try:
            check_url(body.base_url, egress_policy(ctx.workspace, allow_public=body.allow_egress))
        except EgressBlocked as exc:
            raise fail(409, "EGRESS_BLOCKED", f"{exc.reason}. Offline workspaces accept only local or allowlisted "
                       "model endpoints; a connected workspace must also set allow_egress for public providers.") from exc
    provider = ModelProvider(workspace_id=ctx.ws, name=body.name.strip(), kind=body.kind, base_url=body.base_url,
                             default_model=body.default_model, allow_egress=body.allow_egress, params=body.params,
                             secret_id=_store_secret(ctx, f"model-provider:{body.name}", body.api_key))
    ctx.db.add(provider)
    ctx.db.flush()
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="model_provider.created",
                 subject_type="model_provider", subject_id=provider.id,
                 detail={"kind": body.kind, "allow_egress": body.allow_egress, "has_credential": bool(body.api_key)})
    ctx.db.commit()
    return ser(provider, extra={"synthetic": provider.kind == "fake"})


@router.get("/model-providers")
def list_providers(ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    rows = ctx.db.scalars(select(ModelProvider).where(ModelProvider.workspace_id == ctx.ws).order_by(ModelProvider.created_at))
    return {"items": [ser(p, extra={"synthetic": p.kind == "fake"}) for p in rows]}


@router.post("/model-providers/{provider_id}/verify")
def verify_provider(provider_id: str, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Preflight: network policy, connectivity and structured-output capability. Required before AI-assisted steps."""
    provider = scoped(ctx, ModelProvider, provider_id)
    result = Gateway(ctx.db, ctx.ws).preflight(provider)
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="model_provider.preflight",
                 subject_type="model_provider", subject_id=provider.id, detail={"ok": result["ok"]})
    ctx.db.commit()
    return result


@router.delete("/model-providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, ctx: Ctx = Depends(workspace_ctx("owner"))) -> None:
    provider = scoped(ctx, ModelProvider, provider_id)
    for policy in ctx.db.scalars(select(ModelRolePolicy).where(ModelRolePolicy.provider_id == provider.id)):
        ctx.db.delete(policy)
    if provider.secret_id:
        secret = ctx.db.get(Secret, provider.secret_id)
        if secret is not None:
            ctx.db.delete(secret)
    ctx.db.delete(provider)
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="model_provider.deleted",
                 subject_type="model_provider", subject_id=provider_id)
    ctx.db.commit()


@router.get("/model-role-policies")
def list_policies(ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    policies = {p.role: p for p in ctx.db.scalars(select(ModelRolePolicy).where(ModelRolePolicy.workspace_id == ctx.ws))}
    gateway = Gateway(ctx.db, ctx.ws)
    roles = []
    for role in ROLES:
        own = policies.get(role)
        effective = own or policies.get("default")
        roles.append({"role": role, "policy": ser(own) if own else None, "inherits_default": own is None and effective is not None,
                      "effective_provider_id": effective.provider_id if effective else None,
                      "ready": gateway.is_configured(role), "synthetic": gateway.is_synthetic(role)})
    return {"default": ser(policies["default"]) if "default" in policies else None, "roles": roles}


@router.put("/model-role-policies/{role}")
def set_policy(role: str, body: RolePolicyBody, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    if role != "default" and role not in ROLES:
        raise fail(422, "UNKNOWN_ROLE", f"role must be 'default' or one of {ROLES}")
    scoped(ctx, ModelProvider, body.provider_id)
    policy = ctx.db.scalar(select(ModelRolePolicy).where(ModelRolePolicy.workspace_id == ctx.ws, ModelRolePolicy.role == role))
    if policy is None:
        policy = ModelRolePolicy(workspace_id=ctx.ws, role=role, provider_id=body.provider_id)
        ctx.db.add(policy)
    policy.provider_id, policy.model = body.provider_id, body.model
    policy.temperature, policy.max_tokens, policy.timeout_s = body.temperature, body.max_tokens, body.timeout_s
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="model_policy.set",
                 subject_type="model_role_policy", subject_id=role, detail=body.model_dump())
    ctx.db.commit()
    return ser(policy)


@router.delete("/model-role-policies/{role}", status_code=204)
def delete_policy(role: str, ctx: Ctx = Depends(workspace_ctx("editor"))) -> None:
    policy = ctx.db.scalar(select(ModelRolePolicy).where(ModelRolePolicy.workspace_id == ctx.ws, ModelRolePolicy.role == role))
    if policy is not None:
        ctx.db.delete(policy)
        ctx.db.commit()


@router.get("/model-usage")
def usage(run_id: str | None = None, scope_id: str | None = None, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return Gateway(ctx.db, ctx.ws).usage(run_id=run_id, scope_id=scope_id)


@router.get("/runs/{run_id}/model-usage")
def run_usage(run_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    """Platform-model usage for one run. Optimization runs aggregate all of their evaluations."""
    gateway = Gateway(ctx.db, ctx.ws)
    scope = gateway.usage(scope_id=run_id)
    return scope if scope["calls"] else gateway.usage(run_id=run_id)


# --- agents -------------------------------------------------------------------------------


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    adapter: str
    config: dict[str, Any] = Field(default_factory=dict)
    credential: str | None = Field(default=None, max_length=4000)
    access_level: Literal["black_box", "read_only", "candidate_write"] = "black_box"
    allow_promote: bool = False


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    credential: str | None = Field(default=None, max_length=4000)
    access_level: Literal["black_box", "read_only", "candidate_write"] | None = None
    allow_promote: bool | None = None


LEVELS = ["black_box", "read_only", "candidate_write"]


def _check_agent(ctx: Ctx, adapter: str, config: dict[str, Any], access_level: str) -> None:
    if adapter not in ADAPTERS:
        raise fail(422, "UNKNOWN_ADAPTER", f"adapter must be one of {sorted(ADAPTERS)}")
    maximum = ADAPTER_INFO[adapter]["max_access_level"]
    if LEVELS.index(access_level) > LEVELS.index(maximum):
        raise fail(409, "UNSUPPORTED_CAPABILITY", f"the '{adapter}' adapter supports at most '{maximum}' access. "
                   "Evaluation and recommendations remain available.")
    url = config.get("url") or config.get("base_url")
    if not url:
        raise fail(422, "URL_REQUIRED", "the agent configuration needs 'url' (http_chat) or 'base_url' (local_config)")
    try:
        check_url(url, egress_policy(ctx.workspace, allow_public=bool(config.get("allow_public"))))
    except EgressBlocked as exc:
        raise fail(409, "EGRESS_BLOCKED", f"{exc.reason}. Offline workspaces can only reach local or allowlisted agents.") from exc


def _agent_dict(agent: Agent) -> dict[str, Any]:
    return ser(agent, extra={"verified": agent.verified_at is not None})


@router.get("/agent-adapters")
def adapters(ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return {"adapters": ADAPTER_INFO, "capabilities": ALL_CAPABILITIES,
            "access_levels": {level: sorted(caps) for level, caps in ACCESS_GRANTS.items()}}


@router.post("/agents", status_code=201)
def create_agent(body: AgentCreate, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    _check_agent(ctx, body.adapter, body.config, body.access_level)
    if body.allow_promote or body.access_level == "candidate_write":
        ctx.require("owner")  # write and promotion scopes are granted by an owner only
    agent = Agent(workspace_id=ctx.ws, name=body.name.strip(), adapter=body.adapter, config=body.config,
                  access_level=body.access_level, allow_promote=body.allow_promote,
                  secret_id=_store_secret(ctx, f"agent:{body.name}", body.credential))
    ctx.db.add(agent)
    ctx.db.flush()
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="agent.registered", subject_type="agent",
                 subject_id=agent.id, detail={"adapter": body.adapter, "access_level": body.access_level,
                                              "allow_promote": body.allow_promote})
    ctx.db.commit()
    return _agent_dict(agent)


@router.get("/agents")
def list_agents(ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    rows = ctx.db.scalars(select(Agent).where(Agent.workspace_id == ctx.ws).order_by(Agent.created_at))
    return {"items": [_agent_dict(a) for a in rows]}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return _agent_dict(scoped(ctx, Agent, agent_id))


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    agent = scoped(ctx, Agent, agent_id)
    config = body.config if body.config is not None else agent.config
    level = body.access_level or agent.access_level
    _check_agent(ctx, agent.adapter, config, level)
    if body.allow_promote or level == "candidate_write" or body.credential:
        ctx.require("owner")
    if body.name:
        agent.name = body.name.strip()
    agent.config, agent.access_level = config, level
    if body.allow_promote is not None:
        agent.allow_promote = body.allow_promote
    if body.credential:
        agent.secret_id = _store_secret(ctx, f"agent:{agent.name}", body.credential)
    # Any change invalidates the previous verification.
    agent.verified_at, agent.capabilities, agent.last_verification = None, [], None
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="agent.updated", subject_type="agent",
                 subject_id=agent.id, detail={"access_level": level, "allow_promote": agent.allow_promote})
    ctx.db.commit()
    return _agent_dict(agent)


@router.post("/agents/{agent_id}/verify")
def verify_agent(agent_id: str, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Probe the connection and record which capabilities are both implemented and granted."""
    agent = scoped(ctx, Agent, agent_id)
    try:
        adapter = build_adapter(ctx.db, agent)
        health = adapter.healthcheck()
        adapter_caps = adapter.capabilities()
        inspectable = None
        if "inspect" in effective_capabilities(adapter_caps, agent.access_level, agent.allow_promote):
            inspectable = sorted((adapter.inspect("baseline").get("schema") or {}).keys())
        adapter.close()
    except AdapterError as exc:
        agent.verified_at, agent.capabilities = None, []
        agent.last_verification = {"ok": False, "code": exc.code, "message": exc.message, "at": utcnow().isoformat()}
        ctx.db.commit()
        raise fail(502, exc.code, exc.message) from exc
    agent.capabilities = effective_capabilities(adapter_caps, agent.access_level, agent.allow_promote)
    agent.verified_at = utcnow()
    agent.last_verification = {"ok": True, "healthcheck": health, "adapter_capabilities": sorted(adapter_caps),
                               "patchable_settings": inspectable, "at": agent.verified_at.isoformat()}
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="agent.verified", subject_type="agent",
                 subject_id=agent.id, detail={"capabilities": agent.capabilities})
    ctx.db.commit()
    return {"ok": True, "healthcheck": health, "capabilities": agent.capabilities, "matrix": _matrix(agent, adapter_caps)}


def _matrix(agent: Agent, adapter_caps: set[str] | None = None) -> list[dict[str, Any]]:
    if adapter_caps is None:
        adapter_caps = set((agent.last_verification or {}).get("adapter_capabilities") or [])
    granted = set(ACCESS_GRANTS.get(agent.access_level, set())) | ({"promote"} if agent.allow_promote and agent.access_level == "candidate_write" else set())
    rows = []
    for capability in ALL_CAPABILITIES:
        supported, allowed = capability in adapter_caps, capability in granted
        reason = None
        if agent.verified_at is None:
            reason = "NOT_VERIFIED: verify the connection to discover what this adapter supports"
        elif not supported:
            reason = f"UNSUPPORTED_CAPABILITY: the '{agent.adapter}' adapter does not implement this"
        elif not allowed:
            reason = ("INSUFFICIENT_PERMISSION: promotion was not granted for this agent" if capability == "promote"
                      else f"INSUFFICIENT_PERMISSION: access level '{agent.access_level}' does not include this")
        rows.append({"capability": capability, "supported_by_adapter": supported, "granted": allowed,
                     "available": supported and allowed and capability in (agent.capabilities or []), "reason": reason})
    return rows


@router.get("/agents/{agent_id}/capabilities")
def capabilities(agent_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    agent = scoped(ctx, Agent, agent_id)
    caps = set(agent.capabilities or [])
    can_optimize = {"inspect", "create_candidate", "apply_patch", "restore_candidate"} <= caps
    return {
        "verified": agent.verified_at is not None, "access_level": agent.access_level, "capabilities": sorted(caps),
        "matrix": _matrix(agent),
        "modes": {
            "recommendations_only": "invoke" in caps,
            "human_guided_optimization": can_optimize,
            "autonomous_optimization": can_optimize and bool((ctx.workspace.settings or {}).get("allow_autonomous_optimization")),
            "promotion": "promote" in caps,
        },
        "explanation": None if can_optimize else
        "This agent cannot be patched safely through its connector, so Kaizeval will evaluate it and produce "
        "recommendations only. No automatic change will be offered.",
    }


@router.get("/agents/{agent_id}/configuration")
def agent_configuration(agent_id: str, version: str = "baseline", ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    agent = scoped(ctx, Agent, agent_id)
    if "inspect" not in (agent.capabilities or []):
        raise fail(409, "UNSUPPORTED_CAPABILITY", "this agent's configuration is not readable (black-box access)")
    try:
        return build_adapter(ctx.db, agent).inspect(version)
    except AdapterError as exc:
        raise fail(502, exc.code, exc.message) from exc


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, ctx: Ctx = Depends(workspace_ctx("owner"))) -> None:
    agent = scoped(ctx, Agent, agent_id)
    from ..models import EvaluationRun

    if ctx.db.scalar(select(EvaluationRun.id).where(EvaluationRun.agent_id == agent.id).limit(1)):
        raise fail(409, "IN_USE", "this agent has evaluation history and cannot be deleted")
    ctx.db.delete(agent)
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="agent.deleted", subject_type="agent", subject_id=agent_id)
    ctx.db.commit()
