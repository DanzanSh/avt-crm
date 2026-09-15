from fulfil.integrations.wb.mock import WBMockClient
from fulfil.models.product import Product
from fulfil.services.receiving import place_stock
from fulfil.services.storage import generate_cells
from fulfil.services.stock import (
    get_stock_summary,
    list_stock_summaries,
    refresh_wb_fbs_amounts,
    transfer_to_fbs,
)


def _make_product(db, seller) -> Product:
    seller.wb_warehouse_id = "WH-1"
    db.commit()
    p = Product(client_id=seller.id, barcode="2000000000017", name="Майка белая")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_transfer_to_fbs_client_scenario(db, seller):
    """101 принято -> 101 передано -> +59 принято -> 160 на полках / 101 на WB / 59 доступно."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()

    place_stock(db, product, cell, 101)
    transfer_to_fbs(db, product, 101, idempotency_key="tx-1", wb_client=wb)

    place_stock(db, product, cell, 59)
    summary = get_stock_summary(db, product)

    assert summary["total"] == 160
    assert summary["wbFbsAmount"] == 101
    assert summary["availableToTransfer"] == 59


def test_transfer_to_fbs_idempotent_on_retry(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()
    place_stock(db, product, cell, 10)

    first = transfer_to_fbs(db, product, 10, idempotency_key="same-key", wb_client=wb)
    second = transfer_to_fbs(db, product, 10, idempotency_key="same-key", wb_client=wb)

    assert first.id == second.id
    summary = get_stock_summary(db, product)
    assert summary["wbFbsAmount"] == 10  # не удвоилось при повторе


# --- Этап 2 плана №3, п.2.3: WB ЗАДАЁТ остаток, а не прибавляет к нему --------


def test_transfer_to_fbs_sets_absolute_amount_not_increment(db, seller):
    """Передать 5, потом 3 (двумя разными вызовами) -> на WB именно 8, а не 3
    (старый баг: PUT отправлял голый qty и стирал предыдущее значение)."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()
    place_stock(db, product, cell, 20)

    first = transfer_to_fbs(db, product, 5, idempotency_key="k1", wb_client=wb)
    assert first.wb_amount_before == 0
    assert first.wb_amount_after == 5

    second = transfer_to_fbs(db, product, 3, idempotency_key="k2", wb_client=wb)
    assert second.wb_amount_before == 5
    assert second.wb_amount_after == 8

    # Мок хранит остаток per-warehouse — читаем его напрямую как "истину" WB.
    assert wb.get_fbs_stocks("WH-1", [product.barcode]) == {product.barcode: 8}
    db.refresh(product)
    assert product.wb_fbs_amount == 8


def test_transfer_to_fbs_records_error_text_on_failure(db, seller, monkeypatch):
    """Вместо голого except Exception — узкий except WbApiError, текст причины
    попадает в wb_response (Этап 2, п.2.3)."""
    from fulfil.errors import WbApiError
    from fulfil.models.stock import FbsTransferStatus

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 10)
    wb = WBMockClient()

    def _boom(*a, **kw):
        raise WbApiError("WB временно недоступен", upstream_status=503)

    monkeypatch.setattr(wb, "get_fbs_stocks", _boom)

    transfer = transfer_to_fbs(db, product, 5, idempotency_key="k-fail", wb_client=wb)
    assert transfer.status == FbsTransferStatus.FAILED
    assert "WB временно недоступен" in transfer.wb_response


# --- Формула «можно передать» учитывает несобранные заказы --------------------


def _make_order_with_item(db, seller, product, qty, status):
    from fulfil.models.fbs import Order, OrderItem

    order = Order(client_id=seller.id, wb_order_id=f"WB-{product.id}-{status.value}", status=status)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=qty))
    db.commit()
    return order


def test_available_to_transfer_subtracts_unassembled_orders(db, seller):
    from fulfil.models.fbs import OrderStatus

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 100)
    _make_order_with_item(db, seller, product, 30, OrderStatus.CONFIRMED)

    summary = get_stock_summary(db, product)
    assert summary["inOrders"] == 30
    assert summary["availableToTransfer"] == 70


