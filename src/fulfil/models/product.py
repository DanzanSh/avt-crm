import datetime as dt

from sqlalchemy import BigInteger, ForeignKey, Index, JSON, String, DateTime, func, text
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
    # Идентификаторы WB — BIGINT: chrtID уже перерос INTEGER (2222347681 > 2^31-1,
    # боевой кабинет падал 500 на синхронизации), nmID/imtID растут туда же.
    wb_nm_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    wb_imt_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # Идентификатор размера карточки WB — от него зависит upsert (см. services/products.py
    # _upsert_card): один nmId/imtId может дать несколько товаров, по одному на chrtId.
    wb_chrt_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    vendor_code: Mapped[str | None] = mapped_column(String(128))
    barcode: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str | None] = mapped_column(String(256))
    size: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(64))
    image_url: Mapped[str | None] = mapped_column(String(1024))
    # Кэш текущего остатка на складе WB FBS (Этап 2 плана №3, п.2.3) — обновляется
    # при каждой успешной передаче (services.stock.transfer_to_fbs) и, позже,
    # фоновым опросом (Этап 3). Источник правды для «доступно к передаче»
    # вместо ломкого Σ уже отправленных транзакций.
    wb_fbs_amount: Mapped[int] = mapped_column(default=0, server_default="0")
    # Доп. баркоды того же размера карточки WB (skus[1:] — P2-11): у части
    # размеров WB отдаёт больше одного skus для одного и того же chrtId, и заказ
    # может прийти с любым из них. Не хранится в поле barcode (оно одно и
    # уникально в пределах клиента) — только для поиска товара по позиции заказа
    # (см. services.orders._find_product).
    extra_barcodes: Mapped[list[str]] = mapped_column(JSON, default=list)
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

    @property
    def client_archived(self) -> bool:
        # Товар клиента в архиве виден только с «показать архивные» (problems.txt, п.2)
        # и помечается отдельным бейджем — его архивом управляет клиент, не товар.
        return self.client is not None and self.client.archived_at is not None
