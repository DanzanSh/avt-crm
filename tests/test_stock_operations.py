import pytest

from fulfil.errors import CellBlockedError, CellOccupiedError, StockChangedError
from fulfil.models.product import Product
from fulfil.services.receiving import place_stock
from fulfil.services.storage import block_cell, generate_cells
from fulfil.services.stock import adjust_stock, get_moves, move_stock, write_off_stock


def _make_product(db, seller, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(client_id=seller.id, barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_adjust_stock_happy_path(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 100)

    row = adjust_stock(db, product, cell, new_qty=90, expected_qty=100, actor="tester", comment="пересчёт")
    assert row.qty == 90


def test_adjust_stock_rejects_stale_expected_qty(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 100)

    with pytest.raises(StockChangedError) as exc_info:
        adjust_stock(db, product, cell, new_qty=90, expected_qty=50, actor="tester", comment="x")
    assert exc_info.value.extra["actualQty"] == 100

    # остаток не изменился
    from fulfil.services.stock import get_stock_by_cell

    rows = get_stock_by_cell(db, product)
    assert rows[0]["qty"] == 100


def test_adjust_stock_decrease_requires_comment(db, seller):
    from fulfil.errors import AppError

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 100)

    with pytest.raises(AppError):
        adjust_stock(db, product, cell, new_qty=50, expected_qty=100, actor="tester", comment=None)


def test_write_off_zeroes_and_frees_cell(db, seller):
    from fulfil.models.storage import CellStatus

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 30)

    write_off_stock(db, product, cell, expected_qty=30, actor="tester", comment="брак")

    from fulfil.services.stock import get_stock_by_cell

    assert get_stock_by_cell(db, product) == []  # скрыт в UI (qty=0 отфильтрован)
    db.refresh(cell)
    assert cell.status == CellStatus.FREE


def test_move_stock_preserves_total(db, seller):
    product = _make_product(db, seller)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=1)
    cells_b = generate_cells(db, "B", racks=1, cells_per_rack=1)
    place_stock(db, product, cells_a[0], 50)

    move_stock(db, product, cells_a[0], cells_b[0], qty=20, expected_qty=50, actor="tester")

    from fulfil.services.stock import get_stock_summary

    summary = get_stock_summary(db, product)
    assert summary["total"] == 50


def test_move_stock_into_blocked_cell_rejected(db, seller):
    product = _make_product(db, seller)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=1)
    cells_b = generate_cells(db, "B", racks=1, cells_per_rack=1)
    place_stock(db, product, cells_a[0], 50)
    block_cell(db, cells_b[0], "залив")

    with pytest.raises(CellBlockedError):
        move_stock(db, product, cells_a[0], cells_b[0], qty=10, expected_qty=50, actor="tester")


def test_move_stock_into_cell_with_other_sku_rejected(db, seller):
    product_a = _make_product(db, seller, barcode="1111111111111", name="A")
    product_b = _make_product(db, seller, barcode="2222222222222", name="B")
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=1)
    cells_b = generate_cells(db, "B", racks=1, cells_per_rack=1)
    place_stock(db, product_a, cells_a[0], 50)
    place_stock(db, product_b, cells_b[0], 10)

    with pytest.raises(CellOccupiedError):
        move_stock(db, product_a, cells_a[0], cells_b[0], qty=10, expected_qty=50, actor="tester")


def test_move_stock_empties_source_cell_to_free(db, seller):
    from fulfil.models.storage import CellStatus

    product = _make_product(db, seller)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=1)
    cells_b = generate_cells(db, "B", racks=1, cells_per_rack=1)
    place_stock(db, product, cells_a[0], 50)

    move_stock(db, product, cells_a[0], cells_b[0], qty=50, expected_qty=50, actor="tester")

    db.refresh(cells_a[0])
    db.refresh(cells_b[0])
    assert cells_a[0].status == CellStatus.FREE
    assert cells_b[0].status == CellStatus.OCCUPIED


def test_get_moves_journal_records_actor_and_group(db, seller):
    product = _make_product(db, seller)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=1)
    cells_b = generate_cells(db, "B", racks=1, cells_per_rack=1)
    place_stock(db, product, cells_a[0], 50, actor="receiver")
    move_stock(db, product, cells_a[0], cells_b[0], qty=20, expected_qty=50, actor="mover")

    moves = get_moves(db, product_id=product.id)
    assert len(moves) == 3  # receipt + move_out + move_in
    actors = {m["actor"] for m in moves}
    assert actors == {"receiver", "mover"}
    move_out = next(m for m in moves if m["reason"] == "move_out")
    move_in = next(m for m in moves if m["reason"] == "move_in")
    assert move_out["moveGroupId"] == move_in["moveGroupId"]
    assert move_out["qtyAfter"] == 30
    assert move_in["qtyAfter"] == 20
