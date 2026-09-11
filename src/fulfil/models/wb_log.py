import datetime as dt

from sqlalchemy import ForeignKey, String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base


class WbApiLog(Base):
    __tablename__ = "wb_api_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # nullable — лог пишется и для запросов без клиента (например, /ping диагностики).
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(256))
    status: Mapped[int | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
