"""Generic HTTP chat connector (black-box invocation).

Two request styles are built in:

* ``simple``: POST ``{"message": ..., "session_id": ..., "history": [...]}`` and read the answer
  from a configurable path (default ``answer``).
* ``openai``: POST an OpenAI-style ``/chat/completions`` body and read ``choices.0.message.content``.

This adapter can only invoke. It never reports patch or candidate capabilities.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from ..netguard import EgressBlocked, EgressPolicy, guarded_client
from .base import INVOKE, AdapterError, AgentAdapter, AgentReply


def dig(data: Any, path: str) -> Any:
    """Resolve a dotted path such as ``choices.0.message.content``. Returns None when absent."""
    current = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def as_citations(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    out: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            label = item.get("source") or item.get("title") or item.get("document") or item.get("url") or item.get("id")
            if label:
                section = item.get("section")
                out.append(f"{label}#{section}" if section else str(label))
    return out


class HttpChatAdapter(AgentAdapter):
    name = "http_chat"

    def __init__(self, config: dict[str, Any], secret: str | None, policy: EgressPolicy):
        self.config = config
        self.secret = secret
        self.policy = policy
        self.url = config.get("url", "")
        self.style = config.get("style", "simple")
        self.timeout = float(config.get("timeout_s", 60))
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = guarded_client(self.policy, timeout=self.timeout, headers=self._headers())
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **(self.config.get("headers") or {})}
        auth = self.config.get("auth") or {}
        if self.secret and auth.get("type", "bearer") == "bearer":
            headers["Authorization"] = f"Bearer {self.secret}"
        elif self.secret and auth.get("type") == "header":
            headers[auth.get("header_name", "X-API-Key")] = self.secret
        return headers

    def capabilities(self) -> set[str]:
        return {INVOKE}

    def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        started = time.perf_counter()
        try:
            response = self._http().post(self.url, json=body)
        except EgressBlocked as exc:
            raise AdapterError("EGRESS_BLOCKED", exc.reason) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("TIMEOUT", f"agent did not answer within {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError("CONNECTION_ERROR", f"cannot reach agent ({type(exc).__name__})") from exc
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            raise AdapterError("AUTH", f"agent rejected the credentials (HTTP {response.status_code})")
        if response.status_code == 429:
            raise AdapterError("RATE_LIMITED", "agent rate limit reached")
        if response.status_code >= 400:
            raise AdapterError("BAD_RESPONSE", f"agent returned HTTP {response.status_code}")
        try:
            return response.json(), latency
        except ValueError as exc:
            raise AdapterError("BAD_RESPONSE", "agent did not return JSON") from exc

    def _body(self, messages: list[dict[str, str]], session_id: str, version: str) -> dict[str, Any]:
        if self.style == "openai":
            body: dict[str, Any] = {"model": self.config.get("model", "default"), "messages": messages}
        else:
            field = self.config.get("message_field", "message")
            body = {field: messages[-1]["content"], self.config.get("session_field", "session_id"): session_id,
                    "history": messages[:-1]}
        body.update(self.config.get("extra_body") or {})
        return body

    def invoke(self, messages: list[dict[str, str]], *, version: str = "baseline",
               session_id: str | None = None) -> AgentReply:
        if version != "baseline":
            raise AdapterError("UNSUPPORTED_CAPABILITY", "a black-box HTTP agent has no candidate versions")
        session_id = session_id or uuid.uuid4().hex
        data, latency = self._post(self._body(messages, session_id, version))
        default_path = "choices.0.message.content" if self.style == "openai" else "answer"
        text = dig(data, self.config.get("response_path") or default_path)
        if not isinstance(text, str):
            raise AdapterError("BAD_RESPONSE", "no answer text at the configured response path")
        citations = as_citations(dig(data, self.config.get("citations_path", "citations")))
        return AgentReply(text=text, citations=citations, latency_ms=latency, session_id=session_id,
                          cost=self.config.get("cost_per_call"), usage=data.get("usage") if isinstance(data, dict) else None,
                          version="baseline", raw={})

    def healthcheck(self) -> dict[str, Any]:
        probe = self.config.get("healthcheck_message", "Hello")
        reply = self.invoke([{"role": "user", "content": probe}])
        return {"ok": True, "latency_ms": reply.latency_ms, "returns_citations": bool(reply.citations)}
