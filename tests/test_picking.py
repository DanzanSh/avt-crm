from fulfil.models.fbs import Order, OrderItem, OrderStatus
from fulfil.models.product import Product
from fulfil.services.picking import build_pick_list, commit_pick_lines
from fulfil.services.receiving import place_stock
from fulfil.services.storage import generate_cells


def _make_product(db, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_order(db, product: Product, qty: int) -> Order:
    order = Order(wb_order_id="WB-1", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=qty))
    db.commit()
    db.refresh(order)
    return order


def test_pick_list_drains_first_cell_before_next_not_50_50(db):
    """Инвариант из DEV-PLAN.md: остаток 75 в A-1-10 и 175 в B-5-5, заказ на 100 —
    строки должны быть 75 + 25, а НЕ 50/50."""
    product = _make_product(db)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_a110 = next(c for c in cells_a if c.address == "A-1-10")
    cells_b = generate_cells(db, "B", racks=5, cells_per_rack=5)
    cell_b55 = next(c for c in cells_b if c.address == "B-5-5")

    place_stock(db, product, cell_a110, 75)
    place_stock(db, product, cell_b55, 175)

    order = _make_order(db, product, qty=100)
    lines = build_pick_list(db, order)

    by_cell = {line.cell_id: line.qty for line in lines}
    assert by_cell[cell_a110.id] == 75
    assert by_cell[cell_b55.id] == 25


def test_pick_list_route_order_is_numeric_not_lexicographic(db):
    """A-1-2 должна идти раньше A-1-10 — строковая сортировка сломала бы это
    ('10' < '2' лексикографически)."""
    product = _make_product(db)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_2 = next(c for c in cells if c.address == "A-1-2")
    cell_10 = next(c for c in cells if c.address == "A-1-10")

    # размещаем в порядке "10 затем 2", чтобы сортировка была не по вставке, а по адресу
    place_stock(db, product, cell_10, 5)
    place_stock(db, product, cell_2, 5)

    order = _make_order(db, product, qty=10)
    lines = build_pick_list(db, order)

    assert [line.cell_id for line in lines] == [cell_2.id, cell_10.id]


def test_commit_pick_lines_decrements_stock(db):
    product = _make_product(db)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 50)

    order = _make_order(db, product, qty=20)
    build_pick_list(db, order)
    commit_pick_lines(db, order)

    from sqlalchemy import select

    from fulfil.models.stock import StockByCell

    row = db.scalar(select(StockByCell).where(StockByCell.cell_id == cell.id))
    assert row.qty == 30
