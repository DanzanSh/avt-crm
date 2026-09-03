import datetime as dt
import enum

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, DateTime, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fulfil.db import Base

# Частичный уникальный индекс "пока живой" — поддержан и PostgreSQL, и SQLite
# (последний нужен для юнит-тестов на in-memory БД, tests/conftest.py).
# Без него soft-delete не даёт создать новую сущность с тем же кодом/адресом/баркодом:
# упёрлись бы в архивную строку.
_LIVE = text("deleted_at IS NULL")


class CellStatus(str, enum.Enum):
    FREE = "free"
    OCCUPIED = "occupied"
    BLOCKED = "blocked"


class Zone(Base):
    __tablename__ = "zones"
    __table_args__ = (
        Index("uq_zone_code_live", "code", unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(2), index=True)  # 'A', 'B', '1'... — только латиница
    name: Mapped[str | None] = mapped_column(String(128))
    # Порядок отображения на карте склада: новая зона получает position = max+1,
    # так что она всегда рисуется последней (Scope IN п.1).
    position: Mapped[int] = mapped_column(default=0, index=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    racks: Mapped[list["Rack"]] = relationship(back_populates="zone", cascade="all, delete-orphan")


class Rack(Base):
    __tablename__ = "racks"
    __table_args__ = (
        Index(
            "uq_rack_zone_number_live", "zone_id", "number",
            unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    zone_id: Mapped[int] = mapped_column(ForeignKey("zones.id", ondelete="CASCADE"))
    number: Mapped[int]
    cells_count: Mapped[int] = mapped_column(default=0)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    zone: Mapped[Zone] = relationship(back_populates="racks")
    cells: Mapped[list["Cell"]] = relationship(back_populates="rack", cascade="all, delete-orphan")


class Cell(Base):
    __tablename__ = "cells"
    __table_args__ = (
        Index("uq_cell_address_live", "address", unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE),
        Index("uq_cell_barcode_live", "barcode", unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rack_id: Mapped[int] = mapped_column(ForeignKey("racks.id", ondelete="CASCADE"))

    # Разобранный адрес — для последовательной сортировки маршрута подбора.
    # Числовые сегменты хранятся как INT: строковая сортировка "10" < "2" сломает маршрут.
    zone_code: Mapped[str] = mapped_column(String(2), index=True)
    rack_no: Mapped[int] = mapped_column(index=True)
    cell_no: Mapped[int] = mapped_column(index=True)

    address: Mapped[str] = mapped_column(String(32), index=True)  # 'A-1-10' — денормализовано для UI/сканера
    barcode: Mapped[str] = mapped_column(String(32), index=True)  # 'CELL-000123'

    status: Mapped[CellStatus] = mapped_column(default=CellStatus.FREE, index=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(256))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    rack: Mapped[Rack] = relationship(back_populates="cells")
    allowed_barcodes: Mapped[list["CellAllowedBarcode"]] = relationship(
        back_populates="cell", cascade="all, delete-orphan"
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CellAllowedBarcode(Base):
    """Допуск: если для ячейки задан хотя бы один разрешённый баркод,
    приёмка чужого баркода в неё блокируется (Scope IN п.4 бизнес-плана)."""

    __tablename__ = "cell_allowed_barcodes"
    __table_args__ = (UniqueConstraint("cell_id", "barcode", name="uq_cell_allowed_barcode"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cell_id: Mapped[int] = mapped_column(ForeignKey("cells.id", ondelete="CASCADE"))
    barcode: Mapped[str] = mapped_column(String(64), index=True)

    cell: Mapped[Cell] = relationship(back_populates="allowed_barcodes")
