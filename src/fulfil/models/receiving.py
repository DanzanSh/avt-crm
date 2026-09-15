import datetime as dt
import enum

from sqlalchemy import Date, ForeignKey, String, Text, UniqueConstraint, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fulfil.db import Base


class ReceiptStatus(str, enum.Enum):
    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Receipt(Base):
    """Приёмка по плану (Этап 5 плана №3, п.3.1): заявленное количество живёт в
    ReceiptPlanLine, принятое — это SUM(ReceiptLine.qty) по паре (приёмка, товар),
    отдельно нигде не хранится (см. services.receiving.receipt_progress)."""

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    status: Mapped[ReceiptStatus] = mapped_column(default=ReceiptStatus.DRAFT, index=True)
    expected_date: Mapped[dt.date | None] = mapped_column(Date)
    comment: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["ReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )
    plan_lines: Mapped[list["ReceiptPlanLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="ReceiptPlanLine.id"
    )
    client: Mapped["Client"] = relationship()

    @property
    def client_name(self) -> str | None:
        return self.client.name if self.client is not None else None


class ReceiptPlanLine(Base):
    """Заявленное количество одного товара в приёмке. Редактируется только пока
    Receipt.status == DRAFT (см. services.receiving.set_plan_lines)."""

    __tablename__ = "receipt_plan_lines"
    __table_args__ = (
        UniqueConstraint("receipt_id", "product_id", name="uq_receipt_plan_line_product"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    expected_qty: Mapped[int]

    receipt: Mapped[Receipt] = relationship(back_populates="plan_lines")
    product: Mapped["Product"] = relationship()


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    cell_id: Mapped[int] = mapped_column(ForeignKey("cells.id"))
    qty: Mapped[int]
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    receipt: Mapped[Receipt] = relationship(back_populates="lines")
