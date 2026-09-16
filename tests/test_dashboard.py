import datetime as dt

from conftest import make_client

from fulfil.integrations.wb.mock import WBMockClient
from fulfil.models.fbs import Order, OrderStatus
from fulfil.models.product import Product
from fulfil.services.dashboard import (
    get_dashboard_summary,
    stock_totals,
    top_clients_by_stock,
    zone_fill,
)
from fulfil.services.receiving import place_stock
from fulfil.services.storage import block_cell, generate_cells
from fulfil.services.stock import transfer_to_fbs


def _make_product(db, client, barcode="2000000000017") -> Product:
    p = Product(client_id=client.id, barcode=barcode, name="Майка белая")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_stock_totals_counts_qty_sku_and_cell_fill(db, seller):
    product_a = _make_product(db, seller, barcode="2000000000017")
    product_b = _make_product(db, seller, barcode="2000000000024")
    [cell1, cell2] = generate_cells(db, "A", racks=1, cells_per_rack=2)

    place_stock(db, product_a, cell1, 10)
    place_stock(db, product_b, cell2, 5)

    totals = stock_totals(db)
    assert totals["totalQty"] == 15
    assert totals["skuWithStock"] == 2
    assert totals["cellsOccupied"] == 2
    assert totals["cellsTotal"] == 2
    assert totals["cellsFillPercent"] == 100.0


def test_stock_totals_filters_by_client(db, seller):
    other = make_client(db, name="Другой клиент")
    product_a = _make_product(db, seller, barcode="2000000000017")
    product_b = _make_product(db, other, barcode="2000000000031")
    [cell1, cell2] = generate_cells(db, "A", racks=1, cells_per_rack=2)

    place_stock(db, product_a, cell1, 10)
    place_stock(db, product_b, cell2, 40)

    totals = stock_totals(db, client_id=seller.id)
    assert totals["totalQty"] == 10
    assert totals["skuWithStock"] == 1
    # Заполненность мест — всегда глобальная, фильтр клиента её не сужает.
    assert totals["cellsOccupied"] == 2


def test_top_clients_by_stock_ignores_client_filter_by_design(db, seller):
    """topClientsByStock всегда по всем клиентам — согласовано с заказчиком."""
    other = make_client(db, name="Клиент Б")
    product_a = _make_product(db, seller, barcode="2000000000017")
    product_b = _make_product(db, other, barcode="2000000000031")
    [cell1, cell2] = generate_cells(db, "A", racks=1, cells_per_rack=2)

    place_stock(db, product_a, cell1, 30)
    place_stock(db, product_b, cell2, 90)

    top = top_clients_by_stock(db)
    assert [t["clientName"] for t in top] == ["Клиент Б", "Тестовый клиент"]
    assert top[0]["qty"] == 90


def test_zone_fill_reports_occupied_over_total_per_zone(db, seller):
    product = _make_product(db, seller)
    cells_a = generate_cells(db, "A", racks=1, cells_per_rack=3)
    generate_cells(db, "B", racks=1, cells_per_rack=2)

    place_stock(db, product, cells_a[0], 5)

    fill = {z["zoneCode"]: z for z in zone_fill(db)}
    assert fill["A"]["occupied"] == 1
    assert fill["A"]["total"] == 3
    assert fill["B"]["occupied"] == 0
    assert fill["B"]["total"] == 2


def test_dashboard_summary_attention_items(db, seller):
    seller.wb_warehouse_id = "WH-1"
    seller.last_sync_error = "401 просрочен токен"
    seller.wb_token_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    db.commit()

    order = Order(client_id=seller.id, wb_order_id="ORD-1", status=OrderStatus.NEW, problem="unknown_sku")
    db.add(order)
    db.commit()

    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    [blocked_cell] = generate_cells(db, "B", racks=1, cells_per_rack=1)
    block_cell(db, blocked_cell, "повреждена")

    product = _make_product(db, seller)
    place_stock(db, product, cell, 5)
    wb = WBMockClient()
    transfer_to_fbs(db, product, 5, idempotency_key="tx-1", wb_client=wb)
    # Списываем с полки после передачи — на WB остаётся больше, чем физически
    # есть, это и есть fbsOversold (см. services.stock._summary_from_parts).
    from fulfil.services.stock import write_off_stock

    write_off_stock(db, product, cell, expected_qty=5, actor="test", comment="усушка")

    summary = get_dashboard_summary(db)
    types = {item["type"] for item in summary["attention"]}
    assert types == {"unknown_sku", "sync_error", "key_expired", "fbs_oversold", "blocked_cells"}


def test_dashboard_summary_counters_are_camel_case_and_client_scoped(db, seller):
    order = Order(client_id=seller.id, wb_order_id="ORD-2", status=OrderStatus.NEW)
    db.add(order)
    db.commit()

    summary = get_dashboard_summary(db, client_id=seller.id)
    assert summary["orders"]["new"] == 1
    assert "inDelivery" in summary["supplies"]
    assert "inProgress" in summary["receipts"]
