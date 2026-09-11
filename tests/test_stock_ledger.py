import pytest

from fulfil.models.product import Product
from fulfil.models.storage import CellStatus
from fulfil.models.stock import MoveReason
from fulfil.services.picking import build_pick_list, commit_pick_lines
from fulfil.services.receiving import place_stock
from fulfil.services.storage import block_cell, generate_cells, unblock_cell
from fulfil.services.stock_ledger import apply_move


def _make_product(db, seller, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(client_id=seller.id, barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_order(db, product: Product, qty: int):
    from fulfil.models.fbs import Order, OrderItem, OrderStatus

    order = Order(client_id=product.client_id, wb_order_id="WB-1", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=qty))
    db.commit()
    db.refresh(order)
    return order


def test_apply_move_rejects_negative_result(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 5)

    with pytest.raises(ValueError):
        apply_move(db, product=product, cell=cell, qty_delta=-10, reason=MoveReason.ADJUST, actor="tester")


def test_cell_returns_to_free_after_full_pick(db, seller):
    """Дефект №1: после подбора ячейка должна вернуться в free, а не остаться occupied
    навсегда (FEATURES-PLAN.md, этап 0.3)."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 20)
    assert cell.status == CellStatus.OCCUPIED

    order = _make_order(db, product, qty=20)
    build_pick_list(db, order)
    commit_pick_lines(db, order, actor="tester")
    db.commit()  # commit_pick_lines намеренно не коммитит — транзакция общая с send-marks

    db.refresh(cell)
    assert cell.status == CellStatus.FREE


def test_unblock_cell_with_stock_becomes_occupied_not_free(db, seller):
    """Дефект №2: unblock_cell на непустой ячейке не должен ставить free — иначе она
    попадает в список свободных для приёмки (FEATURES-PLAN.md, этап 0.3)."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 10)
    block_cell(db, cell, "плановая проверка")
    assert cell.status == CellStatus.BLOCKED

    unblock_cell(db, cell)

    assert cell.status == CellStatus.OCCUPIED


def test_unblock_cell_without_stock_becomes_free(db, seller):
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    block_cell(db, cell, "плановая проверка")
    unblock_cell(db, cell)
    assert cell.status == CellStatus.FREE


def test_apply_move_writes_actor_and_comment(db, seller):
    from sqlalchemy import select

    from fulfil.models.stock import StockMove

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    apply_move(
        db, product=product, cell=cell, qty_delta=10, reason=MoveReason.ADJUST,
        actor="warehouse_user", comment="пересчёт",
    )
    db.commit()

    move = db.scalar(select(StockMove).where(StockMove.product_id == product.id))
    assert move.actor == "warehouse_user"
    assert move.comment == "пересчёт"
