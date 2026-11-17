"""Append-only audit trail."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def record(db: Session, *, workspace_id: str, action: str, user_id: str | None = None,
           subject_type: str = "", subject_id: str = "", detail: dict[str, Any] | None = None) -> None:
    """Add an audit event to the current transaction. ``detail`` must never contain secret values."""
    db.add(AuditEvent(workspace_id=workspace_id, user_id=user_id, action=action,
                      subject_type=subject_type, subject_id=subject_id, detail=detail or {}))
