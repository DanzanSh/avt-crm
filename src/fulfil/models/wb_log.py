import datetime as dt

from sqlalchemy import String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base


class WbApiLog(Base):
    __tablename__ = "wb_api_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(256))
    status: Mapped[int | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
