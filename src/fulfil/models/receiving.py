import datetime as dt
import enum

from sqlalchemy import ForeignKey, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fulfil.db import Base


class ReceiptStatus(str, enum.Enum):
    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    status: Mapped[ReceiptStatus] = mapped_column(default=ReceiptStatus.DRAFT, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["ReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


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
