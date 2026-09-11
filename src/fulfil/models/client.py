import datetime as dt

from sqlalchemy import Index, String, Text, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base

# Частичный уникальный индекс "пока живой" — тот же приём, что uq_product_barcode_live
# в models/product.py: поддержан и PostgreSQL, и SQLite (для юнит-тестов).
_LIVE = text("archived_at IS NULL")


class Client(Base):
    """Клиент фулфилмента — один кабинет WB (Этап 1 плана доработок №3, п.2.2).

    wb_api_key_enc хранится ЗАШИФРОВАННЫМ (Fernet, см. fulfil.secrets) — наружу
    API отдаёт только hasApiKey/маску/wbTokenExpiresAt (schemas/client.py).
    last_sync_at/last_sync_error — общая диагностика последней синхронизации
    (карточки, заказы, поставки — что бы ни бегало по расписанию в фоне)."""

    __tablename__ = "clients"
    __table_args__ = (
        Index("uq_client_name_live", "name", unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    wb_api_key_enc: Mapped[str | None] = mapped_column(Text)
    wb_warehouse_id: Mapped[str | None] = mapped_column(String(64))
    wb_warehouse_name: Mapped[str | None] = mapped_column(String(256))
    wb_token_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
