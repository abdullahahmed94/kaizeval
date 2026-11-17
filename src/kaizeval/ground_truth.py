"""Ground-truth providers.

A provider supplies reference evidence and decides how far a drafted reference answer can be
trusted. Release 0.1 implements documents; SQL and API providers are typed extension points only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, EvidenceChunk
from .textutil import contains_fact, normalize


@dataclass
class Verification:
    # draft: linked to verbatim evidence, not yet approved by a person
    # unverified: no usable evidence link
    # needs_human_review: evidence and draft disagree
    status: str
    key_facts: list[str] = field(default_factory=list)
    detail: str = ""


class GroundTruthProvider(ABC):
    name = "base"

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def evidence_ref(self, source_id: str, quote: str = "") -> dict[str, Any] | None:
        """Return a provenance record for a source item, or None when it does not exist."""

    @abstractmethod
    def verify_reference(self, source_id: str, *, answer: str, key_facts: list[str], quote: str,
                         sufficient: bool = True) -> Verification: ...


class DocumentGroundTruthProvider(GroundTruthProvider):
    """Evidence comes from parsed document chunks; verification is a verbatim text check."""

    name = "document"

    def __init__(self, db: Session, workspace_id: str):
        self.db = db
        self.workspace_id = workspace_id

    def available(self) -> bool:
        return self.db.scalar(select(EvidenceChunk.id).where(EvidenceChunk.workspace_id == self.workspace_id).limit(1)) is not None

    def chunk(self, chunk_id: str) -> EvidenceChunk | None:
        row = self.db.get(EvidenceChunk, chunk_id)
        return row if row is not None and row.workspace_id == self.workspace_id else None

    def evidence_ref(self, source_id: str, quote: str = "") -> dict[str, Any] | None:
        row = self.chunk(source_id)
        if row is None:
            return None
        document = self.db.get(Document, row.document_id)
        assert document is not None
        return {
            "provider": self.name, "chunk_id": row.id, "document_id": document.id, "filename": document.filename,
            "document_version": document.version, "sha256": document.sha256, "page": row.page,
            "section": row.section, "char_start": row.char_start, "char_end": row.char_end, "quote": quote,
        }

    def verify_reference(self, source_id: str, *, answer: str, key_facts: list[str], quote: str,
                         sufficient: bool = True) -> Verification:
        row = self.chunk(source_id)
        if row is None:
            return Verification("unverified", detail="the cited passage does not exist")
        if not sufficient or not answer.strip():
            return Verification("unverified", detail="the drafting model reported insufficient evidence")
        quote_ok = bool(quote.strip()) and normalize(quote) in normalize(row.text)
        kept = [f for f in key_facts if f.strip() and contains_fact(row.text, f)]
        dropped = len([f for f in key_facts if f.strip()]) - len(kept)
        if not quote_ok:
            return Verification("unverified", kept, "the supporting quote was not found verbatim in the passage")
        if not kept:
            return Verification("unverified", [], "no key fact could be located verbatim in the passage")
        if dropped > len(kept):
            return Verification("needs_human_review", kept,
                                f"{dropped} drafted key fact(s) are not in the passage; a person should confirm the reference")
        return Verification("draft", kept, "quote and key facts found verbatim in the cited passage")


class _FutureProvider(GroundTruthProvider):
    """Typed placeholder. Present so integrations can target the contract; not implemented in 0.1."""

    def available(self) -> bool:
        return False

    def evidence_ref(self, source_id: str, quote: str = "") -> dict[str, Any] | None:
        raise NotImplementedError(f"{self.name} ground truth is on the roadmap and not implemented in this release")

    def verify_reference(self, source_id: str, *, answer: str, key_facts: list[str], quote: str,
                         sufficient: bool = True) -> Verification:
        raise NotImplementedError(f"{self.name} ground truth is on the roadmap and not implemented in this release")


class SQLGroundTruthProvider(_FutureProvider):
    name = "sql"


class APIGroundTruthProvider(_FutureProvider):
    name = "api"


def current_kb_chunks(db: Session, workspace_id: str, document_ids: list[str] | None = None) -> list[tuple[EvidenceChunk, Document]]:
    """Chunks of the latest parsed version of each knowledge document, in a stable order."""
    q = (select(EvidenceChunk, Document).join(Document, Document.id == EvidenceChunk.document_id)
         .where(Document.workspace_id == workspace_id, Document.kind == "kb", Document.status == "parsed")
         .order_by(Document.filename, EvidenceChunk.ordinal))
    if document_ids:
        q = q.where(Document.id.in_(document_ids))
    return [(c, d) for c, d in db.execute(q).all()]
