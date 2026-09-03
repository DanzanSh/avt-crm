import datetime as dt

from sqlalchemy import JSON, String, DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base


class IdempotencyKey(Base):
    """X-Idempotency-Key на мутирующих складских операциях (DEV-PLAN.md, конвенции API).

    Ключ уникален на (endpoint, key) — один и тот же ключ на разных ручках не конфликтует.
    Повтор возвращает сохранённый ответ, не выполняя операцию второй раз."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("endpoint", "key", name="uq_idempotency_endpoint_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    status_code: Mapped[int]
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
