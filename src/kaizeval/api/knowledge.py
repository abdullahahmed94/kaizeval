"""Knowledge documents, specifications and test datasets."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import audit, jobs
from ..config import get_settings
from ..documents import ingest
from ..models import Agent, Dataset, Document, EvidenceChunk, Specification, TestCase, iso
from ..services import datasets as dataset_service
from ..services import specs as spec_service
from ..services.datasets import HoldoutAccess
from .deps import Ctx, fail, idempotency_key, job_dict, page, scoped, ser, workspace_ctx

router = APIRouter(prefix="/workspaces/{workspace_id}")


async def _read(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise fail(413, "FILE_TOO_LARGE", f"files are limited to {get_settings().max_upload_mb} MB")
    return data


# --- documents ----------------------------------------------------------------------------


@router.post("/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), kind: Literal["kb", "requirements"] = Form("kb"),
                          ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    data = await _read(file)
    document = ingest(ctx.db, workspace_id=ctx.ws, filename=file.filename or "document", data=data, kind=kind,
                      content_type=file.content_type or "")
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="document.uploaded", subject_type="document",
                 subject_id=document.id, detail={"filename": document.filename, "kind": kind, "version": document.version,
                                                 "sha256": document.sha256, "status": document.status})
    ctx.db.commit()
    flagged = ctx.db.scalars(select(EvidenceChunk).where(EvidenceChunk.document_id == document.id)).all()
    return ser(document, exclude={"storage_key"},
               extra={"flagged_chunks": sum(1 for c in flagged if c.flags)})


@router.get("/documents")
def list_documents(kind: str | None = None, include_superseded: bool = False, limit: int = 100, offset: int = 0,
                   ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(Document).where(Document.workspace_id == ctx.ws).order_by(Document.filename, Document.version.desc())
    if kind:
        query = query.where(Document.kind == kind)
    if not include_superseded:
        query = query.where(Document.status != "superseded")
    return page(ctx.db, query, limit=limit, offset=offset, transform=lambda d: ser(d, exclude={"storage_key"}))


@router.get("/documents/{document_id}/chunks")
def document_chunks(document_id: str, limit: int = 100, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    document = scoped(ctx, Document, document_id)
    query = select(EvidenceChunk).where(EvidenceChunk.document_id == document.id).order_by(EvidenceChunk.ordinal)
    result = page(ctx.db, query, limit=limit, offset=offset)
    result["document"] = ser(document, exclude={"storage_key"})
    return result


@router.get("/evidence/{chunk_id}")
def evidence(chunk_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    chunk = scoped(ctx, EvidenceChunk, chunk_id)
    document = ctx.db.get(Document, chunk.document_id)
    return ser(chunk, extra={"filename": document.filename if document else None,
                             "document_version": document.version if document else None,
                             "sha256": document.sha256 if document else None})


# --- specifications -----------------------------------------------------------------------


class SpecBuild(BaseModel):
    agent_id: str | None = None
    requirements_text: str | None = Field(default=None, max_length=50000)
    requirement_document_ids: list[str] = Field(default_factory=list)
    infer: bool = True


class SpecEdit(BaseModel):
    content: dict[str, Any]
    approve: bool = False


class SpecReview(BaseModel):
    decision: Literal["approve", "skip"]
    note: str | None = Field(default=None, max_length=2000)


@router.post("/specifications:build", status_code=202)
def build_spec(body: SpecBuild, request: Request, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    if body.agent_id:
        scoped(ctx, Agent, body.agent_id)
    for document_id in body.requirement_document_ids:
        scoped(ctx, Document, document_id)
    job = jobs.enqueue(ctx.db, workspace_id=ctx.ws, kind="specification.build", payload=body.model_dump(),
                       created_by=ctx.user.id, idempotency_key=idempotency_key(request))
    return job_dict(job)


@router.get("/specifications")
def list_specs(limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(Specification).where(Specification.workspace_id == ctx.ws).order_by(Specification.created_at.desc())
    return page(ctx.db, query, limit=limit, offset=offset)


@router.get("/specifications/{spec_id}")
def get_spec(spec_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    spec = scoped(ctx, Specification, spec_id)
    versions = ctx.db.scalars(select(Specification).where(Specification.lineage_id == spec.lineage_id)
                              .order_by(Specification.version))
    return ser(spec, extra={"versions": [{"id": v.id, "version": v.version, "status": v.status, "source": v.source,
                                          "created_at": iso(v.created_at), "created_by": v.created_by}
                                         for v in versions]})


@router.patch("/specifications/{spec_id}")
def edit_spec(spec_id: str, body: SpecEdit, ctx: Ctx = Depends(workspace_ctx("reviewer"))) -> dict[str, Any]:
    spec = scoped(ctx, Specification, spec_id)
    new = spec_service.edit_specification(ctx.db, spec, content=body.content, user_id=ctx.user.id, approve=body.approve)
    ctx.db.commit()
    return ser(new)


@router.post("/specifications/{spec_id}/review")
def review_spec(spec_id: str, body: SpecReview, ctx: Ctx = Depends(workspace_ctx("reviewer"))) -> dict[str, Any]:
    spec = scoped(ctx, Specification, spec_id)
    spec_service.review_specification(ctx.db, spec, decision=body.decision, user_id=ctx.user.id, note=body.note)
    ctx.db.commit()
    return ser(spec)


# --- datasets -----------------------------------------------------------------------------


class DatasetGenerate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_id: str | None = None
    specification_id: str | None = None
    n_cases: int = Field(default=20, ge=4, le=500)
    holdout_fraction: float = Field(default=0.3, ge=0.0, le=0.6)
    seed: int = 0
    categories: list[Literal["knowledge", "citation", "edge_case"]] = ["knowledge", "citation", "edge_case"]
    include_multi_turn: bool = True
    unanswerable_share: float = Field(default=0.2, ge=0.0, le=0.5)
    languages: list[str] = Field(default_factory=list)
    document_ids: list[str] | None = None
    limits: dict[str, float] = Field(default_factory=dict)


class CaseUpdate(BaseModel):
    expected: dict[str, Any] | None = None
    answer_status: Literal["approved", "needs_human_review", "unverified"] | None = None
    severity: Literal["low", "normal", "high", "critical"] | None = None


class RotateBody(BaseModel):
    n_cases: int | None = Field(default=None, ge=4, le=500)


def _dataset_dict(ctx: Ctx, dataset: Dataset) -> dict[str, Any]:
    return ser(dataset, extra={"counts": dataset_service.split_counts(ctx.db, dataset.id)})


def _case_dict(case: TestCase) -> dict[str, Any]:
    return ser(case)


@router.post("/datasets:generate", status_code=202)
def generate_dataset(body: DatasetGenerate, request: Request, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Generate evidence-linked cases and split them into regression and sealed holdout, before any optimization."""
    key = idempotency_key(request)
    if key:
        existing = ctx.db.scalar(select(jobs.Job).where(jobs.Job.workspace_id == ctx.ws, jobs.Job.idempotency_key == key))
        if existing is not None:
            return {"job": job_dict(existing), "dataset": _dataset_dict(ctx, scoped(ctx, Dataset, existing.payload["dataset_id"]))}
    if body.agent_id:
        scoped(ctx, Agent, body.agent_id)
    if body.specification_id:
        scoped(ctx, Specification, body.specification_id)
    dataset = Dataset(workspace_id=ctx.ws, name=body.name.strip(), agent_id=body.agent_id,
                      specification_id=body.specification_id, seed=body.seed, holdout_fraction=body.holdout_fraction)
    ctx.db.add(dataset)
    ctx.db.flush()
    params = body.model_dump(exclude={"name", "agent_id", "specification_id", "seed", "holdout_fraction"})
    job = jobs.enqueue(ctx.db, workspace_id=ctx.ws, kind="dataset.generate",
                       payload={"dataset_id": dataset.id, "params": params}, created_by=ctx.user.id, idempotency_key=key)
    ctx.db.expire_all()
    return {"job": job_dict(job), "dataset": _dataset_dict(ctx, scoped(ctx, Dataset, dataset.id))}


