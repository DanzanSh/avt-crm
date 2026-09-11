import pytest
from sqlalchemy import select

from fulfil.errors import BarcodeNotAllowedError, CellBlockedError, CellOccupiedError
from fulfil.models.product import Product
from fulfil.models.stock import StockMove
from fulfil.models.storage import CellAllowedBarcode
from fulfil.services.receiving import (
    add_receipt_line,
    get_or_create_open_receipt,
    list_receipt_lines,
    place_stock,
)
from fulfil.services.storage import block_cell, generate_cells, get_cell_contents, get_cell_map


def _make_product(db, seller, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(client_id=seller.id, barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_place_stock_happy_path(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    row = place_stock(db, product, cell, 100)
    assert row.qty == 100
    assert cell.status.value == "occupied"


def test_place_stock_split_across_two_cells_client_scenario(db, seller):
    """Сценарий из бизнес-плана: 100 шт в A-1-10 в понедельник, затем 250 шт в среду —
    75 докладывается в A-1-10, 175 размещается в B-5-5."""
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_a110 = next(c for c in cells if c.address == "A-1-1-10")
    cells_b = generate_cells(db, "B", racks=5, cells_per_rack=5)
    cell_b55 = next(c for c in cells_b if c.address == "B-5-1-5")

    place_stock(db, product, cell_a110, 100)
    row_a = place_stock(db, product, cell_a110, 75)  # докладка того же SKU разрешена
    row_b = place_stock(db, product, cell_b55, 175)

    assert row_a.qty == 175
    assert row_b.qty == 175


def test_place_stock_blocks_other_sku_in_occupied_cell(db, seller):
    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    product_b = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    place_stock(db, product_a, cell, 10)
    with pytest.raises(CellOccupiedError):
        place_stock(db, product_b, cell, 5)


def test_place_stock_blocks_disallowed_barcode(db, seller):
    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    product_b = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    db.add(CellAllowedBarcode(cell_id=cell.id, barcode=product_a.barcode))
    db.commit()

    with pytest.raises(BarcodeNotAllowedError):
        place_stock(db, product_b, cell, 5)

    place_stock(db, product_a, cell, 5)  # разрешённый баркод проходит


def test_place_stock_blocks_when_cell_blocked(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    block_cell(db, cell, "Сломан стеллаж")

    with pytest.raises(CellBlockedError):
        place_stock(db, product, cell, 1)


def test_add_receipt_line_records_actor(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = get_or_create_open_receipt(db, seller)

    line = add_receipt_line(db, receipt, product, cell, 7, actor="operator-1")
    assert line.actor == "operator-1"


def test_list_receipt_lines_newest_first_with_joined_fields(db, seller):
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=2)
    receipt = get_or_create_open_receipt(db, seller)
    add_receipt_line(db, receipt, product, cells[0], 3, actor="op")
    add_receipt_line(db, receipt, product, cells[1], 5, actor="op")

    rows = list_receipt_lines(db)
    assert [r["qty"] for r in rows] == [5, 3]  # newest-first
    assert rows[0]["productName"] == "Майка белая"
    assert rows[0]["cellAddress"] == cells[1].address
    assert rows[0]["actor"] == "op"
    assert rows[0]["receiptNumber"] == receipt.number
    assert rows[0]["clientId"] == seller.id


def test_receiving_fills_cell_contents_map_qty_and_move_ref(db, seller):
    """Приёмка в место → содержимое места, qty на карте и связь движения со строкой (C.5)."""
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=2)
    receipt = get_or_create_open_receipt(db, seller)

    line = add_receipt_line(db, receipt, product, cells[0], 9, actor="op")

    contents = get_cell_contents(db, cells[0])
    assert len(contents) == 1
    assert contents[0]["productId"] == product.id
    assert contents[0]["productName"] == "Майка белая"
    assert contents[0]["qty"] == 9

    data = get_cell_map(db)
    map_cells = data["zones"][0]["racks"][0]["shelves"][0]["cells"]
    by_address = {c["address"]: c for c in map_cells}
    assert by_address[cells[0].address]["qty"] == 9
    assert by_address[cells[0].address]["clientId"] == seller.id
    assert by_address[cells[1].address]["qty"] == 0  # место без остатка
    assert by_address[cells[1].address]["clientId"] is None

    move = db.scalar(select(StockMove).where(StockMove.cell_id == cells[0].id))
    assert move.ref_type == "receipt_line"
    assert move.ref_id == line.id


def test_receipt_history_visible_from_fresh_session(db, seller):
    """Эмуляция F5: история читается новым запросом, не живёт в DOM."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = get_or_create_open_receipt(db, seller)
    add_receipt_line(db, receipt, product, cell, 2, actor="op")

    db.expire_all()  # как будто новая сессия — ничего не закешировано
    rows = list_receipt_lines(db)
    assert len(rows) == 1
    assert rows[0]["qty"] == 2
