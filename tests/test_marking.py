import pytest

from fulfil.errors import AppError, DuplicateMarkError
from fulfil.models.fbs import Order, OrderItem, OrderStatus
from fulfil.models.product import Product
from fulfil.services.marking import confirm_assembly, scan_mark


def _make_order_item(db, seller) -> tuple[OrderItem, OrderItem]:
    product = Product(client_id=seller.id, barcode="2000000000017", name="Майка белая")
    db.add(product)
    db.flush()

    order1 = Order(client_id=seller.id, wb_order_id="WB-1", status=OrderStatus.CONFIRMED)
    order2 = Order(client_id=seller.id, wb_order_id="WB-2", status=OrderStatus.CONFIRMED)
    db.add_all([order1, order2])
    db.flush()

    item1 = OrderItem(order_id=order1.id, product_id=product.id, barcode=product.barcode, qty=1)
    item2 = OrderItem(order_id=order2.id, product_id=product.id, barcode=product.barcode, qty=1)
    db.add_all([item1, item2])
    db.commit()
    db.refresh(item1)
    db.refresh(item2)
    return item1, item2


def test_mark_code_rejects_reuse_across_orders(db, seller):
    item1, item2 = _make_order_item(db, seller)
    raw_code = "\x1d01046012345678909721abc123\x1d91EE00\x1d92AbCdEf"

    scan_mark(db, item1, raw_code)

    with pytest.raises(DuplicateMarkError):
        scan_mark(db, item2, raw_code)


def test_mark_code_stored_verbatim_with_gs_separator(db, seller):
    """Код хранится ровно как выдал сканер — с разделителем GS (\\x1d) и криптохвостом,
    без нормализации (см. DEV-PLAN.md)."""
    item, _ = _make_order_item(db, seller)
    raw_code = "\x1d01046012345678909721abc123\x1d91EE00\x1d92AbCdEf"

    mark = scan_mark(db, item, raw_code)

    assert mark.mark_code == raw_code


# --- P1-8: статус заказа и лимит количества марок на позицию ------------------


def test_scan_mark_rejects_wrong_order_status(db, seller):
    item, _ = _make_order_item(db, seller)
    item.order.status = OrderStatus.PACKED
    db.commit()

    with pytest.raises(AppError) as exc_info:
        scan_mark(db, item, "\x1d01046012345678909721code\x1d91EE00")
    assert exc_info.value.reason_code == "wrong_status"


def test_scan_mark_rejects_over_item_qty(db, seller):
    """qty=1 у позиции — вторая марка на ту же позицию отклоняется лимитом,
    а не молча копится сверх заявленного количества товара."""
    item, _ = _make_order_item(db, seller)
    scan_mark(db, item, "\x1d01046012345678909721aaa\x1d91EE00")

    with pytest.raises(AppError) as exc_info:
        scan_mark(db, item, "\x1d01046012345678909721bbb\x1d91EE00")
    assert exc_info.value.reason_code == "marks_limit_reached"


def _make_order_with_stock(db, seller, qty=1):
    from fulfil.services.receiving import place_stock
    from fulfil.services.storage import generate_cells

    product = Product(client_id=seller.id, barcode="2000000000031", name="Майка синяя")
    db.add(product)
    db.flush()
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, qty)

    order = Order(client_id=seller.id, wb_order_id="WB-ASSEMBLY", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=qty)
    db.add(item)
    db.commit()
    db.refresh(order)
    db.refresh(item)
    return order, item


def test_confirm_assembly_rejects_partial_marks(db, seller):
    """Заявлено 2 шт, отсканирована 1 марка — подтверждать нечем, ошибка до
    списания остатка и до обращения к WB (P1-8)."""
    from fulfil.integrations.wb.mock import WBMockClient
    from fulfil.models.stock import StockByCell
    from sqlalchemy import select

    order, item = _make_order_with_stock(db, seller, qty=2)
    scan_mark(db, item, "\x1d01046012345678909721only\x1d91EE00")

    with pytest.raises(AppError) as exc_info:
        confirm_assembly(db, order, WBMockClient(client_id=seller.id))
    assert exc_info.value.reason_code == "marks_count_mismatch"

    row = db.scalar(select(StockByCell).where(StockByCell.product_id == item.product_id))
    assert row.qty == 2  # остаток не тронут — сборка не подтверждена


def test_confirm_assembly_without_marks_commits_stock_and_packs(db, seller):
    from fulfil.integrations.wb.mock import WBMockClient

    order, item = _make_order_with_stock(db, seller, qty=3)
    result = confirm_assembly(db, order, WBMockClient(client_id=seller.id))
    assert result.status == OrderStatus.PACKED
