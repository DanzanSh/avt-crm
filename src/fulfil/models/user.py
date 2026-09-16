import datetime as dt
import enum

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base

# Логин уникален только среди живых — тот же приём, что uq_client_name_live:
# логин архивированного сотрудника можно выдать заново, его аудит остаётся.
_LIVE = text("archived_at IS NULL")


class UserRole(str, enum.Enum):
    HOST = "host"  # владелец, один на систему; только он восстанавливает и удаляет клиентов
    ADMIN = "admin"  # заводит сотрудников
    EMPLOYEE = "employee"


class User(Base):
    """Учётная запись (problems.txt, п.5; authorization-model.md, этап 1).

    Не удаляется, а архивируется: логин остаётся строкой в audit_log.actor,
    stock_moves.actor и receipt_lines.actor. is_active — быстрая блокировка без
    архивации: заблокированный получает 401 и по уже выданному токену."""

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_user_login_live", "login", unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[UserRole] = mapped_column(default=UserRole.EMPLOYEE)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
