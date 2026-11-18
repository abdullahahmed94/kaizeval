"""Structured outputs that model roles must return. Every model response is validated against one."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

FAILURE_CATEGORIES = [
    "instruction_prompt", "retrieval_grounding", "missing_kb_evidence", "citation",
    "output_formatting", "tool_calling", "orchestration", "unknown",
]


class Objective(BaseModel):
    id: str = ""
    statement: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    source: Literal["requirement", "inferred"] = "inferred"
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class SpecDraft(BaseModel):
    purpose: str
    objectives: list[Objective] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class GeneratedQuestion(BaseModel):
    chunk_ref: str
    question: str
    category: Literal["knowledge", "citation", "edge_case", "multi_turn"] = "knowledge"
    severity: Literal["low", "normal", "high", "critical"] = "normal"
    follow_up: str | None = None


class GeneratedQuestions(BaseModel):
    questions: list[GeneratedQuestion] = Field(default_factory=list)


class UnanswerableQuestion(BaseModel):
    question: str
    reason: str = ""


class UnanswerableQuestions(BaseModel):
    questions: list[UnanswerableQuestion] = Field(default_factory=list)


class Variant(BaseModel):
    case_ref: str
    language: str
    question: str


class Variants(BaseModel):
    variants: list[Variant] = Field(default_factory=list)


class ReferenceDraft(BaseModel):
    ref: str
    sufficient: bool = True
    answer: str = ""
    key_facts: list[str] = Field(default_factory=list)
    evidence_quote: str = ""


class ReferenceDrafts(BaseModel):
    drafts: list[ReferenceDraft] = Field(default_factory=list)


class Judgment(BaseModel):
    # Required and non-empty: an empty object must be rejected, not read as a judgment.
    scores: dict[str, float] = Field(min_length=1)
    verdict: Literal["pass", "fail", "uncertain"] = "uncertain"
    confidence: float = 0.5
    rationale: str = ""
    # Required evidence when "abstention" is scored above 0.5: the sentence of the response that declines.
    refusal_quote: str = ""

    @field_validator("scores")
    @classmethod
    def _clamp(cls, value: dict[str, float]) -> dict[str, float]:
        return {k: max(0.0, min(1.0, float(v))) for k, v in value.items()}

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class ArbiterDecision(Judgment):
    needs_human_review: bool = False


class RootCauseFinding(BaseModel):
    case_ref: str
    category: str = "unknown"
    hypothesis: str = ""
    confidence: float = 0.3

    @field_validator("category")
    @classmethod
    def _known(cls, value: str) -> str:
        return value if value in FAILURE_CATEGORIES else "unknown"


class RootCauses(BaseModel):
    findings: list[RootCauseFinding] = Field(default_factory=list)


class OptimizerProposal(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    expected_impact: str = ""
    risk: Literal["low", "medium", "high"] = "low"


class PreflightEcho(BaseModel):
    ok: bool
    echo: str
