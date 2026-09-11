import pytest

from fulfil.errors import DuplicateMarkError
from fulfil.models.fbs import Order, OrderItem, OrderStatus
from fulfil.models.product import Product
from fulfil.services.marking import scan_mark


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
