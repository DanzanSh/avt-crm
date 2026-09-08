"""Экран «Склад»: агрегация по SKU, передача остатка в ФБС (Scope IN п.7),
изменение/списание/перемещение остатка с журналом (Scope IN п.3, FEATURES-PLAN.md этап 2).
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfil.config import get_settings
from fulfil.errors import AppError, CellOccupiedError, StockChangedError
from fulfil.integrations.wb.base import WBClient
from fulfil.models.product import Product
from fulfil.models.stock import FbsTransfer, FbsTransferStatus, MoveReason, StockByCell, StockMove, new_move_group_id
from fulfil.models.storage import Cell
from fulfil.services.stock_ledger import apply_move


def get_stock_summary(db: Session, product: Product) -> dict:
    total = db.scalar(
        select(func.coalesce(func.sum(StockByCell.qty), 0)).where(
            StockByCell.product_id == product.id
        )
    )
    transferred = db.scalar(
        select(func.coalesce(func.sum(FbsTransfer.qty), 0))
        .where(FbsTransfer.product_id == product.id)
        .where(FbsTransfer.status == FbsTransferStatus.SENT)
    )
    return {
        "productId": product.id,
        "total": total,
        "transferredFbs": transferred,
        "availableToTransfer": max(total - transferred, 0),
        # Уменьшение остатка ниже уже переданного в ФБС количества не уведомляет WB
        # автоматически (см. FEATURES-PLAN.md, этап 2.5) — это видно тут явно.
        "fbsOversold": max(transferred - total, 0),
    }


def get_stock_by_cell(db: Session, product: Product) -> list[dict]:
    rows = db.execute(
        select(StockByCell, Cell)
        .join(Cell, Cell.id == StockByCell.cell_id)
        .where(StockByCell.product_id == product.id, StockByCell.qty > 0)
        .order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no)
    ).all()
    return [
        {"cellId": c.id, "cellAddress": c.address, "cellBarcode": c.barcode, "qty": s.qty}
        for s, c in rows
    ]


def transfer_to_fbs(
    db: Session, product: Product, qty: int, idempotency_key: str, wb_client: WBClient
) -> FbsTransfer:
    existing = db.scalar(select(FbsTransfer).where(FbsTransfer.idempotency_key == idempotency_key))
    if existing is not None:
        return existing  # повтор того же запроса — не передаём второй раз

    summary = get_stock_summary(db, product)
    if qty > summary["availableToTransfer"]:
        raise AppError(
            f"Нельзя передать {qty} шт — доступно только {summary['availableToTransfer']}.",
            status_code=409,
            reason_code="not_enough_stock",
        )

    settings = get_settings()
    transfer = FbsTransfer(
        product_id=product.id,
        qty=qty,
        wb_warehouse_id=settings.wb_warehouse_id or None,
        status=FbsTransferStatus.PENDING,
        idempotency_key=idempotency_key,
    )
    db.add(transfer)
    db.commit()
    db.refresh(transfer)

    try:
        resp = wb_client.update_fbs_stock(settings.wb_warehouse_id, product.barcode, qty)
        transfer.status = FbsTransferStatus.SENT if resp.get("ok") else FbsTransferStatus.FAILED
        transfer.wb_response = str(resp)
    except Exception as exc:  # лимиты/сеть WB — не считаем поставку неудачной молча
        transfer.status = FbsTransferStatus.FAILED
        transfer.wb_response = str(exc)

    db.commit()
    db.refresh(transfer)
    return transfer


def _lock_cell(db: Session, cell: Cell) -> Cell:
    return db.execute(select(Cell).where(Cell.id == cell.id).with_for_update()).scalar_one()


def _current_qty(db: Session, product: Product, cell: Cell) -> int:
    row = db.scalar(
        select(StockByCell).where(StockByCell.product_id == product.id, StockByCell.cell_id == cell.id)
    )
    return row.qty if row else 0


def adjust_stock(
    db: Session, product: Product, cell: Cell, new_qty: int, expected_qty: int, actor: str,
    comment: str | None = None,
) -> StockByCell:
    """CAS-корректировка: expected_qty — то, что оператор видел на экране. Если фактический
    остаток уже другой (кто-то принял/собрал заказ параллельно), 409 stock_changed вместо
    молчаливой перезаписи (DEV-PLAN.md, приём stock.html эталона)."""
    if new_qty < 0:
        raise AppError("Остаток не может быть отрицательным.", status_code=400, reason_code="invalid_qty")

    locked_cell = _lock_cell(db, cell)
    current = _current_qty(db, product, locked_cell)
    if current != expected_qty:
        raise StockChangedError(
            f"Остаток изменился: вы видели {expected_qty}, сейчас в ячейке {locked_cell.address} — {current}.",
            actual_qty=current,
        )

    delta = new_qty - current
    if delta == 0:
        row = db.scalar(
            select(StockByCell).where(StockByCell.product_id == product.id, StockByCell.cell_id == locked_cell.id)
        )
        return row

    if delta < 0 and not comment:
        raise AppError(
            "Уменьшение остатка требует комментария — он попадёт в журнал.",
            status_code=400,
            reason_code="comment_required",
        )

    row = apply_move(
        db, product=product, cell=locked_cell, qty_delta=delta, reason=MoveReason.ADJUST,
        actor=actor, comment=comment,
    )
    db.commit()
    db.refresh(row)
    return row


def write_off_stock(
    db: Session, product: Product, cell: Cell, expected_qty: int, actor: str, comment: str,
) -> None:
    """«Удаление» количества — списание в ноль, а не удаление строки: stock_by_cell
    остаётся проекцией stock_moves (DEV-PLAN.md, инвариант №1)."""
    if not comment:
        raise AppError(
            "Списание требует комментария — он попадёт в журнал.",
            status_code=400,
            reason_code="comment_required",
        )

    locked_cell = _lock_cell(db, cell)
    current = _current_qty(db, product, locked_cell)
    if current != expected_qty:
        raise StockChangedError(
            f"Остаток изменился: вы видели {expected_qty}, сейчас в ячейке {locked_cell.address} — {current}.",
            actual_qty=current,
        )
    if current == 0:
        raise AppError("Списывать нечего — остаток уже 0.", status_code=400, reason_code="nothing_to_write_off")

    apply_move(
        db, product=product, cell=locked_cell, qty_delta=-current, reason=MoveReason.WRITE_OFF,
        actor=actor, comment=comment,
    )
    db.commit()


def move_stock(
    db: Session, product: Product, from_cell: Cell, to_cell: Cell, qty: int, expected_qty: int,
    actor: str, comment: str | None = None,
) -> dict:
    """Перемещение между ячейками. Обе ячейки блокируются в порядке возрастания id —
    иначе два встречных перемещения дают дедлок (FEATURES-PLAN.md, этап 2.3)."""
    from fulfil.services.storage import check_placement_allowed, find_free_cell_suggestions

    if from_cell.id == to_cell.id:
        raise AppError("Нельзя переместить в ту же ячейку.", status_code=400, reason_code="same_cell")

    first_id, second_id = sorted((from_cell.id, to_cell.id))
    locked_by_id = {
        c.id: c
        for c in db.scalars(
            select(Cell).where(Cell.id.in_([first_id, second_id])).order_by(Cell.id).with_for_update()
        )
    }
    locked_from = locked_by_id[from_cell.id]
    locked_to = locked_by_id[to_cell.id]

    current = _current_qty(db, product, locked_from)
    if current != expected_qty:
        raise StockChangedError(
            f"Остаток изменился: вы видели {expected_qty}, сейчас в ячейке {locked_from.address} — {current}.",
            actual_qty=current,
        )
    if qty > current:
        raise AppError(
            f"Нельзя переместить {qty} шт — в ячейке {locked_from.address} только {current}.",
            status_code=409,
            reason_code="not_enough_stock",
        )

    check_placement_allowed(db, locked_to, product.barcode)
    other_sku = db.scalars(
        select(StockByCell).where(
            StockByCell.cell_id == locked_to.id,
            StockByCell.product_id != product.id,
            StockByCell.qty > 0,
        )
    ).all()
    if other_sku:
        suggestions = find_free_cell_suggestions(db, locked_to.zone_code)
        raise CellOccupiedError(
            f"Ячейка {locked_to.address} занята другим товаром.", suggestions=suggestions
        )

    group_id = new_move_group_id()
    apply_move(
        db, product=product, cell=locked_from, qty_delta=-qty, reason=MoveReason.MOVE_OUT,
        actor=actor, comment=comment, move_group_id=group_id,
    )
    apply_move(
        db, product=product, cell=locked_to, qty_delta=qty, reason=MoveReason.MOVE_IN,
        actor=actor, comment=comment, move_group_id=group_id,
    )
    db.commit()
    return {"fromCellAddress": locked_from.address, "toCellAddress": locked_to.address, "qty": qty}


def get_moves(
    db: Session,
    *,
    product_id: int | None = None,
    cell_id: int | None = None,
    actor: str | None = None,
    reason: str | None = None,
    from_date: dt.datetime | None = None,
    to_date: dt.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Журнал движений (Scope IN п.3: кто/что/когда изменил). qtyAfter считается оконной
    функцией по всей истории (product, cell) — не хранится, иначе разъехалось бы при
    любой правке истории (FEATURES-PLAN.md, этап 2.4)."""
    base = select(
        StockMove.id.label("move_id"),
        StockMove.product_id.label("product_id"),
        StockMove.cell_id.label("cell_id"),
        StockMove.qty_delta.label("qty_delta"),
        StockMove.reason.label("reason"),
        StockMove.actor.label("actor"),
        StockMove.comment.label("comment"),
        StockMove.move_group_id.label("move_group_id"),
        StockMove.ref_type.label("ref_type"),
        StockMove.ref_id.label("ref_id"),
        StockMove.created_at.label("created_at"),
        func.sum(StockMove.qty_delta)
        .over(partition_by=(StockMove.product_id, StockMove.cell_id), order_by=(StockMove.created_at, StockMove.id))
        .label("qty_after"),
    ).subquery()

    stmt = (
        select(base, Product.name.label("product_name"), Cell.address.label("cell_address"))
        .join(Product, Product.id == base.c.product_id)
        .join(Cell, Cell.id == base.c.cell_id)
    )
    if product_id is not None:
        stmt = stmt.where(base.c.product_id == product_id)
    if cell_id is not None:
        stmt = stmt.where(base.c.cell_id == cell_id)
    if actor is not None:
        stmt = stmt.where(base.c.actor == actor)
    if reason is not None:
        stmt = stmt.where(base.c.reason == reason)
    if from_date is not None:
        stmt = stmt.where(base.c.created_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(base.c.created_at <= to_date)

    stmt = stmt.order_by(base.c.created_at.desc(), base.c.move_id.desc()).limit(limit).offset(offset)

    return [
        {
            "id": row.move_id,
            "createdAt": row.created_at,
            "actor": row.actor,
            "reason": row.reason.value if hasattr(row.reason, "value") else row.reason,
            "qtyDelta": row.qty_delta,
            "qtyAfter": row.qty_after,
            "comment": row.comment,
            "moveGroupId": row.move_group_id,
            "refType": row.ref_type,
            "refId": row.ref_id,
            "productId": row.product_id,
            "productName": row.product_name,
            "cellId": row.cell_id,
            "cellAddress": row.cell_address,
        }
        for row in db.execute(stmt)
    ]
