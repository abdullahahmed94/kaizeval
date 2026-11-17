"""Document ingestion: parse PDF, DOCX, Markdown and plain text into traceable evidence chunks.

Document content is evidence only. Nothing in a document is ever executed or treated as an
instruction; passages that look like prompt injection are flagged so reviewers can see them.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, EvidenceChunk, TestCase
from .storage import get_storage, object_key
from .textutil import injection_flags

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MAX_CHUNK_CHARS = 1200


class DocumentError(Exception):
    pass


@dataclass
class Segment:
    text: str
    page: int | None = None
    section: str | None = None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1256", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentError("could not decode text file")


def _parse_markdown(text: str) -> list[Segment]:
    segments: list[Segment] = []
    path: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            segments.append(Segment(body, section=" > ".join(path) or None))
        buffer.clear()

    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            flush()
            level = len(match.group(1))
            path[:] = path[: level - 1] + [match.group(2).strip()]
        else:
            buffer.append(line)
    flush()
    return segments


def _parse_text(text: str) -> list[Segment]:
    return [Segment(p.strip()) for p in re.split(r"\n\s*\n", text) if p.strip()]


def _parse_pdf(data: bytes) -> list[Segment]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("encrypted PDF files are not supported")
        segments = []
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                segments.append(Segment(text, page=number))
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - parser boundary
        raise DocumentError(f"could not read PDF: {type(exc).__name__}") from exc
    if not segments:
        raise DocumentError("the PDF has no extractable text (scanned documents need OCR first)")
    return segments


def _parse_docx(data: bytes) -> list[Segment]:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - parser boundary
        raise DocumentError(f"could not read DOCX: {type(exc).__name__}") from exc
    segments: list[Segment] = []
    section: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            segments.append(Segment(body, section=section))
        buffer.clear()

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style_name = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style_name.lower().startswith(("heading", "title")):
            flush()
            section = text
        else:
            buffer.append(text)
    flush()
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        if any(rows):
            segments.append(Segment("\n".join(rows), section=section))
    return segments


def parse(filename: str, data: bytes) -> list[Segment]:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in SUPPORTED:
        raise DocumentError(f"unsupported file type '{suffix or filename}'. Supported: PDF, DOCX, Markdown, TXT")
    if not data:
        raise DocumentError("the file is empty")
    if suffix == ".pdf":
        return _parse_pdf(data)
    if suffix == ".docx":
        return _parse_docx(data)
    text = _decode(data)
    segments = _parse_markdown(text) if suffix in (".md", ".markdown") else _parse_text(text)
    if not segments:
        raise DocumentError("the document contains no text")
    return segments


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for piece in re.split(r"(?<=[.!?؟])\s+|\n+", text):
        if current and len(current) + len(piece) + 1 > limit:
            parts.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
        while len(current) > limit:
            parts.append(current[:limit])
            current = current[limit:]
    if current:
        parts.append(current)
    return parts


def chunk(segments: list[Segment]) -> list[dict]:
    """Turn segments into chunks with character offsets into the concatenated document text."""
    chunks, offset = [], 0
    for segment in segments:
        for part in _split(segment.text, MAX_CHUNK_CHARS):
            chunks.append({"text": part, "page": segment.page, "section": segment.section,
                           "char_start": offset, "char_end": offset + len(part), "flags": injection_flags(part)})
            offset += len(part) + 1
    return chunks


def ingest(db: Session, *, workspace_id: str, filename: str, data: bytes, kind: str = "kb",
           content_type: str = "") -> Document:
    """Store the original file, parse it and persist evidence chunks. Re-uploading a changed file
    with the same name creates a new version and marks test cases built on the old one as stale."""
    sha = hashlib.sha256(data).hexdigest()
    previous = db.scalar(
        select(Document).where(Document.workspace_id == workspace_id, Document.filename == filename,
                               Document.kind == kind, Document.status != "superseded")
        .order_by(Document.version.desc())
    )
    if previous is not None and previous.sha256 == sha and previous.status == "parsed":
        return previous
    document = Document(workspace_id=workspace_id, filename=filename, kind=kind, content_type=content_type,
                        sha256=sha, size_bytes=len(data), storage_key="",
                        version=(previous.version + 1) if previous else 1,
                        supersedes_id=previous.id if previous else None)
    db.add(document)
    db.flush()
    document.storage_key = object_key(workspace_id, "documents", document.id, filename)
    get_storage().put(document.storage_key, data)
    try:
        chunks = chunk(parse(filename, data))
    except DocumentError as exc:
        document.status, document.error = "failed", str(exc)
        db.flush()
        return document
    for ordinal, item in enumerate(chunks):
        db.add(EvidenceChunk(workspace_id=workspace_id, document_id=document.id, ordinal=ordinal, **item))
    document.chunk_count = len(chunks)
    if previous is not None:
        previous.status = "superseded"
        mark_stale(db, workspace_id, previous.id)
    db.flush()
    return document


def mark_stale(db: Session, workspace_id: str, old_document_id: str) -> int:
    """Flag test cases whose evidence points at a superseded document version."""
    count = 0
    for case in db.scalars(select(TestCase).where(TestCase.workspace_id == workspace_id, TestCase.stale.is_(False))):
        if any(ref.get("document_id") == old_document_id for ref in case.evidence_refs or []):
            case.stale = True
            count += 1
    return count


def chunk_ref(chunk_row: EvidenceChunk) -> str:
    return f"chunk:{chunk_row.id}"