@router.post("/datasets:import", status_code=201)
async def import_dataset(file: UploadFile = File(...), name: str | None = Form(None), dataset_id: str | None = Form(None),
                         approved: bool = Form(True), holdout_fraction: float = Form(0.0),
                         ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Import business-owned Q&A or real conversations (.csv, .jsonl, .json)."""
    data = await _read(file)
    try:
        rows = dataset_service.parse_import(file.filename or "", data)
    except (dataset_service.DatasetError, ValueError) as exc:
        raise fail(422, "INVALID_IMPORT", str(exc)) from exc
    if dataset_id:
        dataset = scoped(ctx, Dataset, dataset_id)
    else:
        dataset = Dataset(workspace_id=ctx.ws, name=(name or file.filename or "Imported cases").strip(), status="ready")
        ctx.db.add(dataset)
        ctx.db.flush()
    try:
        result = dataset_service.import_cases(ctx.db, dataset, rows, approved=approved, user_id=ctx.user.id,
                                              holdout_fraction=max(0.0, min(0.6, holdout_fraction)),
                                              source_name=file.filename or "upload")
    except dataset_service.DatasetError as exc:
        ctx.db.rollback()
        raise fail(422, "INVALID_IMPORT", str(exc)) from exc
    ctx.db.commit()
    return {"dataset": _dataset_dict(ctx, dataset), **result}


@router.get("/datasets")
def list_datasets(limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    query = select(Dataset).where(Dataset.workspace_id == ctx.ws).order_by(Dataset.created_at.desc())
    return page(ctx.db, query, limit=limit, offset=offset, transform=lambda d: _dataset_dict(ctx, d))


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return _dataset_dict(ctx, scoped(ctx, Dataset, dataset_id))


@router.get("/datasets/{dataset_id}/coverage")
def dataset_coverage(dataset_id: str, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return dataset_service.coverage(ctx.db, scoped(ctx, Dataset, dataset_id))


@router.get("/datasets/{dataset_id}/cases")
def list_cases(dataset_id: str, split: Literal["regression", "holdout"] = "regression", acknowledge: bool = False,
               limit: int = 50, offset: int = 0, ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    dataset = scoped(ctx, Dataset, dataset_id)
    if split == "holdout":
        # Holdout items are sealed. A reviewer may open them deliberately; doing so is audited and
        # marks the holdout as exposed, so it must be rotated before it can gate further tuning.
        ctx.require("reviewer")
        if not acknowledge:
            raise fail(403, "HOLDOUT_SEALED", "holdout items are sealed. Pass acknowledge=true to open them; "
                       "this is audited and the holdout will need rotating before it is used again.")
        dataset_service.holdout_cases(ctx.db, dataset.id, HoldoutAccess("human_review", ctx.user.id))
        dataset.holdout_exposed = True
        audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="holdout.viewed",
                     subject_type="dataset", subject_id=dataset.id, detail={"generation": dataset.holdout_generation})
        ctx.db.commit()
    query = (select(TestCase).where(TestCase.dataset_id == dataset.id, TestCase.split == split)
             .order_by(TestCase.created_at, TestCase.id))
    return page(ctx.db, query, limit=limit, offset=offset, transform=_case_dict)


@router.patch("/datasets/{dataset_id}/cases/{case_id}")
def update_case(dataset_id: str, case_id: str, body: CaseUpdate, ctx: Ctx = Depends(workspace_ctx("reviewer"))) -> dict[str, Any]:
    """Human review of a reference: approve it, correct it, or flag it. Approved references outrank drafts."""
    dataset = scoped(ctx, Dataset, dataset_id)
    case = scoped(ctx, TestCase, case_id)
    if case.dataset_id != dataset.id:
        raise fail(404, "NOT_FOUND", "case not found in this dataset")
    if case.split != "regression":
        raise fail(403, "HOLDOUT_SEALED", "only regression cases can be edited")
    if body.expected is not None:
        case.expected = {**(case.expected or {}), **body.expected}
    if body.answer_status:
        case.answer_status = body.answer_status
        case.provenance = {**(case.provenance or {}), "reviewed_by": ctx.user.id}
    if body.severity:
        case.severity = body.severity
        case.weight = 2.0 if body.severity == "critical" else 1.0
    dataset.version += 1
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="case.reviewed", subject_type="test_case",
                 subject_id=case.id, detail={"answer_status": case.answer_status, "severity": case.severity})
    ctx.db.commit()
    return _case_dict(case)


@router.post("/datasets/{dataset_id}/holdout:rotate", status_code=202)
def rotate(dataset_id: str, body: RotateBody, request: Request, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    dataset = scoped(ctx, Dataset, dataset_id)
    job = jobs.enqueue(ctx.db, workspace_id=ctx.ws, kind="dataset.rotate_holdout",
                       payload={"dataset_id": dataset.id, "params": body.model_dump(exclude_none=True)},
                       created_by=ctx.user.id, idempotency_key=idempotency_key(request))
    return job_dict(job)


@router.post("/datasets/{dataset_id}/stale:retire")
def retire_stale(dataset_id: str, ctx: Ctx = Depends(workspace_ctx("editor"))) -> dict[str, Any]:
    """Retire regression cases whose evidence came from a document version that has been replaced."""
    dataset = scoped(ctx, Dataset, dataset_id)
    retired = 0
    for case in ctx.db.scalars(select(TestCase).where(TestCase.dataset_id == dataset.id, TestCase.stale.is_(True),
                                                      TestCase.split == "regression")):
        case.split = "retired"
        retired += 1
    if retired:
        dataset.version += 1
        audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="dataset.stale_retired",
                     subject_type="dataset", subject_id=dataset.id, detail={"retired": retired})
    ctx.db.commit()
    return {"retired": retired, "dataset": _dataset_dict(ctx, dataset)}
