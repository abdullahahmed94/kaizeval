"""Agent connector contract.

An adapter declares what it can do. The platform intersects that with what the customer granted
(``access_level`` / ``allow_promote``) before it attempts anything. An optional operation that an
adapter does not implement raises ``UNSUPPORTED_CAPABILITY``; nothing is ever simulated.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

INVOKE, INSPECT, TRACES = "invoke", "inspect", "traces"
CREATE_CANDIDATE, APPLY_PATCH, RESTORE_CANDIDATE, PROMOTE = (
    "create_candidate", "apply_patch", "restore_candidate", "promote")
ALL_CAPABILITIES = [INVOKE, INSPECT, TRACES, CREATE_CANDIDATE, APPLY_PATCH, RESTORE_CANDIDATE, PROMOTE]

# What each customer-granted access level permits, before intersecting with the adapter.
ACCESS_GRANTS = {
    "black_box": {INVOKE},
    "read_only": {INVOKE, INSPECT, TRACES},
    "candidate_write": {INVOKE, INSPECT, TRACES, CREATE_CANDIDATE, APPLY_PATCH, RESTORE_CANDIDATE},
}

ERROR_CODES = [
    "UNSUPPORTED_CAPABILITY", "INSUFFICIENT_PERMISSION", "NO_SAFE_ISOLATION", "RATE_LIMITED",
    "NON_REPLAYABLE_TOOL", "TIMEOUT", "CONNECTION_ERROR", "BAD_RESPONSE", "EGRESS_BLOCKED",
    "AUTH", "INVALID_PATCH",
]


class AdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class AgentReply:
    text: str
    citations: list[str] = field(default_factory=list)
    latency_ms: int = 0
    cost: float | None = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


class AgentAdapter(ABC):
    """Base class for connectors. Override the optional methods an adapter genuinely supports."""

    name = "base"

    @abstractmethod
    def capabilities(self) -> set[str]:
        """Operations this adapter implements for the configured target."""

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]: ...

    @abstractmethod
    def invoke(self, messages: list[dict[str, str]], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply: ...

    def _unsupported(self, capability: str) -> AdapterError:
        return AdapterError("UNSUPPORTED_CAPABILITY", f"the '{self.name}' adapter cannot '{capability}' this agent")

    def inspect(self, version: str = "baseline") -> dict[str, Any]:
        """Return ``{"config": {...}, "schema": {...}}`` describing readable/patchable configuration."""
        raise self._unsupported(INSPECT)

    def get_traces(self, session_id: str) -> dict[str, Any]:
        raise self._unsupported(TRACES)

    def create_candidate(self) -> str:
        raise self._unsupported(CREATE_CANDIDATE)

    def apply_patch(self, candidate_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        raise self._unsupported(APPLY_PATCH)

    def restore_candidate(self, candidate_id: str, config: dict[str, Any]) -> dict[str, Any]:
        raise self._unsupported(RESTORE_CANDIDATE)

    def promote(self, candidate_id: str, approval_id: str) -> dict[str, Any]:
        raise self._unsupported(PROMOTE)

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


def effective_capabilities(adapter_caps: set[str], access_level: str, allow_promote: bool) -> list[str]:
    granted = set(ACCESS_GRANTS.get(access_level, {INVOKE}))
    if allow_promote and access_level == "candidate_write":
        granted.add(PROMOTE)
    return [c for c in ALL_CAPABILITIES if c in adapter_caps and c in granted]


def render_diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    """A readable key-level diff of two flat configuration objects."""
    lines = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, "<unset>"), after.get(key, "<unset>")
        if old != new:
            lines.append(f"- {key}: {json.dumps(old, ensure_ascii=False)}")
            lines.append(f"+ {key}: {json.dumps(new, ensure_ascii=False)}")
    return "\n".join(lines) or "(no changes)"
