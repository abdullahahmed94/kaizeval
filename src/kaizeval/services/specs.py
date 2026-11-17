"""Versioned agent specifications built from business requirements, inference, or both."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..connectors import AdapterError, build_adapter
from ..gateway import Gateway, GatewayError
from ..ground_truth import current_kb_chunks
from ..models import Agent, Document, EvidenceChunk, Specification, uid, utcnow


class SpecError(Exception):
    pass


def _requirements_text(db: Session, workspace_id: str, text: str | None, document_ids: list[str]) -> str:
    parts = [text.strip()] if text and text.strip() else []
    if document_ids:
        rows = db.execute(
            select(EvidenceChunk.text).join(Document, Document.id == EvidenceChunk.document_id)
            .where(Document.workspace_id == workspace_id, Document.id.in_(document_ids))
            .order_by(Document.filename, EvidenceChunk.ordinal)
        ).all()
        parts.extend(r[0] for r in rows)
    return "\n".join(parts)[:20000]


def _agent_profile(db: Session, agent: Agent | None) -> dict[str, Any] | None:
    if agent is None or "inspect" not in (agent.capabilities or []):
        return None
    try:
        adapter = build_adapter(db, agent)
        info = adapter.inspect("baseline")
    except AdapterError:
        return None
    return {"description": info.get("description", ""),
            "settings": {k: v.get("description", "") for k, v in (info.get("schema") or {}).items()}}


def _fallback_from_requirements(requirements: str) -> dict[str, Any]:
    objectives: list[dict[str, Any]] = []
    for line in re.split(r"[\n;]+", requirements):
        line = line.strip(" -*\t0123456789.)")
        if len(line) > 8:
            objectives.append({"id": f"R{len(objectives) + 1}", "statement": line, "source": "requirement",
                               "acceptance_criteria": [], "evidence_refs": [], "confidence": 1.0})
    return {"purpose": "As stated in the supplied business requirements.", "objectives": objectives,
            "constraints": [], "languages": [],
            "assumptions": ["No model was configured, so requirements were split line by line without interpretation."],
            "open_questions": ["Acceptance criteria were not derived; add them during review."]}


def build_specification(db: Session, *, workspace_id: str, agent_id: str | None, requirements_text: str | None,
                        requirement_document_ids: list[str], infer: bool, user_id: str | None,
                        run_id: str | None = None) -> Specification:
    agent = db.get(Agent, agent_id) if agent_id else None
    if agent is not None and agent.workspace_id != workspace_id:
        raise SpecError("agent not found")
    requirements = _requirements_text(db, workspace_id, requirements_text, requirement_document_ids)
    gateway = Gateway(db, workspace_id, run_id=run_id)
    chunks = current_kb_chunks(db, workspace_id)
    step = max(1, len(chunks) // 12)
    sample = [{"ref": f"chunk:{c.id}", "section": c.section, "document": d.filename, "text": c.text[:700]}
              for c, d in chunks[::step][:12]]
    known_refs = {f"chunk:{c.id}" for c, _ in chunks}
    synthetic = False

    if gateway.is_configured("specification_inference") and (infer or requirements):
        try:
            draft, meta = gateway.generate("spec.infer", {
                "requirements": requirements or None,
                "chunks": sample if infer else [],
                "agent_profile": _agent_profile(db, agent) if infer else None,
            })
        except GatewayError as exc:
            raise SpecError(str(exc)) from exc
        content = draft.model_dump()
        synthetic = meta.synthetic
        for index, objective in enumerate(content["objectives"], start=1):
            objective["id"] = objective.get("id") or f"O{index}"
            valid = [r for r in objective.get("evidence_refs", []) if r in known_refs]
            if objective["source"] == "inferred" and len(valid) < len(objective.get("evidence_refs", [])):
                objective["confidence"] = min(objective.get("confidence", 0.5), 0.3)
            objective["evidence_refs"] = valid
    elif requirements:
        content = _fallback_from_requirements(requirements)
    else:
        raise SpecError("MODEL_NOT_CONFIGURED: inferring a specification needs a verified model. "
                        "Configure and verify a model, or provide business requirements.")

    sources = {o["source"] for o in content["objectives"]}
    source = "business" if sources == {"requirement"} else "inferred" if "requirement" not in sources else "mixed"
    content["provisional"] = source != "business"
    notes = []
    if source != "business":
        notes.append("Inferred objectives are assumptions until a person reviews them.")
    if infer and len(chunks) < 3:
        notes.append("Very little knowledge content was available; the inferred specification is low-confidence.")
    if synthetic:
        notes.append("Produced by the fake provider for demonstration; not a semantic analysis.")
    content["uncertainty"] = notes

    spec = Specification(workspace_id=workspace_id, agent_id=agent_id, lineage_id=uid(), version=1, source=source,
                         status="draft", content=content, synthetic=synthetic, created_by=user_id)
    db.add(spec)
    db.flush()
    audit.record(db, workspace_id=workspace_id, user_id=user_id, action="specification.built",
                 subject_type="specification", subject_id=spec.id, detail={"source": source, "synthetic": synthetic})
    return spec


def edit_specification(db: Session, spec: Specification, *, content: dict[str, Any], user_id: str,
                       approve: bool = False) -> Specification:
    """Edits never overwrite: they create the next version in the same lineage."""
    latest = db.scalar(select(Specification).where(Specification.lineage_id == spec.lineage_id)
                       .order_by(Specification.version.desc()))
    assert latest is not None
    merged = {**spec.content, **content}
    new = Specification(workspace_id=spec.workspace_id, agent_id=spec.agent_id, lineage_id=spec.lineage_id,
                        version=latest.version + 1, parent_id=spec.id, source=spec.source, status="draft",
                        content=merged, synthetic=spec.synthetic, created_by=user_id)
    db.add(new)
    db.flush()
    audit.record(db, workspace_id=spec.workspace_id, user_id=user_id, action="specification.edited",
                 subject_type="specification", subject_id=new.id, detail={"from_version": spec.version, "to_version": new.version})
    if approve:
        review_specification(db, new, decision="approve", user_id=user_id)
    return new


def review_specification(db: Session, spec: Specification, *, decision: str, user_id: str, note: str | None = None) -> Specification:
    """Record the customer's choice: 'approve' (Review & Edit path) or 'skip' (Skip & Continue)."""
    if decision not in ("approve", "skip"):
        raise SpecError("decision must be 'approve' or 'skip'")
    spec.status = "reviewed" if decision == "approve" else "skipped"
    spec.review = {"decision": decision, "by": user_id, "at": utcnow().isoformat(), "note": note}
    if decision == "approve":
        content = dict(spec.content)
        content["provisional"] = False
        spec.content = content
    db.flush()
    audit.record(db, workspace_id=spec.workspace_id, user_id=user_id, action=f"specification.{decision}",
                 subject_type="specification", subject_id=spec.id, detail={"version": spec.version})
    return spec
