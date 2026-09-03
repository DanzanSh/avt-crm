import pytest

from fulfil.errors import BarcodeNotAllowedError, CellBlockedError, CellOccupiedError
from fulfil.models.product import Product
from fulfil.models.storage import CellAllowedBarcode
from fulfil.services.receiving import place_stock
from fulfil.services.storage import block_cell, generate_cells


def _make_product(db, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_place_stock_happy_path(db):
    product = _make_product(db)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    row = place_stock(db, product, cell, 100)
    assert row.qty == 100
    assert cell.status.value == "occupied"


def test_place_stock_split_across_two_cells_client_scenario(db):
    """Сценарий из бизнес-плана: 100 шт в A-1-10 в понедельник, затем 250 шт в среду —
    75 докладывается в A-1-10, 175 размещается в B-5-5."""
    product = _make_product(db)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_a110 = next(c for c in cells if c.address == "A-1-10")
    cells_b = generate_cells(db, "B", racks=5, cells_per_rack=5)
    cell_b55 = next(c for c in cells_b if c.address == "B-5-5")

    place_stock(db, product, cell_a110, 100)
    row_a = place_stock(db, product, cell_a110, 75)  # докладка того же SKU разрешена
    row_b = place_stock(db, product, cell_b55, 175)

    assert row_a.qty == 175
    assert row_b.qty == 175


def test_place_stock_blocks_other_sku_in_occupied_cell(db):
    product_a = _make_product(db, barcode="1111111111111", name="Товар A")
    product_b = _make_product(db, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    place_stock(db, product_a, cell, 10)
    with pytest.raises(CellOccupiedError):
        place_stock(db, product_b, cell, 5)


def test_place_stock_blocks_disallowed_barcode(db):
    product_a = _make_product(db, barcode="1111111111111", name="Товар A")
    product_b = _make_product(db, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    db.add(CellAllowedBarcode(cell_id=cell.id, barcode=product_a.barcode))
    db.commit()

    with pytest.raises(BarcodeNotAllowedError):
        place_stock(db, product_b, cell, 5)

    place_stock(db, product_a, cell, 5)  # разрешённый баркод проходит


def test_place_stock_blocks_when_cell_blocked(db):
    product = _make_product(db)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    block_cell(db, cell, "Сломан стеллаж")

    with pytest.raises(CellBlockedError):
        place_stock(db, product, cell, 1)
