from fulfil.integrations.wb.mock import WBMockClient
from fulfil.models.product import Product
from fulfil.services.receiving import place_stock
from fulfil.services.storage import generate_cells
from fulfil.services.stock import get_stock_summary, transfer_to_fbs


def _make_product(db) -> Product:
    p = Product(barcode="2000000000017", name="Майка белая")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_transfer_to_fbs_client_scenario(db):
    """101 принято -> 101 передано -> +59 принято -> 160 всего / 101 в ФБС / 59 доступно."""
    product = _make_product(db)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()

    place_stock(db, product, cell, 101)
    transfer_to_fbs(db, product, 101, idempotency_key="tx-1", wb_client=wb)

    place_stock(db, product, cell, 59)
    summary = get_stock_summary(db, product)

    assert summary["total"] == 160
    assert summary["transferredFbs"] == 101
    assert summary["availableToTransfer"] == 59


def test_transfer_to_fbs_idempotent_on_retry(db):
    product = _make_product(db)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()
    place_stock(db, product, cell, 10)

    first = transfer_to_fbs(db, product, 10, idempotency_key="same-key", wb_client=wb)
    second = transfer_to_fbs(db, product, 10, idempotency_key="same-key", wb_client=wb)

    assert first.id == second.id
    summary = get_stock_summary(db, product)
    assert summary["transferredFbs"] == 10  # не удвоилось при повторе
