"""Authentication, workspaces and membership."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__, audit
from ..config import get_settings
from ..connectors import ADAPTER_INFO
from ..gateway import PROVIDER_KINDS, ROLES, effective_offline
from ..models import Membership, User, Workspace
from ..security import create_access_token, hash_password, verify_password
from .deps import Ctx, current_user, fail, get_db, ser, workspace_ctx

router = APIRouter()

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class Signup(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(default="", max_length=200)


class Login(BaseModel):
    email: str
    password: str


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["offline", "connected"] = "offline"


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    mode: Literal["offline", "connected"] | None = None
    settings: dict[str, Any] | None = None


class MemberUpsert(BaseModel):
    email: str
    role: Literal["owner", "editor", "reviewer", "viewer"]


def _workspace_dict(workspace: Workspace, role: str | None = None) -> dict[str, Any]:
    return ser(workspace, extra={"role": role, "effective_offline": effective_offline(workspace),
                                 "instance_offline": get_settings().offline})


@router.get("/system")
def system(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    return {
        "name": "Kaizeval", "version": __version__, "edition": "self-hosted",
        "instance_offline": settings.offline,
        "signup_open": settings.allow_signup or (db.scalar(select(func.count()).select_from(User)) or 0) == 0,
        "model_roles": ROLES, "provider_kinds": PROVIDER_KINDS, "adapters": ADAPTER_INFO,
        "roles": ["owner", "editor", "reviewer", "viewer"],
        "telemetry": "none",
    }


@router.post("/auth/signup", status_code=201)
def signup(body: Signup, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    first_user = (db.scalar(select(func.count()).select_from(User)) or 0) == 0
    if not settings.allow_signup and not first_user:
        raise fail(403, "SIGNUP_DISABLED", "self-service sign-up is disabled on this instance")
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise fail(409, "EMAIL_TAKEN", "an account with this email already exists")
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    return {"token": create_access_token(user.id), "user": ser(user)}


@router.post("/auth/login")
def login(body: Login, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise fail(401, "INVALID_CREDENTIALS", "email or password is incorrect")
    return {"token": create_access_token(user.id), "user": ser(user)}


@router.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(Workspace, Membership.role).join(Membership, Membership.workspace_id == Workspace.id)
                      .where(Membership.user_id == user.id).order_by(Workspace.created_at)).all()
    return {"user": ser(user), "workspaces": [_workspace_dict(w, role) for w, role in rows]}


@router.post("/workspaces", status_code=201)
def create_workspace(body: WorkspaceCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.mode == "connected" and get_settings().offline:
        raise fail(409, "INSTANCE_OFFLINE", "this instance runs in strict offline mode (KAIZEVAL_OFFLINE=true); "
                   "connected workspaces are not allowed")
    workspace = Workspace(name=body.name.strip(), mode=body.mode, settings={"allow_autonomous_optimization": False})
    db.add(workspace)
    db.flush()
    db.add(Membership(user_id=user.id, workspace_id=workspace.id, role="owner"))
    audit.record(db, workspace_id=workspace.id, user_id=user.id, action="workspace.created",
                 subject_type="workspace", subject_id=workspace.id, detail={"mode": body.mode})
    db.commit()
    return _workspace_dict(workspace, "owner")


@router.get("/workspaces/{workspace_id}")
def get_workspace(ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    return _workspace_dict(ctx.workspace, ctx.role)


@router.patch("/workspaces/{workspace_id}")
def update_workspace(body: WorkspaceUpdate, ctx: Ctx = Depends(workspace_ctx("owner"))) -> dict[str, Any]:
    if body.mode == "connected" and get_settings().offline:
        raise fail(409, "INSTANCE_OFFLINE", "this instance runs in strict offline mode; it cannot be overridden per workspace")
    changes: dict[str, Any] = {}
    if body.name:
        ctx.workspace.name = changes["name"] = body.name.strip()
    if body.mode and body.mode != ctx.workspace.mode:
        ctx.workspace.mode = changes["mode"] = body.mode
    if body.settings is not None:
        allowed = {k: v for k, v in body.settings.items() if k in ("allow_autonomous_optimization", "retention_days")}
        ctx.workspace.settings = {**(ctx.workspace.settings or {}), **allowed}
        changes["settings"] = allowed
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="workspace.updated",
                 subject_type="workspace", subject_id=ctx.ws, detail=changes)
    ctx.db.commit()
    return _workspace_dict(ctx.workspace, ctx.role)


@router.get("/workspaces/{workspace_id}/members")
def list_members(ctx: Ctx = Depends(workspace_ctx())) -> dict[str, Any]:
    rows = ctx.db.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id)
                          .where(Membership.workspace_id == ctx.ws).order_by(User.email)).all()
    return {"items": [{"user_id": u.id, "email": u.email, "name": u.name, "role": role} for u, role in rows]}


@router.put("/workspaces/{workspace_id}/members")
def upsert_member(body: MemberUpsert, ctx: Ctx = Depends(workspace_ctx("owner"))) -> dict[str, Any]:
    user = ctx.db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None:
        raise fail(404, "USER_NOT_FOUND", "no account with this email; the person must sign up first")
    membership = ctx.db.scalar(select(Membership).where(Membership.workspace_id == ctx.ws, Membership.user_id == user.id))
    if membership is None:
        membership = Membership(user_id=user.id, workspace_id=ctx.ws, role=body.role)
        ctx.db.add(membership)
    else:
        owners = ctx.db.scalar(select(func.count()).select_from(Membership).where(
            Membership.workspace_id == ctx.ws, Membership.role == "owner")) or 0
        if membership.role == "owner" and body.role != "owner" and owners <= 1:
            raise fail(409, "LAST_OWNER", "a workspace must keep at least one owner")
        membership.role = body.role
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="member.role_set",
                 subject_type="user", subject_id=user.id, detail={"role": body.role})
    ctx.db.commit()
    return {"user_id": user.id, "email": user.email, "role": membership.role}


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(user_id: str, ctx: Ctx = Depends(workspace_ctx("owner"))) -> None:
    membership = ctx.db.scalar(select(Membership).where(Membership.workspace_id == ctx.ws, Membership.user_id == user_id))
    if membership is None:
        raise fail(404, "NOT_FOUND", "member not found")
    owners = ctx.db.scalar(select(func.count()).select_from(Membership).where(
        Membership.workspace_id == ctx.ws, Membership.role == "owner")) or 0
    if membership.role == "owner" and owners <= 1:
        raise fail(409, "LAST_OWNER", "a workspace must keep at least one owner")
    ctx.db.delete(membership)
    audit.record(ctx.db, workspace_id=ctx.ws, user_id=ctx.user.id, action="member.removed",
                 subject_type="user", subject_id=user_id)
    ctx.db.commit()
