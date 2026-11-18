"""Relational model. Every customer-owned row carries ``workspace_id`` and is queried through it."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    """ISO-8601 with an explicit UTC offset (SQLite returns naive datetimes; they are stored in UTC)."""
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class _Row:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class _Scoped(_Row):
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), index=True)


# --- identity -----------------------------------------------------------------------------


class User(_Row, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Workspace(_Row, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(200))
    # offline: only local/private model and agent endpoints. connected: owner-approved egress.
    mode: Mapped[str] = mapped_column(String(20), default="offline")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Membership(_Row, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id"),)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # owner | editor | reviewer | viewer


class Secret(_Scoped, Base):
    """An encrypted credential. Only ever referenced by id; the value is never returned by the API."""

    __tablename__ = "secrets"
    name: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[str] = mapped_column(Text)


# --- model execution ----------------------------------------------------------------------


class ModelProvider(_Scoped, Base):
    __tablename__ = "model_providers"
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))  # fake | openai_compatible
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("secrets.id"), nullable=True)
    default_model: Mapped[str] = mapped_column(String(200), default="")
    # Owner opt-in to send workspace content to a public (non-local) destination.
    allow_egress: Mapped[bool] = mapped_column(Boolean, default=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_preflight: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class ModelRolePolicy(_Scoped, Base):
    __tablename__ = "model_role_policies"
    __table_args__ = (UniqueConstraint("workspace_id", "role"),)
    role: Mapped[str] = mapped_column(String(40))  # one of gateway.ROLES or "default"
    provider_id: Mapped[str] = mapped_column(String(32), ForeignKey("model_providers.id"))
    model: Mapped[str] = mapped_column(String(200), default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.0)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2000)
    timeout_s: Mapped[float] = mapped_column(Float, default=300.0)


class ModelInvocation(_Scoped, Base):
    __tablename__ = "model_invocations"
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    # Budget scope: the optimization run when the call belongs to one, otherwise the run itself.
    scope_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    task: Mapped[str] = mapped_column(String(60), default="")
    role: Mapped[str] = mapped_column(String(40))
    provider_id: Mapped[str] = mapped_column(String(32))
    provider_kind: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(30))  # ok | invalid_output | error
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)


# --- agents -------------------------------------------------------------------------------


class Agent(_Scoped, Base):
    __tablename__ = "agents"
    name: Mapped[str] = mapped_column(String(200))
    adapter: Mapped[str] = mapped_column(String(40))  # http_chat | local_config
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secret_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("secrets.id"), nullable=True)
    # black_box | read_only | candidate_write : what the customer granted, not what the adapter can do
    access_level: Mapped[str] = mapped_column(String(30), default="black_box")
    allow_promote: Mapped[bool] = mapped_column(Boolean, default=False)
    capabilities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verification: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


# --- knowledge and specification ----------------------------------------------------------


class Document(_Scoped, Base):
    __tablename__ = "documents"
    filename: Mapped[str] = mapped_column(String(400))
    kind: Mapped[str] = mapped_column(String(30), default="kb")  # kb | requirements
    content_type: Mapped[str] = mapped_column(String(100), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="parsed")  # parsed | failed | superseded
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str] = mapped_column(String(600))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EvidenceChunk(_Scoped, Base):
    __tablename__ = "evidence_chunks"
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(400), nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    flags: Mapped[list[Any]] = mapped_column(JSON, default=list)  # e.g. ["possible_injection"]


class Specification(_Scoped, Base):
    __tablename__ = "specifications"
    agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lineage_id: Mapped[str] = mapped_column(String(32), index=True)  # shared by all versions
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(30))  # business | inferred | mixed
    # draft | reviewed | skipped
    status: Mapped[str] = mapped_column(String(30), default="draft")
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


# --- datasets -----------------------------------------------------------------------------


class Dataset(_Scoped, Base):
    __tablename__ = "datasets"
    name: Mapped[str] = mapped_column(String(200))
    agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    specification_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="generating")  # generating | ready | failed
    version: Mapped[int] = mapped_column(Integer, default=1)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    holdout_fraction: Mapped[float] = mapped_column(Float, default=0.3)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when a person opened holdout items or a holdout result steered further tuning.
    holdout_exposed: Mapped[bool] = mapped_column(Boolean, default=False)
    holdout_generation: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[list[Any]] = mapped_column(JSON, default=list)


class TestCase(_Scoped, Base):
    __tablename__ = "test_cases"
    __test__ = False  # not a pytest class
    dataset_id: Mapped[str] = mapped_column(String(32), ForeignKey("datasets.id"), index=True)
    split: Mapped[str] = mapped_column(String(20), index=True)  # regression | holdout | retired
    holdout_generation: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(20), default="normal")  # low | normal | high | critical
    language: Mapped[str] = mapped_column(String(20), default="en")
    turns: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{"role": "user", "content": ...}, ...]
    # {"behavior": "answer"|"abstain", "answer": str, "key_facts": [str], "follow_up_key_facts": [str]}
    expected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # approved | verified | draft | unverified | needs_human_review
    answer_status: Mapped[str] = mapped_column(String(30), default="draft")
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


# --- evaluation ---------------------------------------------------------------------------


class EvaluationRun(_Scoped, Base):
    __tablename__ = "evaluation_runs"
    agent_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(32), ForeignKey("datasets.id"), index=True)
    dataset_version: Mapped[int] = mapped_column(Integer, default=1)
    split: Mapped[str] = mapped_column(String(20))  # regression | holdout
    kind: Mapped[str] = mapped_column(String(30), default="baseline")  # baseline | candidate | holdout
    agent_version: Mapped[str] = mapped_column(String(80), default="baseline")
    agent_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="single")  # deterministic | single | dual
    status: Mapped[str] = mapped_column(String(30), default="queued")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    optimization_run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseResult(_Scoped, Base):
    __tablename__ = "case_results"
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("evaluation_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(32), ForeignKey("test_cases.id"), index=True)
    split: Mapped[str] = mapped_column(String(20))
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    agent_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deterministic: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    judgments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    final: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(30), default="fail")  # pass | fail | needs_human_review | n/a


class Finding(_Scoped, Base):
    __tablename__ = "findings"
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("evaluation_runs.id"), index=True)
    case_result_id: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(20))
    observation: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(20), default="rule")  # rule | model


class Recommendation(_Scoped, Base):
    __tablename__ = "recommendations"
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("evaluation_runs.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40))
    target_component: Mapped[str] = mapped_column(String(80))
    impact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(20), default="low")
    required_capability: Mapped[str] = mapped_column(String(40), default="")
    auto_apply_available: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_apply_blocker: Mapped[str | None] = mapped_column(String(200), nullable=True)
    suggested_patch: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # proposed | approved | deferred | rejected | applied
    state: Mapped[str] = mapped_column(String(20), default="proposed")
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


# --- optimization -------------------------------------------------------------------------


class Candidate(_Scoped, Base):
    __tablename__ = "candidates"
    agent_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    # active | rejected | awaiting_decision | promoted | rolled_back
    status: Mapped[str] = mapped_column(String(30), default="active")
    baseline_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    baseline_digest: Mapped[str] = mapped_column(String(64), default="")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    optimization_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CandidatePatch(_Scoped, Base):
    __tablename__ = "candidate_patches"
    candidate_id: Mapped[str] = mapped_column(String(32), ForeignKey("candidates.id"), index=True)
    round: Mapped[int] = mapped_column(Integer, default=0)
    patch: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diff: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(String(20), default="low")
    # proposed | approved | rejected | applied | kept | rolled_back
    state: Mapped[str] = mapped_column(String(20), default="proposed")
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


class OptimizationRun(_Scoped, Base):
    __tablename__ = "optimization_runs"
    agent_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(32), ForeignKey("datasets.id"))
    mode: Mapped[str] = mapped_column(String(30))  # human_guided | autonomous
    status: Mapped[str] = mapped_column(String(30), default="queued")
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    baseline_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    best_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    holdout: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Experiment(_Scoped, Base):
    """A paired comparison of two runs over the same cases."""

    __tablename__ = "experiments"
    optimization_run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    round: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="regression")  # regression | holdout
    baseline_run_id: Mapped[str] = mapped_column(String(32))
    candidate_run_id: Mapped[str] = mapped_column(String(32))
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(30), default="")  # kept | rolled_back | final


class Approval(_Scoped, Base):
    __tablename__ = "approvals"
    subject_type: Mapped[str] = mapped_column(String(40))  # candidate_patch | candidate
    subject_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(30))  # patch | promote
    decision: Mapped[str] = mapped_column(String(20))  # approved | rejected
    user_id: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(_Scoped, Base):
    __tablename__ = "audit_events"
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    subject_type: Mapped[str] = mapped_column(String(40), default="")
    subject_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Job(_Scoped, Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued | running | review_required | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
