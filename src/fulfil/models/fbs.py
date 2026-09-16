import datetime as dt
import enum

from sqlalchemy import BigInteger, ForeignKey, String, Text, DateTime, func
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
    # Этап 4 плана №3, п.6 problems.txt: статус выводится из полей WB (wb_done,
    # scan_dt), а не назначается вручную по одному месту в коде — done=false
    # остаётся OPEN, done=true без scanDt — IN_DELIVERY, scanDt заполнен — ACCEPTED.
    IN_DELIVERY = "in_delivery"
    ACCEPTED = "accepted"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    wb_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    wb_supply_id: Mapped[str | None] = mapped_column(String(64), index=True)
    supply_id: Mapped[int | None] = mapped_column(ForeignKey("supplies.id"), index=True)
    # Склад WB, на который пришёл заказ (Этап 3 плана №3, п.3.1/4.1 problems.txt) —
    # синхронизация берёт ТОЛЬКО заказы с warehouseId == client.wb_warehouse_id,
    # чужие склады продавца в базу вообще не попадают.
    wb_warehouse_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # BIGINT — те же идентификаторы WB, что в Product (см. комментарий там).
    wb_nm_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    wb_chrt_id: Mapped[int | None] = mapped_column(BigInteger)
    article: Mapped[str | None] = mapped_column(String(128))
    # Статусы самого WB (см. POST /api/v3/orders/status) — опрашиваются отдельно от
    # /orders/new, иначе отмена покупателем/доставка/приёмка до нас не доходят.
    wb_status: Mapped[str | None] = mapped_column(String(32))
    supplier_status: Mapped[str | None] = mapped_column(String(32))
    # 'unknown_sku' — позиция не нашла товар даже после пересинхронизации карточек
    # клиента; заказ сохраняется (а не теряется молча), но взять его в работу нельзя.
    problem: Mapped[str | None] = mapped_column(String(32), index=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[OrderStatus] = mapped_column(default=OrderStatus.NEW, index=True)
    created_at_wb: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    client: Mapped["Client"] = relationship()

    @property
    def client_name(self) -> str | None:
        return self.client.name if self.client is not None else None


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    # Nullable (Этап 3 плана №3, п.3.1): позиция с нераспознанным баркодом всё равно
    # сохраняется — заказ помечается Order.problem='unknown_sku' вместо того, чтобы
    # тихо потерять строку (раньше _find_product()==None просто пропускал item).
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    barcode: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int]
    picked_qty: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")

    # Этикетка заказа от WB — приходит инлайном в ответе на упаковку, кэшируется тут.
    sticker_data: Mapped[str | None] = mapped_column(Text)
    sticker_type: Mapped[str | None] = mapped_column(String(16))

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped["Product | None"] = relationship()
    marks: Mapped[list["OrderItemMark"]] = relationship(
        back_populates="order_item", cascade="all, delete-orphan"
    )

    @property
    def product_name(self) -> str | None:
        return self.product.name if self.product is not None else None

    @property
    def product_size(self) -> str | None:
        return self.product.size if self.product is not None else None

    @property
    def product_image_url(self) -> str | None:
        return self.product.image_url if self.product is not None else None


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
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    wb_supply_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    # Имя, отправленное в WB при создании ("<клиент> <дата>" — Этап 3, п.3.2) или
    # прочитанное из чужой поставки при синхронизации (Этап 4, п.4.2). Раньше не
    # персистилось вовсе.
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[SupplyStatus] = mapped_column(default=SupplyStatus.OPEN, index=True)
    # done/scanDt — сырые поля WB, из которых services.supplies выводит status
    # (Этап 4, п.4.1). closed_at — момент, когда МЫ вызвали close_supply (наш
    # локальный акт передачи в доставку); closed_at_wb — то, что при синхронизации
    # отдаёт сам WB (может быть выставлено и без нашего участия).
    wb_done: Mapped[bool] = mapped_column(default=False)
    created_at_wb: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at_wb: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    scan_dt: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    qr_data: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    boxes: Mapped[list["SupplyBox"]] = relationship(
        back_populates="supply", cascade="all, delete-orphan"
    )
    client: Mapped["Client"] = relationship()
    # Заказы поставки — односторонняя связь для отдачи в API ("список заказов
    # раскрывается по клику", Этап 4, п.4.3). viewonly: принадлежность заказа
    # поставке по-прежнему меняется через прямое присвоение order.supply_id
    # (services.supplies.add_order_to_supply), а не через эту коллекцию.
    orders: Mapped[list["Order"]] = relationship(
        foreign_keys="Order.supply_id", viewonly=True, order_by="Order.id"
    )

    @property
    def client_name(self) -> str | None:
        return self.client.name if self.client is not None else None


class SupplyBox(Base):
    __tablename__ = "supply_boxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id", ondelete="CASCADE"))
    wb_trbx_id: Mapped[str | None] = mapped_column(String(64))
    sticker_data: Mapped[str | None] = mapped_column(Text)

    supply: Mapped[Supply] = relationship(back_populates="boxes")
