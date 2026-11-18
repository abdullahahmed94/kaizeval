"""LLM Gateway: the single path through which every model-assisted step runs.

Responsibilities: role-based routing, egress policy, preflight, budgets, retries, structured
output validation and usage metering. The agent under test is *not* reached through this module.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ModelInvocation, ModelProvider, ModelRolePolicy, Secret, Workspace, utcnow
from ..netguard import EgressBlocked, EgressPolicy, check_url, guarded_client
from ..security import decrypt_secret, redact
from .fake import fake_complete
from .prompts import TASKS, Task, render

ROLES = [
    "specification_inference", "test_generator", "reference_draft", "evaluator_primary",
    "evaluator_secondary", "arbiter", "root_cause", "optimizer",
]
PROVIDER_KINDS = ["fake", "openai_compatible"]

T = TypeVar("T", bound=BaseModel)
_semaphores: dict[str, threading.BoundedSemaphore] = {}
# Optional request fields that a provider refused once (HTTP 400). They are left out from then on.
_refused_fields: dict[str, set[str]] = {}
_semaphore_lock = threading.Lock()
RETRY_BASE_SECONDS = 0.5


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    tokens_estimated: bool
    latency_ms: int
    model: str
    truncated: bool = False  # the endpoint stopped at the output limit


@dataclass
class CallMeta:
    synthetic: bool
    provider_id: str
    provider_kind: str
    model: str
    prompt_version: str
    role: str


def effective_offline(workspace: Workspace) -> bool:
    return get_settings().offline or workspace.mode != "connected"


def egress_policy(workspace: Workspace, *, allow_public: bool) -> EgressPolicy:
    settings = get_settings()
    return EgressPolicy(offline=effective_offline(workspace), allow_public=allow_public,
                        allowlist=frozenset(settings.allowlist_hosts))


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def extract_json(text: str) -> Any:
    """Parse a JSON object from a model reply, tolerating code fences and surrounding prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


