"""Заказы ФБС — фильтр по складу продавца, статусы WB, unknown_sku, поставки
при «взятии в работу» (Этап 3 плана №3, пп.4.1, 4.2 problems.txt)."""

import pytest
from sqlalchemy import func, select

from conftest import make_client
from fulfil.errors import AppError
from fulfil.integrations.wb.mock import WBMockClient
from fulfil.models.fbs import Order, OrderStatus, Supply, SupplyStatus
from fulfil.services.orders import (
    get_order_counters,
    list_orders,
    refresh_order_statuses,
    sync_orders_from_wb,
    take_to_work,
    take_to_work_bulk,
)


def _with_warehouse(client):
    client.wb_warehouse_id = f"WH-{client.id}"
    return client


# --- Синхронизация: фильтр по складу и unknown_sku ---------------------------


def test_sync_orders_requires_warehouse(db, seller):
    with pytest.raises(AppError) as exc_info:
        sync_orders_from_wb(db, seller, WBMockClient(client_id=seller.id))
    assert exc_info.value.reason_code == "no_wb_warehouse"


def test_sync_orders_filters_foreign_warehouse(db, seller):
    """Заказ с чужого склада продавца не импортируется (problems.txt, п.4.2) —
    только warehouseId, совпадающий со складом клиента."""
    _with_warehouse(seller)
    db.commit()
    wb = WBMockClient(client_id=seller.id)
    wb.add_order(warehouseId="WH-SOMEONE-ELSE")

    created = sync_orders_from_wb(db, seller, wb)

    assert len(created) == 1
    assert created[0].wb_order_id == f"WB-ORDER-DEMO-{seller.id}"
    assert created[0].wb_warehouse_id == seller.wb_warehouse_id


def test_sync_orders_unknown_sku_sets_problem_and_resyncs_cards(db, seller):
    """Позиция с нераспознанным баркодом не теряется молча (problems.txt, п.6):
    заказ сохраняется с problem='unknown_sku'. Заодно проверяет retry через
    пересинхронизацию карточек — баркод дефолтного заказа находится ПОСЛЕ неё."""
    _with_warehouse(seller)
    db.commit()
    wb = WBMockClient(client_id=seller.id)
    wb.add_order(items=[{"barcode": "9999999999999", "qty": 1}])

    created = sync_orders_from_wb(db, seller, wb)

    assert len(created) == 2
    known = next(o for o in created if o.wb_order_id.startswith("WB-ORDER-DEMO"))
    unknown = next(o for o in created if o.wb_order_id != known.wb_order_id)

    assert known.problem is None
    assert known.items[0].product_id is not None  # найден пересинхронизацией карточек

    assert unknown.problem == "unknown_sku"
    assert unknown.items[0].product_id is None
    assert unknown.items[0].barcode == "9999999999999"  # позиция сохранена, а не потеряна


def test_sync_orders_records_error_and_rolls_back_batch(db, seller):
    """Сбой WB API среди заказов откатывает всю пачку этого вызова (как и было —
    один commit на весь sync_orders_from_wb) и пишет причину в last_sync_error,
    не проглатывая исключение (Этап 2 плана №3, п.2.3: тот же приём, что и у
    transfer_to_fbs — except WbApiError, а не голый except Exception)."""
    from fulfil.errors import WbApiError

    _with_warehouse(seller)
    db.commit()

    class _BoomAfterFirstItem(WBMockClient):
        def get_product_cards(self, cursor=None):
            raise WbApiError("WB временно недоступен", upstream_status=503)

    wb = _BoomAfterFirstItem(client_id=seller.id)
    wb.add_order(items=[{"barcode": "9999999999999", "qty": 1}])  # заставит вызвать resync карточек

    with pytest.raises(WbApiError):
        sync_orders_from_wb(db, seller, wb)

    db.refresh(seller)
    assert "WB временно недоступен" in seller.last_sync_error
    total = db.scalar(select(func.count()).select_from(Order).where(Order.client_id == seller.id))
    assert total == 0  # пачка откатилась целиком


def test_sync_orders_skips_already_imported(db, seller):
    _with_warehouse(seller)
    db.commit()
    wb = WBMockClient(client_id=seller.id)
    first = sync_orders_from_wb(db, seller, wb)
    assert len(first) == 1

    wb.add_order()  # ещё один заказ в очереди, но с тем же orderId по умолчанию…
    wb._pending_orders[0]["orderId"] = first[0].wb_order_id  # …имитируем повтор от WB
    second = sync_orders_from_wb(db, seller, wb)
    assert second == []  # уже импортирован — не дублируется

    total = db.scalar(select(func.count()).select_from(Order).where(Order.client_id == seller.id))
    assert total == 1


# --- Статусы: отмена покупателем -------------------------------------------


