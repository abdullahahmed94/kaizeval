"""Make your own agent optimizable in a few lines.

Kaizeval can only change an agent that gives it a safe way to do so. This module adds that way
(the "candidate protocol") to any FastAPI agent:

    from fastapi import FastAPI
    from kaizeval.agentkit import CandidateStore

    store = CandidateStore(
        defaults={"system_prompt": "You are a support assistant.", "top_k": 3, "cite_sources": False},
        hints={"system_prompt": {"addresses": ["instruction_prompt", "citation"], "max_length": 4000},
               "top_k": {"min": 1, "max": 8, "addresses": ["retrieval_grounding"]}},
        state_file="agent-candidates.json",
    )
    app = FastAPI()
    app.include_router(store.router(admin_token="choose-a-secret"))

    @app.post("/chat")
    def chat(body: dict):
        version, settings = store.settings(body.get("version"))   # baseline, a candidate, or production
        answer = my_agent(body["message"], **settings)
        return {"answer": answer, "citations": [], "version": version, "config_digest": store.digest(version)}

What it guarantees:

* the baseline (the `defaults` you pass) has no endpoint that can modify it;
* every candidate is an independent copy; patches are validated against the declared settings;
* promotion only moves a pointer and needs an approval reference, so going back to the
  baseline is always possible.

Then register the agent in Kaizeval with `candidate_protocol: true` (project file) or the
"Local candidate protocol" adapter (console).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

FAILURE_CATEGORIES = ("instruction_prompt", "retrieval_grounding", "missing_kb_evidence", "citation",
                      "output_formatting", "tool_calling", "orchestration")


def digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


def _infer(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string", "max_length": 4000}
    raise TypeError(f"unsupported setting type: {type(value).__name__} (use bool, int, float or str)")


class _Patch(BaseModel):
    patch: dict[str, Any]


class _Config(BaseModel):
    config: dict[str, Any]


class _Promote(BaseModel):
    candidate_id: str
    approval_id: str


class CandidateStore:
    """Baseline settings, isolated candidate copies and the production pointer of one agent."""

    def __init__(self, defaults: dict[str, Any], hints: dict[str, dict[str, Any]] | None = None, *,
                 description: str = "", state_file: str | Path | None = None):
        if not defaults:
            raise ValueError("defaults must contain at least one setting")
        self._baseline = dict(defaults)
        self.schema: dict[str, dict[str, Any]] = {}
        for key, value in defaults.items():
            self.schema[key] = {**_infer(value), **((hints or {}).get(key) or {})}
        unknown = set(hints or {}) - set(defaults)
        if unknown:
            raise ValueError(f"hints for unknown settings: {sorted(unknown)}")
        self.description = description
        self.state_file = Path(state_file) if state_file else None
        self.lock = threading.Lock()
        self.candidates: dict[str, dict[str, Any]] = {}
        self.production = "baseline"
        self.promotions: list[dict[str, Any]] = []
        if self.state_file and self.state_file.exists():
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.candidates = data.get("candidates", {})
            self.production = data.get("production", "baseline")
            self.promotions = data.get("promotions", [])

    # -- reading ---------------------------------------------------------------------------

    def settings(self, version: str | None = None) -> tuple[str, dict[str, Any]]:
        """Return ``(version, settings)``. ``None`` means the production version."""
        version = version or self.production
        if version == "baseline":
            return "baseline", dict(self._baseline)
        if version not in self.candidates:
            raise KeyError(version)
        return version, dict(self.candidates[version])

    def digest(self, version: str | None = None) -> str:
        return digest(self.settings(version)[1])

    # -- validation ------------------------------------------------------------------------

    def validate(self, patch: dict[str, Any]) -> None:
        for key, value in patch.items():
            meta = self.schema.get(key)
            if meta is None:
                raise ValueError(f"'{key}' is not a patchable setting")
            kind = meta["type"]
            if kind == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"'{key}' must be true or false")
            elif kind == "string":
                if not isinstance(value, str):
                    raise ValueError(f"'{key}' must be text")
                if len(value) > int(meta.get("max_length", 4000)):
                    raise ValueError(f"'{key}' is longer than {meta.get('max_length', 4000)} characters")
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"'{key}' must be a number")
                if kind == "integer" and int(value) != value:
                    raise ValueError(f"'{key}' must be a whole number")
                if (meta.get("min") is not None and value < meta["min"]) or (meta.get("max") is not None and value > meta["max"]):
                    raise ValueError(f"'{key}' must be between {meta.get('min')} and {meta.get('max')}")

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "candidates": self.candidates, "production": self.production, "promotions": self.promotions,
            }, indent=2), encoding="utf-8")

    # -- the candidate protocol ------------------------------------------------------------

    def router(self, admin_token: str | None) -> APIRouter:
        """The ``/admin`` endpoints Kaizeval uses. Without a token the admin API answers 403."""
        store = self

        def admin(authorization: str | None = Header(default=None)) -> None:
            if not admin_token:
                raise HTTPException(403, "admin API is disabled: start the agent with an admin token")
            if authorization != f"Bearer {admin_token}":
                raise HTTPException(401, "invalid admin token")

        router = APIRouter(prefix="/admin", dependencies=[Depends(admin)])

        @router.get("/schema")
        def schema() -> dict[str, Any]:
            return {"schema": store.schema, "description": store.description}

        @router.get("/config")
        def get_config(version: str = "baseline") -> dict[str, Any]:
            try:
                name, config = store.settings(version)
            except KeyError:
                raise HTTPException(404, f"unknown version '{version}'") from None
            return {"version": name, "config": config, "digest": digest(config)}

        @router.post("/candidates")
        def create_candidate() -> dict[str, Any]:
            with store.lock:
                candidate_id = f"cand-{uuid.uuid4().hex[:10]}"
                store.candidates[candidate_id] = dict(store._baseline)
                store._save()
            return {"candidate_id": candidate_id, "config": dict(store._baseline)}

        @router.patch("/candidates/{candidate_id}")
        def patch_candidate(candidate_id: str, body: _Patch) -> dict[str, Any]:
            if candidate_id == "baseline" or candidate_id not in store.candidates:
                raise HTTPException(404, "unknown candidate (the baseline cannot be patched)")
            try:
                store.validate(body.patch)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            with store.lock:
                before = dict(store.candidates[candidate_id])
                store.candidates[candidate_id].update(body.patch)
                after = dict(store.candidates[candidate_id])
                store._save()
            return {"candidate_id": candidate_id, "before": before, "after": after, "digest": digest(after)}

        @router.put("/candidates/{candidate_id}/config")
        def restore_candidate(candidate_id: str, body: _Config) -> dict[str, Any]:
            if candidate_id == "baseline" or candidate_id not in store.candidates:
                raise HTTPException(404, "unknown candidate (the baseline cannot be changed)")
            if set(body.config) != set(store._baseline):
                raise HTTPException(422, "a restored configuration must contain exactly the known settings")
            try:
                store.validate(body.config)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            with store.lock:
                store.candidates[candidate_id] = dict(body.config)
                store._save()
            return {"candidate_id": candidate_id, "config": dict(body.config), "digest": digest(body.config)}

        @router.post("/promote")
        def promote(body: _Promote) -> dict[str, Any]:
            if not body.approval_id.strip():
                raise HTTPException(422, "promotion requires an approval reference")
            if body.candidate_id != "baseline" and body.candidate_id not in store.candidates:
                raise HTTPException(404, "unknown candidate")
            with store.lock:
                previous = store.production
                store.production = body.candidate_id
                store.promotions.append({"from": previous, "to": body.candidate_id, "approval_id": body.approval_id,
                                         "at": datetime.now(timezone.utc).isoformat()})
                store._save()
            return {"production": store.production, "previous": previous}

        @router.get("/production")
        def production() -> dict[str, Any]:
            return {"production": store.production, "promotions": store.promotions}

        return router