def _openai_complete(provider: ModelProvider, api_key: str | None, policy: EgressPolicy, *, model: str,
                     messages: list[dict[str, str]], temperature: float, max_tokens: int, timeout: float) -> Completion:
    base = (provider.base_url or "").rstrip("/")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = provider.params or {}
    refused = _refused_fields.setdefault(provider.id, set())
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if params.get("send_temperature", True) and "temperature" not in refused:
        body["temperature"] = temperature
    if params.get("json_mode", True) and "response_format" not in refused:
        body["response_format"] = {"type": "json_object"}
    started = time.perf_counter()
    try:
        with guarded_client(policy, timeout=timeout, headers=headers) as client:
            response = client.post(url, json=body)
            # Endpoints differ in what they accept: some refuse this response_format, and models
            # that reason before answering refuse a temperature. Leave the optional fields out one
            # at a time, starting with the one the error names, and remember what was refused.
            order = ["response_format", "temperature"]
            complaint = response.text.lower() if response.status_code == 400 else ""
            if "temperature" in complaint and "response_format" not in complaint:
                order.reverse()
            dropped: list[str] = []
            for name in order:
                if response.status_code == 400 and name in body:
                    del body[name]
                    dropped.append(name)
                    response = client.post(url, json=body)
            if dropped and response.status_code < 400:
                refused.update(dropped)
    except EgressBlocked as exc:
        raise GatewayError("EGRESS_BLOCKED", exc.reason) from exc
    except httpx.TimeoutException as exc:
        raise GatewayError("TIMEOUT", f"model endpoint did not answer within {timeout}s", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise GatewayError("CONNECTION", f"cannot reach model endpoint ({type(exc).__name__})", retryable=True) from exc
    latency = int((time.perf_counter() - started) * 1000)
    if response.status_code in (401, 403):
        raise GatewayError("AUTH", f"model endpoint rejected the credentials (HTTP {response.status_code})")
    if response.status_code == 429:
        raise GatewayError("RATE_LIMITED", "model endpoint rate limit reached", retryable=True)
    if response.status_code >= 500:
        raise GatewayError("PROVIDER_ERROR", f"model endpoint failed (HTTP {response.status_code})", retryable=True)
    if response.status_code >= 400:
        raise GatewayError("BAD_REQUEST", f"model endpoint refused the request (HTTP {response.status_code}): "
                           f"{redact(response.text[:300])}")
    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"] or ""
        truncated = data["choices"][0].get("finish_reason") == "length"
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise GatewayError("BAD_RESPONSE", "model endpoint returned an unexpected payload") from exc
    usage = data.get("usage") or {}
    estimated = "prompt_tokens" not in usage
    prompt_tokens = usage.get("prompt_tokens") or _estimate_tokens(json.dumps(messages))
    completion_tokens = usage.get("completion_tokens") or _estimate_tokens(text)
    return Completion(text, int(prompt_tokens), int(completion_tokens), estimated, latency, data.get("model") or model,
                      truncated=truncated)


TRUNCATED_HELP = ("the model reached its output limit of {limit} tokens before finishing its answer. Models that reason "
                  "before answering spend output tokens on that reasoning. Raise max_tokens for this role "
                  "(project file: model.max_tokens)")


class Gateway:
    def __init__(self, db: Session, workspace_id: str, *, run_id: str | None = None, scope_id: str | None = None,
                 limits: dict[str, Any] | None = None):
        self.db = db
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.scope_id = scope_id or run_id
        self.limits = limits or {}
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            raise GatewayError("NOT_FOUND", "workspace does not exist")
        self.workspace = workspace

    # -- routing ---------------------------------------------------------------------------

    def resolve(self, role: str) -> tuple[ModelProvider, ModelRolePolicy]:
        policies = {
            p.role: p for p in self.db.scalars(
                select(ModelRolePolicy).where(ModelRolePolicy.workspace_id == self.workspace_id,
                                              ModelRolePolicy.role.in_([role, "default"])))
        }
        policy = policies.get(role) or policies.get("default")
        if policy is None:
            raise GatewayError("MODEL_NOT_CONFIGURED",
                               f"no model is assigned to role '{role}' and no default model is set")
        provider = self.db.get(ModelProvider, policy.provider_id)
        if provider is None or provider.workspace_id != self.workspace_id:
            raise GatewayError("MODEL_NOT_CONFIGURED", f"the provider assigned to role '{role}' no longer exists")
        return provider, policy

    def is_configured(self, role: str) -> bool:
        try:
            provider, _ = self.resolve(role)
        except GatewayError:
            return False
        return bool(provider.last_preflight and provider.last_preflight.get("ok"))

    def is_synthetic(self, role: str) -> bool:
        try:
            return self.resolve(role)[0].kind == "fake"
        except GatewayError:
            return False

    def _policy_for(self, provider: ModelProvider) -> EgressPolicy:
        return egress_policy(self.workspace, allow_public=bool(provider.allow_egress))

    def _api_key(self, provider: ModelProvider) -> str | None:
        if not provider.secret_id:
            return None
        secret = self.db.get(Secret, provider.secret_id)
        if secret is None or secret.workspace_id != self.workspace_id:
            raise GatewayError("AUTH", "the credential referenced by this provider is missing")
        return decrypt_secret(secret.ciphertext)

    # -- budgets and metering --------------------------------------------------------------

    def usage(self, *, run_id: str | None = None, scope_id: str | None = None) -> dict[str, Any]:
        q = select(
            ModelInvocation.role, func.count(), func.sum(ModelInvocation.prompt_tokens),
            func.sum(ModelInvocation.completion_tokens), func.sum(ModelInvocation.latency_ms),
            func.sum(ModelInvocation.estimated_cost), func.max(ModelInvocation.provider_kind),
        ).where(ModelInvocation.workspace_id == self.workspace_id).group_by(ModelInvocation.role)
        if run_id:
            q = q.where(ModelInvocation.run_id == run_id)
        if scope_id:
            q = q.where(ModelInvocation.scope_id == scope_id)
        by_role, totals = {}, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0}
        cost_total, cost_known = 0.0, False
        for role, calls, pt, ct, lat, cost, kind in self.db.execute(q):
            by_role[role] = {"calls": calls, "prompt_tokens": pt or 0, "completion_tokens": ct or 0,
                             "latency_ms": lat or 0, "estimated_cost": cost, "provider_kind": kind}
            totals["calls"] += calls
            totals["prompt_tokens"] += pt or 0
            totals["completion_tokens"] += ct or 0
            totals["latency_ms"] += lat or 0
            if cost is not None:
                cost_total, cost_known = cost_total + cost, True
        return {
            "by_role": by_role, **totals,
            # None means "no price configured" (for example a local model): never an invented bill.
            "estimated_cost": round(cost_total, 6) if cost_known else None,
            "cost_note": "Estimate from the per-token prices configured on the provider; not an invoice."
            if cost_known else "No per-token price is configured; token counts and latency are reported instead.",
        }

    def _check_budget(self) -> None:
        if not self.limits or not self.scope_id:
            return
        used = self.usage(scope_id=self.scope_id)
        max_calls = self.limits.get("max_model_calls")
        max_tokens = self.limits.get("max_model_tokens")
        max_cost = self.limits.get("max_model_cost")
        if max_calls is not None and used["calls"] >= max_calls:
            raise GatewayError("BUDGET_EXCEEDED", f"model call budget reached ({used['calls']}/{max_calls})")
        if max_tokens is not None and used["prompt_tokens"] + used["completion_tokens"] >= max_tokens:
            raise GatewayError("BUDGET_EXCEEDED", f"model token budget reached ({max_tokens})")
        if max_cost is not None and (used["estimated_cost"] or 0) >= max_cost:
            raise GatewayError("BUDGET_EXCEEDED", f"estimated model cost budget reached ({max_cost})")

    def _record(self, task: Task, provider: ModelProvider, model: str, *, status: str, error_code: str | None = None,
                completion: Completion | None = None) -> None:
        cost = None
        params = provider.params or {}
        if completion and params.get("price_per_1k_input") is not None:
            cost = (completion.prompt_tokens / 1000 * float(params.get("price_per_1k_input", 0))
                    + completion.completion_tokens / 1000 * float(params.get("price_per_1k_output", 0)))
        self.db.add(ModelInvocation(
            workspace_id=self.workspace_id, run_id=self.run_id, scope_id=self.scope_id, task=task.name,
            role=task.role, provider_id=provider.id, provider_kind=provider.kind, model=model,
            prompt_version=f"{task.name}@{task.version}", status=status, error_code=error_code,
            prompt_tokens=completion.prompt_tokens if completion else 0,
            completion_tokens=completion.completion_tokens if completion else 0,
            tokens_estimated=completion.tokens_estimated if completion else True,
            latency_ms=completion.latency_ms if completion else 0, estimated_cost=cost,
        ))
        self.db.flush()

    # -- execution -------------------------------------------------------------------------

    def _complete(self, provider: ModelProvider, policy: ModelRolePolicy, task: Task, inputs: dict[str, Any],
                  messages: list[dict[str, str]]) -> Completion:
        model = policy.model or provider.default_model or "default"
        if provider.kind == "fake":
            started = time.perf_counter()
            text = json.dumps(fake_complete(task.name, inputs))
            return Completion(text, _estimate_tokens(json.dumps(messages)), _estimate_tokens(text), True,
                              int((time.perf_counter() - started) * 1000), "fake-deterministic")
        if provider.kind != "openai_compatible":
            raise GatewayError("MODEL_NOT_CONFIGURED", f"unknown provider kind '{provider.kind}'")
        egress = self._policy_for(provider)
        try:
            # Checked before any customer content leaves the process.
            check_url(provider.base_url or "", egress)
        except EgressBlocked as exc:
            raise GatewayError("EGRESS_BLOCKED", exc.reason) from exc
        with _semaphore_lock:
            sem = _semaphores.setdefault(
                provider.id, threading.BoundedSemaphore(int((provider.params or {}).get("max_concurrency", 4))))
        with sem:
            return _openai_complete(provider, self._api_key(provider), egress, model=model, messages=messages,
                                    temperature=policy.temperature, max_tokens=policy.max_tokens,
                                    timeout=policy.timeout_s)

    def generate(self, task_name: str, inputs: dict[str, Any], *, require_preflight: bool = True) -> tuple[Any, CallMeta]:
        """Run one task and return ``(validated_output, meta)``. Raises GatewayError; never falls back."""
        task = TASKS[task_name]
        provider, policy = self.resolve(task.role)
        if require_preflight and not (provider.last_preflight and provider.last_preflight.get("ok")):
            raise GatewayError("PREFLIGHT_REQUIRED",
                               f"provider '{provider.name}' has not passed preflight; verify it before running AI-assisted steps")
        messages = render(task, inputs)
        model = policy.model or provider.default_model or "default"
        max_retries = int((provider.params or {}).get("max_retries", 2))
        last_error: GatewayError | None = None
        for attempt in range(max_retries + 1):
            self._check_budget()
            try:
                completion = self._complete(provider, policy, task, inputs, messages)
            except GatewayError as exc:
                self._record(task, provider, model, status="error", error_code=exc.code)
                last_error = exc
                if not exc.retryable:
                    raise
                time.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
                continue
            try:
                parsed = task.schema.model_validate(extract_json(completion.text))
            except (ValueError, ValidationError) as exc:
                if completion.truncated:
                    # Asking again with the same limit would cut the answer at the same place.
                    self._record(task, provider, completion.model, status="error", error_code="OUTPUT_TRUNCATED",
                                 completion=completion)
                    raise GatewayError("OUTPUT_TRUNCATED", TRUNCATED_HELP.format(limit=policy.max_tokens)) from exc
                self._record(task, provider, completion.model, status="invalid_output", error_code="INVALID_OUTPUT",
                             completion=completion)
                last_error = GatewayError("INVALID_OUTPUT", f"model reply did not match the required schema: {str(exc)[:200]}")
                messages = render(task, inputs) + [
                    {"role": "assistant", "content": completion.text[:2000]},
                    {"role": "user", "content": "That reply was not valid JSON for the schema. Reply again with only the JSON object."},
                ]
                continue
            self._record(task, provider, completion.model, status="ok", completion=completion)
            return parsed, CallMeta(provider.kind == "fake", provider.id, provider.kind, completion.model,
                                    f"{task.name}@{task.version}", task.role)
        assert last_error is not None
        raise last_error

    # -- preflight -------------------------------------------------------------------------

    def preflight(self, provider: ModelProvider) -> dict[str, Any]:
        """Verify that a provider is reachable, allowed by policy and able to return structured output."""
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> bool:
            checks.append({"name": name, "ok": ok, "detail": detail})
            return ok

        ok = True
        offline = effective_offline(self.workspace)
        if provider.kind == "fake":
            add("network_policy", True, "The fake provider runs in-process and sends nothing over the network.")
            add("capability", True, "SYNTHETIC provider: exercises the pipeline only; it is not a real evaluator.")
        else:
            try:
                destination = check_url(provider.base_url or "", self._policy_for(provider))
                add("network_policy", True, f"destination class: {destination}; workspace is "
                    f"{'offline (local only)' if offline else 'connected'}")
            except EgressBlocked as exc:
                ok = add("network_policy", False, f"{exc.reason}. Offline workspaces may only use local or allowlisted endpoints.")
        if ok:
            nonce = uuid.uuid4().hex[:8]
            temp_policy = ModelRolePolicy(workspace_id=self.workspace_id, role="preflight", provider_id=provider.id,
                                          model=provider.default_model, temperature=0.0,
                                          # room for a model that reasons before it answers
                                          max_tokens=int((provider.params or {}).get("preflight_max_tokens", 1024)),
                                          # a local model can need minutes to load into memory on its first call
                                          timeout_s=float((provider.params or {}).get("preflight_timeout_s", 300)))
            task = TASKS["preflight"]
            try:
                completion = self._complete(provider, temp_policy, task, {"nonce": nonce}, render(task, {"nonce": nonce}))
                add("connectivity", True, f"model '{completion.model}' answered in {completion.latency_ms} ms")
                try:
                    echo = task.schema.model_validate(extract_json(completion.text))
                    ok = add("structured_output", bool(getattr(echo, "ok", False)) and getattr(echo, "echo", "") == nonce,
                             "returned valid JSON matching the schema" if getattr(echo, "echo", "") == nonce
                             else "JSON was valid but the echoed value was wrong; the model may not follow instructions reliably")
                except (ValueError, ValidationError):
                    ok = add("structured_output", False,
                             TRUNCATED_HELP.format(limit=temp_policy.max_tokens) if completion.truncated
                             else "the reply was not valid JSON for the requested schema")
                self._record(task, provider, completion.model, status="ok" if ok else "invalid_output", completion=completion)
            except GatewayError as exc:
                ok = add("connectivity", False, f"{exc.code}: {exc.message}")
        context = (provider.params or {}).get("context_window")
        add("context_window", True, f"{context} tokens (declared)" if context else
            "not declared; set params.context_window so long documents can be sized safely")
        result = {"ok": ok, "checks": checks, "checked_at": utcnow().isoformat(), "offline": offline,
                  "synthetic": provider.kind == "fake"}
        provider.last_preflight = result
        self.db.flush()
        return result
