import datetime as dt
import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fulfil.db import Base


class MoveReason(str, enum.Enum):
    RECEIPT = "receipt"
    PICK = "pick"
    ADJUST = "adjust"
    MOVE_OUT = "move_out"
    MOVE_IN = "move_in"
    WRITE_OFF = "write_off"


class StockByCell(Base):
    """Материализованная проекция stock_moves — источник правды для UI.
    Изменяется только сервисом, который в одной транзакции пишет и сюда, и в stock_moves."""

    __tablename__ = "stock_by_cell"
    __table_args__ = (UniqueConstraint("product_id", "cell_id", name="uq_stock_product_cell"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    cell_id: Mapped[int] = mapped_column(ForeignKey("cells.id"), index=True)
    qty: Mapped[int] = mapped_column(default=0)


class StockMove(Base):
    """Журнал движений — неизменяемый источник правды."""

    __tablename__ = "stock_moves"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    cell_id: Mapped[int] = mapped_column(ForeignKey("cells.id"), index=True)
    qty_delta: Mapped[int]
    reason: Mapped[MoveReason]
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[int | None]
    # "Кто изменил" (Scope IN п.3) — строка, не FK: в PoC нет таблицы users.
    actor: Mapped[str] = mapped_column(String(64), server_default="system")
    comment: Mapped[str | None] = mapped_column(String(512))
    # Связывает обе половины перемещения между ячейками в одну логическую операцию.
    move_group_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def new_move_group_id() -> str:
    return str(uuid.uuid4())


class FbsTransferStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class FbsTransfer(Base):
    """Передача остатка в кабинет WB (FBS)."""

    __tablename__ = "fbs_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[int]
    wb_warehouse_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[FbsTransferStatus] = mapped_column(default=FbsTransferStatus.PENDING)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    wb_response: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
