import datetime as dt

from sqlalchemy import Index, JSON, String, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column

from fulfil.db import Base

_LIVE = text("archived_at IS NULL")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("uq_product_barcode_live", "barcode", unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wb_nm_id: Mapped[int | None] = mapped_column(unique=True, index=True)
    wb_imt_id: Mapped[int | None] = mapped_column(index=True)
    vendor_code: Mapped[str | None] = mapped_column(String(128))
    barcode: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str | None] = mapped_column(String(256))
    size: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(64))
    image_url: Mapped[str | None] = mapped_column(String(1024))
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Поля, изменённые вручную через PATCH — синхронизация с WB их не перезаписывает
    # (Scope IN п.4; без этого правка молча исчезает при следующем "Синхронизировать с WB").
    manual_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
