"""Единственная точка изменения остатка (FEATURES-PLAN.md, этап 0.2).

Приёмка (services.receiving), подбор (services.picking), корректировка и перемещение
(services.stock) — все проводят количество через apply_move(). Это держит инвариант
DEV-PLAN.md №1 (stock_by_cell — проекция stock_moves) кодом, а не дисциплиной вызывающих.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfil.models.product import Product
from fulfil.models.storage import Cell, CellStatus
from fulfil.models.stock import MoveReason, StockByCell, StockMove


def recalc_cell_status(db: Session, cell: Cell) -> None:
    """Статус — производная от остатка, не ручной флаг (кроме blocked).

    blocked не трогаем: это ручное состояние, снимается только unblock_cell().
    Иначе: occupied, если суммарный остаток в ячейке > 0, иначе free.
    Чинит FEATURES-PLAN.md, дефект №1 (после подбора ячейка навсегда occupied)."""
    if cell.status == CellStatus.BLOCKED:
        return
    # Session создаётся с autoflush=False (fulfil.db.SessionLocal) — без явного flush()
    # эта SUM не увидит ещё не отправленное в БД изменение stock_by_cell.qty.
    db.flush()
    total = db.scalar(
        select(func.coalesce(func.sum(StockByCell.qty), 0)).where(StockByCell.cell_id == cell.id)
    )
    cell.status = CellStatus.OCCUPIED if total > 0 else CellStatus.FREE


def apply_move(
    db: Session,
    *,
    product: Product,
    cell: Cell,
    qty_delta: int,
    reason: MoveReason,
    actor: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
    comment: str | None = None,
    move_group_id: str | None = None,
) -> StockByCell:
    """Блокирует строку ячейки (FOR UPDATE), меняет stock_by_cell, пишет stock_moves,
    пересчитывает статус ячейки — в одной транзакции. Вызывающий коммитит сам.

    qty_delta может быть отрицательным (списание/подбор/выход при перемещении).
    Уводить остаток по ячейке ниже нуля нельзя — это баг вызывающего кода, не сценарий API."""
    if qty_delta == 0:
        raise ValueError("qty_delta не может быть нулевым — это не движение")

    locked_cell = db.execute(
        select(Cell).where(Cell.id == cell.id).with_for_update()
    ).scalar_one()

    row = db.scalar(
        select(StockByCell).where(
            StockByCell.product_id == product.id, StockByCell.cell_id == locked_cell.id
        )
    )
    if row is None:
        row = StockByCell(product_id=product.id, cell_id=locked_cell.id, qty=0)
        db.add(row)
        db.flush()

    new_qty = row.qty + qty_delta
    if new_qty < 0:
        raise ValueError(
            f"Остаток по товару #{product.id} в ячейке {locked_cell.address} "
            f"не может стать отрицательным ({row.qty} + {qty_delta})"
        )
    row.qty = new_qty

    db.add(
        StockMove(
            product_id=product.id,
            cell_id=locked_cell.id,
            qty_delta=qty_delta,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
            actor=actor,
            comment=comment,
            move_group_id=move_group_id,
        )
    )

    recalc_cell_status(db, locked_cell)
    return row