def test_refresh_order_statuses_cancels_order(db, seller):
    _with_warehouse(seller)
    order = Order(client_id=seller.id, wb_order_id="CANCEL-1", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.commit()

    wb = WBMockClient(client_id=seller.id)
    wb.cancel_order("CANCEL-1")
    result = refresh_order_statuses(db, seller, wb)

    db.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.wb_status == "canceled_by_client"
    assert result == {"checked": 1, "cancelled": 1}


def test_refresh_order_statuses_ignores_completed_orders(db, seller):
    """SHIPPED — уже финальный статус, опрашивать нечего."""
    order = Order(client_id=seller.id, wb_order_id="DONE-1", status=OrderStatus.SHIPPED)
    db.add(order)
    db.commit()

    result = refresh_order_statuses(db, seller, WBMockClient(client_id=seller.id))
    assert result == {"checked": 0, "cancelled": 0}


# --- «Взять в работу»: подтверждение через поставку -------------------------


def test_take_to_work_confirms_order_via_supply(db, seller):
    order = Order(client_id=seller.id, wb_order_id="TW-1", status=OrderStatus.NEW)
    db.add(order)
    db.commit()

    take_to_work(db, order, WBMockClient(client_id=seller.id))

    assert order.status == OrderStatus.CONFIRMED
    assert order.supply_id is not None


def test_take_to_work_rejects_problem_order(db, seller):
    order = Order(client_id=seller.id, wb_order_id="TW-2", status=OrderStatus.NEW, problem="unknown_sku")
    db.add(order)
    db.commit()

    with pytest.raises(AppError) as exc_info:
        take_to_work(db, order, WBMockClient(client_id=seller.id))
    assert exc_info.value.reason_code == "order_has_problem"


def test_take_to_work_two_orders_same_client_share_one_supply(db, seller):
    order1 = Order(client_id=seller.id, wb_order_id="TW-A", status=OrderStatus.NEW)
    order2 = Order(client_id=seller.id, wb_order_id="TW-B", status=OrderStatus.NEW)
    db.add_all([order1, order2])
    db.commit()

    wb = WBMockClient(client_id=seller.id)
    take_to_work(db, order1, wb)
    take_to_work(db, order2, wb)

    assert order1.supply_id == order2.supply_id
    count = db.scalar(select(func.count()).select_from(Supply).where(Supply.client_id == seller.id))
    assert count == 1


def test_take_to_work_bulk_one_supply_per_client(db, monkeypatch):
    """Массовое «Взять в работу выбранные» — одна поставка на каждого клиента,
    а не одна на каждый заказ (Этап 3, п.3.2)."""
    c1 = make_client(db, name="Клиент 1")
    c2 = make_client(db, name="Клиент 2")
    o1 = Order(client_id=c1.id, wb_order_id="BULK-1", status=OrderStatus.NEW)
    o2 = Order(client_id=c1.id, wb_order_id="BULK-2", status=OrderStatus.NEW)
    o3 = Order(client_id=c2.id, wb_order_id="BULK-3", status=OrderStatus.NEW)
    db.add_all([o1, o2, o3])
    db.commit()

    mocks: dict[int, WBMockClient] = {}

    def _fake_get_wb_client(client):
        return mocks.setdefault(client.id, WBMockClient(client_id=client.id))

    monkeypatch.setattr("fulfil.services.orders.get_wb_client", _fake_get_wb_client)

    results = take_to_work_bulk(db, [o1.id, o2.id, o3.id])

    assert all(r["ok"] for r in results)
    db.refresh(o1)
    db.refresh(o2)
    db.refresh(o3)
    assert o1.supply_id == o2.supply_id
    assert o1.supply_id != o3.supply_id


def test_take_to_work_bulk_reports_missing_order(db):
    order_missing_id = 999999
    results = take_to_work_bulk(db, [order_missing_id])
    assert results == [
        {"orderId": order_missing_id, "ok": False, "error": f"Заказ #{order_missing_id} не найден."}
    ]


# --- Счётчики и группы -------------------------------------------------------


def test_order_counters_and_group_filters(db, seller):
    problem_order = Order(client_id=seller.id, wb_order_id="C-1", status=OrderStatus.NEW, problem="unknown_sku")
    confirmed = Order(client_id=seller.id, wb_order_id="C-2", status=OrderStatus.CONFIRMED)
    in_assembly = Order(client_id=seller.id, wb_order_id="C-3", status=OrderStatus.IN_ASSEMBLY)
    db.add_all([problem_order, confirmed, in_assembly])
    db.commit()

    supply_open = Supply(client_id=seller.id, status=SupplyStatus.OPEN)
    supply_closed = Supply(client_id=seller.id, status=SupplyStatus.CLOSED)
    db.add_all([supply_open, supply_closed])
    db.flush()

    packed_open = Order(
        client_id=seller.id, wb_order_id="C-4", status=OrderStatus.PACKED, supply_id=supply_open.id
    )
    packed_closed = Order(
        client_id=seller.id, wb_order_id="C-5", status=OrderStatus.PACKED, supply_id=supply_closed.id
    )
    shipped = Order(client_id=seller.id, wb_order_id="C-6", status=OrderStatus.SHIPPED)
    db.add_all([packed_open, packed_closed, shipped])
    db.commit()

    counters = get_order_counters(db, client_id=seller.id)
    assert counters == {"new": 1, "assembly": 2, "packed": 1, "problems": 1}

    assert {o.wb_order_id for o in list_orders(db, client_id=seller.id, group="new")} == {"C-1"}
    assert {o.wb_order_id for o in list_orders(db, client_id=seller.id, group="assembly")} == {"C-2", "C-3"}
    assert {o.wb_order_id for o in list_orders(db, client_id=seller.id, group="packed")} == {"C-4"}
    assert {o.wb_order_id for o in list_orders(db, client_id=seller.id, group="archive")} == {"C-5", "C-6"}
