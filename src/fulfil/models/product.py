import datetime as dt

from sqlalchemy import ForeignKey, Index, JSON, String, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fulfil.db import Base

_LIVE = text("archived_at IS NULL")


class Product(Base):
    """Товар = размер карточки WB (Этап 1, п.1.1): у одной карточки (wb_imt_id) несколько
    размеров (wb_chrt_id), каждый — отдельная строка. wb_nm_id больше не уникален —
    уникальность баркода теперь в пределах клиента (два клиента могут продавать
    один и тот же товар с одинаковым GTIN)."""

    __tablename__ = "products"
    __table_args__ = (
        Index(
            "uq_product_barcode_live", "client_id", "barcode",
            unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    wb_nm_id: Mapped[int | None] = mapped_column(index=True)
    wb_imt_id: Mapped[int | None] = mapped_column(index=True)
    # Идентификатор размера карточки WB — от него зависит upsert (см. services/products.py
    # _upsert_card): один nmId/imtId может дать несколько товаров, по одному на chrtId.
    wb_chrt_id: Mapped[int | None] = mapped_column(index=True)
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

    client: Mapped["Client"] = relationship()

    @property
    def client_name(self) -> str | None:
        # Для ProductOut.model_validate(..., from_attributes=True) — читает по имени
        # поля. Списки должны eager-load'ить .client (selectinload), иначе N+1.
        return self.client.name if self.client is not None else None
