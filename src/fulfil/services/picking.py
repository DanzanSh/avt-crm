"""Лист подбора: последовательное списание по маршруту (Scope IN п.8-9).

Ячейка исчерпывается полностью, потом следующая — не по дате приёмки и не
параллельно из двух. Сортировка на бэке (zone_code, rack_no, cell_no) —
единственный источник порядка; фронт берёт seq готовым и не сортирует сам
(см. DEV-PLAN.md: в эталоне фронт и бэк по этому месту расходились).
"""

import datetime as dt

from sqlalchemy import delete, func, select
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
        .order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no)
    )


def _reserved_qty_by_cell(db: Session, product_id: int, exclude_order_id: int) -> dict[int, int]:
    """Кол-во товара по ячейкам, уже распределённое в листы подбора ДРУГИХ заказов,
    но ещё физически не списанное (picked_at IS NULL) — эти единицы лежат на полке,
    но уже "обещаны" другому заказу (P0-1). Без вычета этого резерва два заказа на
    один и тот же остаток получали пересекающиеся аллокации: второй проходил
    build_pick_list(), а на commit_pick_lines падал в stock_changed."""
    rows = db.execute(
        select(PickLine.cell_id, func.sum(PickLine.qty))
        .where(
            PickLine.product_id == product_id,
            PickLine.order_id != exclude_order_id,
            PickLine.picked_at.is_(None),
        )
        .group_by(PickLine.cell_id)
    ).all()
    return dict(rows)


def build_pick_list(db: Session, order: Order) -> list[PickLine]:
    """Считает распределение по ячейкам для каждой позиции заказа.
    Не списывает остаток — списание происходит при подтверждении сборки (день 11)."""

    existing = db.scalars(select(PickLine).where(PickLine.order_id == order.id)).all()
    if existing:
        return existing

    allocations: list[tuple[int, int, int, tuple]] = []  # (product_id, cell_id, qty, route_key)

    for item in order.items:
        remaining = item.qty
        reserved_by_cell = _reserved_qty_by_cell(db, item.product_id, order.id)
        rows = db.execute(_route_order_query(db, item.product_id)).all()
        for stock_row, cell in rows:
            if remaining <= 0:
                break
            free_in_cell = stock_row.qty - reserved_by_cell.get(cell.id, 0)
            take = min(remaining, free_in_cell)
            if take <= 0:
                continue
            allocations.append(
                (item.product_id, cell.id, take, (cell.zone_code, cell.rack_no, cell.shelf_no, cell.cell_no))
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


def rebuild_pick_list(db: Session, order: Order) -> list[PickLine]:
    """Удаляет незавершённый (не списанный) лист подбора заказа и строит новый по
    актуальному остатку (P0-1). Нужен, когда commit_pick_lines() падает с
    stock_changed — иначе build_pick_list() из-за своего "if existing: return
    existing" вечно возвращал бы тот же протухший лист, и заказ навсегда
    застревал бы на сборке, хотя товар на складе физически есть."""
    db.execute(
        delete(PickLine).where(PickLine.order_id == order.id, PickLine.picked_at.is_(None))
    )
    db.flush()
    return build_pick_list(db, order)


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
        line.picked_at = dt.datetime.now(dt.timezone.utc)
