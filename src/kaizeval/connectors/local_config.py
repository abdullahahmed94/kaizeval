"""Connector for agents that expose the Kaizeval local candidate protocol.

The protocol is a small admin API (see docs/CONNECTOR_SDK.md) that lets the platform read the
agent's configuration, create an isolated candidate copy, patch only that copy, restore it and,
with a recorded approval, switch the production pointer. The bundled sample agent implements it.
The baseline configuration has no mutating endpoint at all.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from ..netguard import EgressBlocked, EgressPolicy, guarded_client
from .base import (
    APPLY_PATCH,
    CREATE_CANDIDATE,
    INSPECT,
    INVOKE,
    PROMOTE,
    RESTORE_CANDIDATE,
    AdapterError,
    AgentAdapter,
    AgentReply,
)
from .http_chat import as_citations


class LocalConfigAdapter(AgentAdapter):
    name = "local_config"

    def __init__(self, config: dict[str, Any], secret: str | None, policy: EgressPolicy):
        self.base = (config.get("base_url") or "").rstrip("/")
        self.secret = secret
        self.policy = policy
        self.timeout = float(config.get("timeout_s", 60))
        self.cost_per_call = config.get("cost_per_call")
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = guarded_client(self.policy, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def capabilities(self) -> set[str]:
        return {INVOKE, INSPECT, CREATE_CANDIDATE, APPLY_PATCH, RESTORE_CANDIDATE, PROMOTE}

    def _request(self, method: str, path: str, *, admin: bool = False, json: Any = None,
                 params: dict[str, Any] | None = None) -> tuple[Any, int]:
        headers = {"Content-Type": "application/json"}
        if admin and self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        started = time.perf_counter()
        try:
            response = self._http().request(method, f"{self.base}{path}", json=json, params=params, headers=headers)
        except EgressBlocked as exc:
            raise AdapterError("EGRESS_BLOCKED", exc.reason) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("TIMEOUT", f"agent did not answer within {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError("CONNECTION_ERROR", f"cannot reach agent ({type(exc).__name__})") from exc
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            raise AdapterError("INSUFFICIENT_PERMISSION" if admin else "AUTH",
                               f"agent rejected the request (HTTP {response.status_code})")
        if response.status_code == 422 or response.status_code == 400:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))[:300]
            except ValueError:
                pass
            raise AdapterError("INVALID_PATCH" if admin else "BAD_RESPONSE", detail or f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise AdapterError("BAD_RESPONSE", f"agent returned HTTP {response.status_code}")
        try:
            return response.json(), latency
        except ValueError as exc:
            raise AdapterError("BAD_RESPONSE", "agent did not return JSON") from exc

    def healthcheck(self) -> dict[str, Any]:
        data, latency = self._request("GET", "/health")
        return {"ok": bool(data.get("ok")), "latency_ms": latency, "production_version": data.get("production")}

    def invoke(self, messages: list[dict[str, str]], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply:
        session_id = session_id or uuid.uuid4().hex
        data, latency = self._request("POST", "/chat", json={
            "message": messages[-1]["content"], "session_id": session_id, "version": version})
        return AgentReply(text=data.get("answer", ""), citations=as_citations(data.get("citations")),
                          latency_ms=latency, session_id=session_id, cost=self.cost_per_call,
                          version=data.get("version"), raw={"config_digest": data.get("config_digest")})

    def inspect(self, version: str = "baseline") -> dict[str, Any]:
        config, _ = self._request("GET", "/admin/config", admin=True, params={"version": version})
        schema, _ = self._request("GET", "/admin/schema", admin=True)
        return {"config": config["config"], "digest": config["digest"], "schema": schema["schema"],
                "description": schema.get("description", "")}

    def create_candidate(self) -> str:
        data, _ = self._request("POST", "/admin/candidates", admin=True)
        return data["candidate_id"]

    def apply_patch(self, candidate_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        data, _ = self._request("PATCH", f"/admin/candidates/{candidate_id}", admin=True, json={"patch": patch})
        return data

    def restore_candidate(self, candidate_id: str, config: dict[str, Any]) -> dict[str, Any]:
        data, _ = self._request("PUT", f"/admin/candidates/{candidate_id}/config", admin=True, json={"config": config})
        return data

    def promote(self, candidate_id: str, approval_id: str) -> dict[str, Any]:
        data, _ = self._request("POST", "/admin/promote", admin=True,
                                json={"candidate_id": candidate_id, "approval_id": approval_id})
        return data
