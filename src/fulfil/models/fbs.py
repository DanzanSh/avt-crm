import datetime as dt
import enum

from sqlalchemy import ForeignKey, String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fulfil.db import Base


class OrderStatus(str, enum.Enum):
    NEW = "new"
    CONFIRMED = "confirmed"
    IN_ASSEMBLY = "in_assembly"
    PACKED = "packed"
    IN_SUPPLY = "in_supply"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED_NO_STOCK = "rejected_no_stock"


class SupplyStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    PARTIAL = "partial"
    FAILED = "failed"
    STALE = "stale"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    wb_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    wb_supply_id: Mapped[str | None] = mapped_column(String(64), index=True)
    supply_id: Mapped[int | None] = mapped_column(ForeignKey("supplies.id"), index=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[OrderStatus] = mapped_column(default=OrderStatus.NEW, index=True)
    created_at_wb: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    barcode: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int]
    picked_qty: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")

    # Этикетка заказа от WB — приходит инлайном в ответе на упаковку, кэшируется тут.
    sticker_data: Mapped[str | None] = mapped_column(Text)
    sticker_type: Mapped[str | None] = mapped_column(String(16))

    order: Mapped[Order] = relationship(back_populates="items")
    marks: Mapped[list["OrderItemMark"]] = relationship(
        back_populates="order_item", cascade="all, delete-orphan"
    )


class OrderItemMark(Base):
    """Код маркировки «Честный знак», привязанный к позиции заказа.

    mark_code хранится РОВНО как его выдал сканер — включая разделитель GS (\\x1d)
    и криптохвост. Никакой нормализации на бэке: WB штрафует и за потерянный
    разделитель, и за самодеятельность с содержимым кода.
    """

    __tablename__ = "order_item_marks"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"))
    mark_code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    scanned_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_to_wb_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    order_item: Mapped[OrderItem] = relationship(back_populates="marks")


class PickLine(Base):
    """Строка листа подбора — результат последовательного списания по маршруту."""

    __tablename__ = "pick_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    cell_id: Mapped[int] = mapped_column(ForeignKey("cells.id"))
    qty: Mapped[int]
    seq: Mapped[int]  # порядок обхода, вычислен на сервере — фронт не сортирует сам
    picked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Supply(Base):
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True)
    wb_supply_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[SupplyStatus] = mapped_column(default=SupplyStatus.OPEN, index=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    qr_data: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    boxes: Mapped[list["SupplyBox"]] = relationship(
        back_populates="supply", cascade="all, delete-orphan"
    )


class SupplyBox(Base):
    __tablename__ = "supply_boxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id", ondelete="CASCADE"))
    wb_trbx_id: Mapped[str | None] = mapped_column(String(64))
    sticker_data: Mapped[str | None] = mapped_column(Text)

    supply: Mapped[Supply] = relationship(back_populates="boxes")
