"""Agent connectors and the adapter registry."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..gateway import egress_policy
from ..models import Agent, Secret, Workspace
from ..security import decrypt_secret
from .base import (
    ALL_CAPABILITIES,
    AdapterError,
    AgentAdapter,
    AgentReply,
    config_digest,
    effective_capabilities,
    render_diff,
)
from .http_chat import HttpChatAdapter
from .local_config import LocalConfigAdapter

ADAPTERS: dict[str, type] = {"http_chat": HttpChatAdapter, "local_config": LocalConfigAdapter}

ADAPTER_INFO: dict[str, dict[str, Any]] = {
    "http_chat": {
        "label": "Generic HTTP chat endpoint",
        "description": "Black-box invocation of any agent reachable over HTTP, including OpenAI-compatible "
                       "chat endpoints. Recommendations only; it cannot create or patch a candidate.",
        "max_access_level": "black_box",
    },
    "local_config": {
        "label": "Local candidate protocol",
        "description": "Agents that implement the Kaizeval admin protocol (the bundled sample agent does). "
                       "Supports an isolated candidate copy, allowlisted patches, restore and approved promotion.",
        "max_access_level": "candidate_write",
    },
}


def register_adapter(name: str, cls: type, info: dict[str, Any]) -> None:
    """Extension point for third-party connectors."""
    ADAPTERS[name] = cls
    ADAPTER_INFO[name] = info


def build_adapter(db: Session, agent: Agent) -> AgentAdapter:
    cls = ADAPTERS.get(agent.adapter)
    if cls is None:
        raise AdapterError("UNSUPPORTED_CAPABILITY", f"unknown adapter '{agent.adapter}'")
    workspace = db.get(Workspace, agent.workspace_id)
    assert workspace is not None
    secret = None
    if agent.secret_id:
        row = db.get(Secret, agent.secret_id)
        if row is None or row.workspace_id != agent.workspace_id:
            raise AdapterError("AUTH", "the credential referenced by this agent is missing")
        secret = decrypt_secret(row.ciphertext)
    policy = egress_policy(workspace, allow_public=bool((agent.config or {}).get("allow_public")))
    return cls(agent.config or {}, secret, policy)


def require(agent: Agent, capability: str) -> None:
    """Raise unless the capability is both implemented by the adapter and granted by the customer."""
    if capability in (agent.capabilities or []):
        return
    where = f"agent '{agent.name}' (adapter: {agent.adapter}, access level: {agent.access_level})"
    if agent.verified_at is None:
        raise AdapterError("UNSUPPORTED_CAPABILITY", f"{where} has not been verified yet")
    adapter_caps = set((agent.last_verification or {}).get("adapter_capabilities") or [])
    if capability not in adapter_caps:
        raise AdapterError("UNSUPPORTED_CAPABILITY", f"the adapter cannot '{capability}' for {where}")
    raise AdapterError("INSUFFICIENT_PERMISSION", f"'{capability}' was not granted for {where}")


__all__ = ["ADAPTERS", "ADAPTER_INFO", "ALL_CAPABILITIES", "AdapterError", "AgentAdapter", "AgentReply",
           "build_adapter", "config_digest", "effective_capabilities", "register_adapter", "render_diff", "require"]
