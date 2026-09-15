import pytest

from fulfil.errors import AppError
from fulfil.models.fbs import Order, OrderItem, OrderStatus
from fulfil.models.product import Product
from fulfil.services.picking import build_pick_list, commit_pick_lines, rebuild_pick_list
from fulfil.services.receiving import place_stock
from fulfil.services.storage import generate_cells


def _make_product(db, seller, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(client_id=seller.id, barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_order(db, product: Product, qty: int, wb_order_id: str = "WB-1") -> Order:
    order = Order(client_id=product.client_id, wb_order_id=wb_order_id, status=OrderStatus.CONFIRMED)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=qty))
    db.commit()
    db.refresh(order)
    return order


def test_pick_list_drains_first_cell_before_next_not_50_50(db, seller):
    """Инвариант из DEV-PLAN.md: остаток 75 в A-1-10 и 175 в B-5-5, заказ на 100 —
    строки должны быть 75 + 25, а НЕ 50/50."""
    product = _make_product(db, seller)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_a110 = next(c for c in cells_a if c.address == "A-1-1-10")
    cells_b = generate_cells(db, "B", racks=5, cells_per_rack=5)
    cell_b55 = next(c for c in cells_b if c.address == "B-5-1-5")

    place_stock(db, product, cell_a110, 75)
    place_stock(db, product, cell_b55, 175)

    order = _make_order(db, product, qty=100)
    lines = build_pick_list(db, order)

    by_cell = {line.cell_id: line.qty for line in lines}
    assert by_cell[cell_a110.id] == 75
    assert by_cell[cell_b55.id] == 25


def test_pick_list_route_order_is_numeric_not_lexicographic(db, seller):
    """A-1-2 должна идти раньше A-1-10 — строковая сортировка сломала бы это
    ('10' < '2' лексикографически)."""
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_2 = next(c for c in cells if c.address == "A-1-1-2")
    cell_10 = next(c for c in cells if c.address == "A-1-1-10")

    # размещаем в порядке "10 затем 2", чтобы сортировка была не по вставке, а по адресу
    place_stock(db, product, cell_10, 5)
    place_stock(db, product, cell_2, 5)

    order = _make_order(db, product, qty=10)
    lines = build_pick_list(db, order)

    assert [line.cell_id for line in lines] == [cell_2.id, cell_10.id]


def test_commit_pick_lines_decrements_stock(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 50)

    order = _make_order(db, product, qty=20)
    build_pick_list(db, order)
    commit_pick_lines(db, order)

    from sqlalchemy import select

    from fulfil.models.stock import StockByCell

    row = db.scalar(select(StockByCell).where(StockByCell.cell_id == cell.id))
    assert row.qty == 30


# --- P0-1: два заказа на один и тот же остаток не должны пересекаться --------


def test_build_pick_list_excludes_stock_reserved_by_other_orders(db, seller):
    """Заказ A уже забронировал 70 из 100 (лист построен, но ещё не списан) —
    заказ B на 50 должен увидеть только оставшиеся 30 и получить not_enough_stock,
    а не повторно распределить те же 70 единиц поверх заказа A."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 100)

    order_a = _make_order(db, product, qty=70, wb_order_id="WB-A")
    build_pick_list(db, order_a)

    order_b = _make_order(db, product, qty=50, wb_order_id="WB-B")
    with pytest.raises(AppError) as exc_info:
        build_pick_list(db, order_b)
    assert exc_info.value.reason_code == "not_enough_stock"


def test_build_pick_list_reservation_frees_up_after_other_order_committed_elsewhere(db, seller):
    """Заказ A бронирует 70 из 100 в одной ячейке; появляется вторая ячейка с
    остатком — заказ B на 50 должен получить доступные 30 в первой ячейке (не
    занятые A) + недостающее из второй, а не упасть."""
    product = _make_product(db, seller)
    [cell_a] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    cells_b = generate_cells(db, "B", racks=1, cells_per_rack=1)
    cell_b = cells_b[0]
    place_stock(db, product, cell_a, 100)
    place_stock(db, product, cell_b, 50)

    order_a = _make_order(db, product, qty=70, wb_order_id="WB-A")
    build_pick_list(db, order_a)

    order_b = _make_order(db, product, qty=50, wb_order_id="WB-B")
    lines_b = build_pick_list(db, order_b)
    by_cell = {line.cell_id: line.qty for line in lines_b}
    assert by_cell.get(cell_a.id, 0) == 30  # только свободный остаток первой ячейки
    assert by_cell.get(cell_b.id, 0) == 20  # остальное — из второй


def test_rebuild_pick_list_recovers_after_stock_changed(db, seller):
    """Заказ B собрал устаревший лист (до того, как заказ A списал остаток) —
    commit_pick_lines() у B падает в stock_changed. rebuild_pick_list() должен
    пересчитать лист по актуальному остатку и позволить собрать заказ снова,
    вместо того чтобы B навсегда застрял с протухшим листом (P0-1)."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 50)

    order_b = _make_order(db, product, qty=30, wb_order_id="WB-B")
    build_pick_list(db, order_b)  # видит все 50, бронирует 30

    # Кто-то списывает остаток мимо резерва B (например, ручная корректировка) —
    # к моменту commit_pick_lines() в ячейке уже меньше, чем нужно B.
    from fulfil.services.stock_ledger import apply_move
    from fulfil.models.stock import MoveReason

    apply_move(db, product=product, cell=cell, qty_delta=-25, reason=MoveReason.WRITE_OFF, actor="tester")
    db.commit()

    with pytest.raises(AppError) as exc_info:
        commit_pick_lines(db, order_b)
    assert exc_info.value.reason_code == "stock_changed"

    # В ячейке осталось 25 — заказу на 30 всё ещё не хватает.
    with pytest.raises(AppError) as exc_info:
        rebuild_pick_list(db, order_b)
    assert exc_info.value.reason_code == "not_enough_stock"
