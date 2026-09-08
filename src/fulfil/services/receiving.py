"""Приёмка товара: три проверки + атомарное обновление остатка (Scope IN п.4-5).

place_stock() делегирует запись остатка services.stock_ledger.apply_move() — единственному
пути изменения stock_by_cell на весь проект (FEATURES-PLAN.md, этап 0.2). Здесь остаются
только проверки, специфичные для приёмки: допуск баркода, занятость другим SKU, блокировка.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.errors import CellOccupiedError
from fulfil.models.product import Product
from fulfil.models.receiving import Receipt, ReceiptLine, ReceiptStatus
from fulfil.models.storage import Cell
from fulfil.models.stock import MoveReason, StockByCell
from fulfil.services.storage import check_placement_allowed, find_free_cell_suggestions
from fulfil.services.stock_ledger import apply_move


def place_stock(db: Session, product: Product, cell: Cell, qty: int, actor: str = "system") -> StockByCell:
    if qty <= 0:
        raise ValueError("qty должен быть положительным")

    # Блокируем строку ячейки на время проверки+записи — гонка при параллельном
    # размещении в одну и ту же ячейку исключена.
    locked_cell = db.execute(
        select(Cell).where(Cell.id == cell.id).with_for_update()
    ).scalar_one()

    check_placement_allowed(db, locked_cell, product.barcode)

    other_sku_rows = db.scalars(
        select(StockByCell).where(
            StockByCell.cell_id == locked_cell.id,
            StockByCell.product_id != product.id,
            StockByCell.qty > 0,
        )
    ).all()
    if other_sku_rows:
        suggestions = find_free_cell_suggestions(db, locked_cell.zone_code)
        raise CellOccupiedError(
            f"Ячейка {locked_cell.address} занята другим товаром.", suggestions=suggestions
        )

    row = apply_move(
        db, product=product, cell=locked_cell, qty_delta=qty, reason=MoveReason.RECEIPT, actor=actor,
    )

    db.commit()
    db.refresh(row)
    return row


def get_or_create_open_receipt(db: Session) -> Receipt:
    receipt = db.scalar(
        select(Receipt).where(Receipt.status == ReceiptStatus.IN_PROGRESS).order_by(Receipt.id.desc())
    )
    if receipt is None:
        last_id = db.scalar(select(Receipt.id).order_by(Receipt.id.desc())) or 0
        number = f"RCPT-{last_id + 1:06d}"
        receipt = Receipt(number=number, status=ReceiptStatus.IN_PROGRESS)
        db.add(receipt)
        db.commit()
        db.refresh(receipt)
    return receipt


def add_receipt_line(
    db: Session, receipt: Receipt, product: Product, cell: Cell, qty: int, actor: str = "system"
) -> ReceiptLine:
    place_stock(db, product, cell, qty, actor=actor)
    line = ReceiptLine(
        receipt_id=receipt.id, product_id=product.id, cell_id=cell.id, qty=qty, actor=actor
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def list_receipt_lines(db: Session, *, limit: int = 50, offset: int = 0) -> list[dict]:
    """История приёмок, newest-first. Join по id без фильтра deleted_at/archived_at —
    строка истории должна пережить архивацию товара или удаление места."""
    rows = db.execute(
        select(
            ReceiptLine.id,
            Receipt.number,
            Product.name,
            Product.barcode,
            Cell.address,
            ReceiptLine.qty,
            ReceiptLine.actor,
            ReceiptLine.created_at,
        )
        .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
        .join(Product, Product.id == ReceiptLine.product_id)
        .join(Cell, Cell.id == ReceiptLine.cell_id)
        .order_by(ReceiptLine.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {
            "id": r.id,
            "receiptNumber": r.number,
            "productName": r.name,
            "barcode": r.barcode,
            "cellAddress": r.address,
            "qty": r.qty,
            "actor": r.actor,
            "createdAt": r.created_at,
        }
        for r in rows
    ]


def finish_receipt(db: Session, receipt: Receipt) -> Receipt:
    import datetime as dt

    receipt.status = ReceiptStatus.DONE
    receipt.finished_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(receipt)
    return receipt
