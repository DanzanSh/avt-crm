import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.models.audit import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(get_current_user)])


@router.get("")
def list_audit(
    entity_type: str | None = None,
    entity_id: int | None = None,
    actor: str | None = None,
    from_date: dt.datetime | None = None,
    to_date: dt.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Общий журнал изменений зон/стеллажей/ячеек/товаров — одна ручка на все сущности
    (FEATURES-PLAN.md, этап 0.5)."""
    stmt = select(AuditLog)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor is not None:
        stmt = stmt.where(AuditLog.actor == actor)
    if from_date is not None:
        stmt = stmt.where(AuditLog.created_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(AuditLog.created_at <= to_date)
    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)

    return [
        {
            "id": row.id,
            "entityType": row.entity_type,
            "entityId": row.entity_id,
            "action": row.action,
            "changes": row.changes,
            "actor": row.actor,
            "comment": row.comment,
            "createdAt": row.created_at,
        }
        for row in db.scalars(stmt)
    ]
