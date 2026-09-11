import pytest

from fulfil.errors import AppError
from fulfil.models.product import Product
from fulfil.services.products import (
    create_product,
    delete_product,
    restore_product,
    sync_products_from_wb,
    update_product,
)
from fulfil.services.receiving import place_stock
from fulfil.services.storage import generate_cells


class _FakeWBClient:
    def __init__(self, cards):
        self._cards = cards

    def get_product_cards(self, cursor=None):
        # total 0 < limit 100 → цикл синка завершается за одну страницу
        return {"cards": self._cards, "cursor": {"updatedAt": None, "nmID": None, "total": 0}}


def test_create_product_rejects_invalid_barcode(db, seller):
    with pytest.raises(AppError):
        create_product(db, client_id=seller.id, barcode="abc", name="Товар", actor="tester")


def test_create_product_rejects_duplicate_barcode(db, seller):
    create_product(db, client_id=seller.id, barcode="12345678", name="Товар 1", actor="tester")
    with pytest.raises(AppError):
        create_product(db, client_id=seller.id, barcode="12345678", name="Товар 2", actor="tester")


def test_update_product_marks_field_manual(db, seller):
    product = create_product(db, client_id=seller.id, barcode="12345678", name="Старое имя", actor="tester")
    update_product(db, product, actor="tester", name="Новое имя")
    assert product.name == "Новое имя"
    assert "name" in product.manual_fields


def test_sync_does_not_overwrite_manual_field(db, seller):
    product = create_product(db, client_id=seller.id, barcode="12345678", name="Ручное имя", actor="tester")
    update_product(db, product, actor="tester", name="Ручное имя (изменено)")

    wb = _FakeWBClient([{"barcode": "12345678", "name": "Имя из WB", "nmId": 1}])
    sync_products_from_wb(db, seller, wb)

    db.refresh(product)
    assert product.name == "Ручное имя (изменено)"  # не затёрто синком
    assert product.wb_nm_id == 1  # но wb-поля обновились


def test_change_barcode_forbidden_for_wb_linked_product(db, seller):
    wb = _FakeWBClient([{"barcode": "12345678", "name": "Товар WB", "nmId": 1}])
    sync_products_from_wb(db, seller, wb)
    from sqlalchemy import select

    product = db.scalar(select(Product).where(Product.barcode == "12345678"))

    with pytest.raises(AppError):
        update_product(db, product, actor="tester", barcode="87654321")


def test_change_barcode_updates_cell_allowed_barcodes(db, seller):
    from fulfil.models.storage import CellAllowedBarcode

    product = create_product(db, client_id=seller.id, barcode="12345678", name="Товар", actor="tester")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    db.add(CellAllowedBarcode(cell_id=cell.id, barcode="12345678"))
    db.commit()

    update_product(db, product, actor="tester", barcode="87654321")

    from sqlalchemy import select

    allowed = db.scalar(select(CellAllowedBarcode).where(CellAllowedBarcode.cell_id == cell.id))
    assert allowed.barcode == "87654321"


def test_delete_product_with_stock_blocked(db, seller):
    product = create_product(db, client_id=seller.id, barcode="12345678", name="Товар", actor="tester")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 5)

    with pytest.raises(AppError):
        delete_product(db, product, actor="tester")


def test_delete_product_without_history_is_physical(db, seller):
    product = create_product(db, client_id=seller.id, barcode="12345678", name="Товар", actor="tester")
    outcome = delete_product(db, product, actor="tester")
    assert outcome == "deleted"

    from sqlalchemy import select

    assert db.scalar(select(Product).where(Product.barcode == "12345678")) is None


def test_delete_product_with_history_archives(db, seller):
    product = create_product(db, client_id=seller.id, barcode="12345678", name="Товар", actor="tester")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 5)
    from fulfil.services.stock import write_off_stock

    write_off_stock(db, product, cell, expected_qty=5, actor="tester", comment="брак")

    outcome = delete_product(db, product, actor="tester")
    assert outcome == "archived"
    assert product.archived_at is not None


def test_archived_product_not_found_by_scan(db, seller):
    from fulfil.services.scan import resolve_scan

    product = create_product(db, client_id=seller.id, barcode="12345678", name="Товар", actor="tester")
    delete_product(db, product, actor="tester")  # физически удалён (нет истории)

    from fulfil.errors import NotFoundError

    with pytest.raises(NotFoundError):
        resolve_scan(db, "12345678")


def test_restore_product(db, seller):
    product = create_product(db, client_id=seller.id, barcode="12345678", name="Товар", actor="tester")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 5)
    from fulfil.services.stock import write_off_stock

    write_off_stock(db, product, cell, expected_qty=5, actor="tester", comment="брак")
    delete_product(db, product, actor="tester")
    assert product.archived_at is not None

    restore_product(db, product, actor="tester")
    assert product.archived_at is None
