"""Лист подбора: последовательное списание по маршруту (Scope IN п.8-9).

Ячейка исчерпывается полностью, потом следующая — не по дате приёмки и не
параллельно из двух. Сортировка на бэке (zone_code, rack_no, cell_no) —
единственный источник порядка; фронт берёт seq готовым и не сортирует сам
(см. DEV-PLAN.md: в эталоне фронт и бэк по этому месту расходились).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.errors import AppError
from fulfil.models.fbs import Order, PickLine
from fulfil.models.product import Product
from fulfil.models.stock import MoveReason, StockByCell
from fulfil.models.storage import Cell
from fulfil.services.stock_ledger import apply_move


def _route_order_query(db: Session, product_id: int):
    return (
        select(StockByCell, Cell)
        .join(Cell, Cell.id == StockByCell.cell_id)
        .where(StockByCell.product_id == product_id, StockByCell.qty > 0)
        .order_by(Cell.zone_code, Cell.rack_no, Cell.cell_no)
    )


def build_pick_list(db: Session, order: Order) -> list[PickLine]:
    """Считает распределение по ячейкам для каждой позиции заказа.
    Не списывает остаток — списание происходит при подтверждении сборки (день 11)."""

    existing = db.scalars(select(PickLine).where(PickLine.order_id == order.id)).all()
    if existing:
        return existing

    allocations: list[tuple[int, int, int, tuple]] = []  # (product_id, cell_id, qty, route_key)

    for item in order.items:
        remaining = item.qty
        rows = db.execute(_route_order_query(db, item.product_id)).all()
        for stock_row, cell in rows:
            if remaining <= 0:
                break
            take = min(remaining, stock_row.qty)
            if take <= 0:
                continue
            allocations.append(
                (item.product_id, cell.id, take, (cell.zone_code, cell.rack_no, cell.cell_no))
            )
            remaining -= take
        if remaining > 0:
            raise AppError(
                f"Недостаточно остатка для позиции заказа (товар #{item.product_id}): "
                f"не хватает {remaining} шт.",
                status_code=409,
                reason_code="not_enough_stock",
            )

    allocations.sort(key=lambda a: a[3])

    lines: list[PickLine] = []
    for seq, (product_id, cell_id, qty, _route_key) in enumerate(allocations, start=1):
        line = PickLine(order_id=order.id, product_id=product_id, cell_id=cell_id, qty=qty, seq=seq)
        db.add(line)
        lines.append(line)

    db.commit()
    for line in lines:
        db.refresh(line)
    return lines


def commit_pick_lines(db: Session, order: Order, actor: str = "system") -> None:
    """Фактическое списание остатка при подтверждении сборки — та же транзакция,
    что и передача марок в WB (services.marking.confirm_assembly). Списание идёт через
    apply_move(), поэтому статус опустошённой ячейки корректно возвращается в free
    (FEATURES-PLAN.md, дефект №1 — раньше ячейка навсегда оставалась occupied)."""
    lines = db.scalars(select(PickLine).where(PickLine.order_id == order.id)).all()
    for line in lines:
        row = db.scalar(
            select(StockByCell).where(
                StockByCell.product_id == line.product_id, StockByCell.cell_id == line.cell_id
            )
        )
        if row is None or row.qty < line.qty:
            raise AppError(
                "Остаток изменился с момента формирования листа подбора — соберите заново.",
                status_code=409,
                reason_code="stock_changed",
            )
        product = db.get(Product, line.product_id)
        cell = db.get(Cell, line.cell_id)
        apply_move(
            db, product=product, cell=cell, qty_delta=-line.qty, reason=MoveReason.PICK,
            actor=actor, ref_type="order", ref_id=order.id,
        )

        import datetime as dt

        line.picked_at = dt.datetime.now(dt.timezone.utc)
