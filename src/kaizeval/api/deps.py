"""Shared API dependencies: sessions, authentication, workspace-scoped authorization, serialization."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Depends, Header, HTTPException, Path, Request
from sqlalchemy import func, select
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.orm import Session

from ..db import session_factory
from ..models import Job, Membership, User, Workspace, iso
from ..security import decode_access_token

ROLE_RANK = {"viewer": 0, "reviewer": 1, "editor": 2, "owner": 3}
HIDDEN = {"password_hash", "ciphertext"}


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    user_id = decode_access_token(token) if token else None
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(401, detail={"code": "UNAUTHENTICATED", "message": "sign in to continue"})
    return user


@dataclass
class Ctx:
    db: Session
    user: User
    workspace: Workspace
    role: str

    @property
    def ws(self) -> str:
        return self.workspace.id

    def require(self, role: str) -> None:
        if ROLE_RANK[self.role] < ROLE_RANK[role]:
            raise HTTPException(403, detail={"code": "ACCESS_DENIED",
                                             "message": f"this action needs the '{role}' role; you are '{self.role}'"})


def workspace_ctx(min_role: str = "viewer"):
    """Dependency factory. Non-members get 404 so that workspace ids are not confirmed to outsiders."""

    def dependency(workspace_id: str = Path(...), user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> Ctx:
        membership = db.scalar(select(Membership).where(Membership.workspace_id == workspace_id,
                                                        Membership.user_id == user.id))
        workspace = db.get(Workspace, workspace_id) if membership else None
        if membership is None or workspace is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "workspace not found"})
        ctx = Ctx(db=db, user=user, workspace=workspace, role=membership.role)
        ctx.require(min_role)
        return ctx

    return dependency


def scoped(ctx: Ctx, model: type, object_id: str) -> Any:
    """Load a row and guarantee it belongs to the caller's workspace."""
    row = ctx.db.get(model, object_id)
    if row is None or getattr(row, "workspace_id", None) != ctx.ws:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": f"{model.__name__} not found"})
    return row


def ser(row: Any, *, exclude: set[str] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in sa_inspect(type(row)).columns:
        key = column.key
        if key in HIDDEN or (exclude and key in exclude):
            continue
        value = getattr(row, key)
        if key == "secret_id":
            out["has_credential"] = bool(value)
            continue
        out[key] = iso(value) if isinstance(value, datetime) else value
    if extra:
        out.update(extra)
    return out


def page(db: Session, query, *, limit: int, offset: int, transform=ser) -> dict[str, Any]:
    limit = max(1, min(200, limit))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    items = [transform(row) for row in db.scalars(query.limit(limit).offset(max(0, offset)))]
    return {"items": items, "total": total or 0, "limit": limit, "offset": max(0, offset)}


def job_dict(job: Job) -> dict[str, Any]:
    return ser(job, exclude={"payload", "idempotency_key"})


def idempotency_key(request: Request) -> str | None:
    key = request.headers.get("Idempotency-Key")
    return key[:120] if key else None


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})