def test_available_to_transfer_ignores_packed_orders(db, seller):
    """PACKED уже прошёл commit_pick_lines() — остаток списан с полки, повторно
    вычитать его из «доступно» значило бы посчитать дважды."""
    from fulfil.models.fbs import OrderStatus

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 100)
    _make_order_with_item(db, seller, product, 30, OrderStatus.PACKED)

    summary = get_stock_summary(db, product)
    assert summary["inOrders"] == 0
    assert summary["availableToTransfer"] == 100


def test_list_stock_summaries_matches_per_product(db, seller):
    """Массовая версия (три GROUP BY на весь список, Этап 2, п.2.3) должна давать
    те же цифры, что get_stock_summary() по одному товару — иначе N+1-фикс молча
    расходится со старым поведением."""
    from fulfil.models.fbs import OrderStatus

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 50)
    _make_order_with_item(db, seller, product, 10, OrderStatus.NEW)
    wb = WBMockClient()
    transfer_to_fbs(db, product, 5, idempotency_key="k-bulk", wb_client=wb)

    single = get_stock_summary(db, product)
    bulk = list_stock_summaries(db)[product.id]
    assert single == bulk

    scoped = list_stock_summaries(db, client_id=seller.id)[product.id]
    assert scoped == single


# --- P0-3: кэш wb_fbs_amount должен подтягиваться с WB, не только при передаче ---


def test_refresh_wb_fbs_amounts_picks_up_wb_side_sales(db, seller):
    """WB сам уменьшает остаток при продаже — без периодического опроса кэш
    Product.wb_fbs_amount расходился бы с реальностью (P0-3): availableToTransfer
    занижался бы на всё проданное после последней передачи, и появлялся бы ложный
    fbsOversold."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()
    place_stock(db, product, cell, 100)
    transfer_to_fbs(db, product, 40, idempotency_key="k1", wb_client=wb)
    db.refresh(product)
    assert product.wb_fbs_amount == 40

    # WB "продал" 15 штук независимо от нас — на его стороне остаток стал 25,
    # наш кэш об этом ещё не знает.
    wb.set_fbs_stocks("WH-1", {product.barcode: 25})

    updated = refresh_wb_fbs_amounts(db, seller, wb)
    assert updated == 1
    db.refresh(product)
    assert product.wb_fbs_amount == 25

    summary = get_stock_summary(db, product)
    assert summary["wbFbsAmount"] == 25
    assert summary["fbsOversold"] == 0


def test_refresh_wb_fbs_amounts_without_warehouse_is_noop(db, seller):
    seller.wb_warehouse_id = None
    db.commit()
    product = Product(client_id=seller.id, barcode="2000000000024", name="Майка чёрная")
    db.add(product)
    db.commit()

    assert refresh_wb_fbs_amounts(db, seller, WBMockClient()) == 0


# --- P0-3/P1-7: гонка двух параллельных передач на один и тот же товар ---------


def test_transfer_to_fbs_locks_product_row(db, seller):
    """with_for_update() на товаре — вторая передача видит уже обновлённый остаток,
    а не тот, что был на момент её собственной проверки (иначе обе передачи могли
    бы пройти проверку availableToTransfer по одному и тому же исходному значению)."""
    from sqlalchemy import select as sa_select

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    wb = WBMockClient()
    place_stock(db, product, cell, 10)

    transfer_to_fbs(db, product, 10, idempotency_key="k1", wb_client=wb)
    db.refresh(product)
    assert product.wb_fbs_amount == 10

    # Вся доступная величина уже передана — вторая (независимая) передача должна
    # быть отклонена, а не молча продавить WB в минус.
    from fulfil.errors import AppError

    import pytest

    with pytest.raises(AppError) as exc_info:
        transfer_to_fbs(db, product, 1, idempotency_key="k2", wb_client=wb)
    assert exc_info.value.reason_code == "not_enough_stock"
    assert db.scalar(sa_select(Product).where(Product.id == product.id)).wb_fbs_amount == 10
