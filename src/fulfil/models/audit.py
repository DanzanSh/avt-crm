import datetime as dt

from sqlalchemy import JSON, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base


class AuditLog(Base):
    """Общий журнал изменений зон/стеллажей/ячеек/товаров.

    Отдельно от stock_moves — там своя природа записи (движение количества),
    здесь — факт правки сущности с дифом полей (Scope IN п.1, п.4)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)  # 'zone'|'rack'|'cell'|'product'
    entity_id: Mapped[int] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(16))  # 'create'|'update'|'delete'|'restore'
    changes: Mapped[dict] = mapped_column(JSON, default=dict)  # {"поле": {"from": ..., "to": ...}}
    actor: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


def record(
    db, *, entity_type: str, entity_id: int, action: str, actor: str,
    changes: dict | None = None, comment: str | None = None,
) -> AuditLog:
    row = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        changes=changes or {},
        actor=actor,
        comment=comment,
    )
    db.add(row)
    return row
