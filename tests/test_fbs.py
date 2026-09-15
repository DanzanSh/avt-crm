"""Заказы и поставки ФБС — фильтр по складу продавца, статусы WB, unknown_sku,
поставки при «взятии в работу» (Этап 3 плана №3, пп.4.1, 4.2), статус доставки/
приёмки поставок (Этап 4, п.6 problems.txt)."""

import datetime as dt

import pytest
from sqlalchemy import func, select

from conftest import make_client
from fulfil.errors import AppError, WbApiError
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
from fulfil.services.supplies import (
    close_supply,
    create_supply,
    get_supply_counters,
    list_supplies,
    sync_supplies,
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


def test_sync_orders_resolves_second_sku_of_same_size(db, seller):
    """WB может отдать больше одного sku для одного размера карточки (P2-11) —
    заказ, пришедший со вторым sku, должен находить тот же товар через
    Product.extra_barcodes, а не получать problem='unknown_sku'."""
    _with_warehouse(seller)
    db.commit()
    primary_barcode = f"20000{seller.id:03d}0017"
    extra_barcode = f"20000{seller.id:03d}0099"

    class _TwoSkusClient(WBMockClient):
        def get_product_cards(self, cursor=None):
            page = super().get_product_cards(cursor)
            if not cursor:
                page["cards"][0]["extraBarcodes"] = [extra_barcode]
            return page

    wb = _TwoSkusClient(client_id=seller.id)
    wb._pending_orders = []  # без дефолтного заказа по первому баркоду
    wb.add_order(items=[{"barcode": extra_barcode, "qty": 1}])

    created = sync_orders_from_wb(db, seller, wb)

    assert len(created) == 1
    assert created[0].problem is None
    assert created[0].items[0].product_id is not None
    assert created[0].items[0].barcode == extra_barcode

    from fulfil.models.product import Product

    product = db.get(Product, created[0].items[0].product_id)
    assert product.barcode == primary_barcode
    assert product.extra_barcodes == [extra_barcode]


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


def test_refresh_order_statuses_sets_delivered(db, seller):
    """Жизненный цикл раньше не доходил до конца (P1-6): статус опрашивался
    только на отмену, "доставлен" никогда не выставлялся."""
    order = Order(client_id=seller.id, wb_order_id="SOLD-1", status=OrderStatus.PACKED)
    db.add(order)
    db.commit()

    wb = WBMockClient(client_id=seller.id)
    wb._order_statuses["SOLD-1"] = {"wbStatus": "sold", "supplierStatus": "sold"}

    result = refresh_order_statuses(db, seller, wb)
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERED
    assert result == {"checked": 1, "cancelled": 0}


def test_refresh_order_statuses_cancel_after_packed_returns_stock(db, seller):
    """Покупатель отменил заказ ПОСЛЕ сборки — остаток уже списан
    commit_pick_lines(), при отмене он должен вернуться на ту же ячейку (P1-5)."""
    from fulfil.models.fbs import OrderItem, PickLine
    from fulfil.models.product import Product
    from fulfil.models.stock import StockByCell
    from fulfil.services.picking import build_pick_list, commit_pick_lines
    from fulfil.services.receiving import place_stock
    from fulfil.services.storage import generate_cells

    _with_warehouse(seller)
    product = Product(client_id=seller.id, barcode="2000000000048", name="Товар")
    db.add(product)
    db.flush()
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 10)

    order = Order(client_id=seller.id, wb_order_id="PACKED-CANCEL", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=4))
    db.commit()
    db.refresh(order)

    build_pick_list(db, order)
    commit_pick_lines(db, order)
    order.status = OrderStatus.PACKED
    db.commit()

    row = db.scalar(select(StockByCell).where(StockByCell.product_id == product.id))
    assert row.qty == 6  # 10 - 4 списано подбором

    wb = WBMockClient(client_id=seller.id)
    wb.cancel_order("PACKED-CANCEL")
    refresh_order_statuses(db, seller, wb)

    db.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    row = db.scalar(select(StockByCell).where(StockByCell.product_id == product.id))
    assert row.qty == 10  # возвращено обратно

    remaining_lines = db.scalars(select(PickLine).where(PickLine.order_id == order.id)).all()
    assert all(line.picked_at is not None for line in remaining_lines)  # списанные строки не трогаем


def test_refresh_order_statuses_cancel_before_packing_releases_pick_lines(db, seller):
    """Отмена ДО сборки (заказ ещё CONFIRMED, лист подбора уже построен, но не
    списан) — незавершённые строки листа должны быть удалены (P1-5), иначе они
    висят и блокируют резерв под другие заказы."""
    from fulfil.models.fbs import OrderItem, PickLine
    from fulfil.models.product import Product
    from fulfil.services.picking import build_pick_list
    from fulfil.services.receiving import place_stock
    from fulfil.services.storage import generate_cells

    _with_warehouse(seller)
    product = Product(client_id=seller.id, barcode="2000000000055", name="Товар")
    db.add(product)
    db.flush()
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 10)

    order = Order(client_id=seller.id, wb_order_id="PRE-CANCEL", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, barcode=product.barcode, qty=4))
    db.commit()
    db.refresh(order)

    build_pick_list(db, order)  # бронирует, но не списывает

    wb = WBMockClient(client_id=seller.id)
    wb.cancel_order("PRE-CANCEL")
    refresh_order_statuses(db, seller, wb)

    assert db.scalars(select(PickLine).where(PickLine.order_id == order.id)).all() == []


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


def test_list_supplies_paginates(db, seller):
    """limit/offset (P3): раньше list_supplies грузила всё без ограничения."""
    for i in range(5):
        db.add(Supply(client_id=seller.id, status=SupplyStatus.OPEN))
    db.commit()

    page1 = list_supplies(db, client_id=seller.id, limit=2, offset=0)
    page2 = list_supplies(db, client_id=seller.id, limit=2, offset=2)

    assert len(page1) == 2
    assert len(page2) == 2
    assert {s.id for s in page1}.isdisjoint({s.id for s in page2})
    # Порядок стабилен (id desc) — постранично не должно терять/дублировать записи.
    assert list_supplies(db, client_id=seller.id, limit=100)[:2] == page1


def test_packed_order_without_supply_counts_as_packed_not_archive(db, seller):
    """PACKED без supply_id (Order.supply_id IS NULL) раньше пропадал из обеих
    вкладок — `Supply.status == OPEN` на NULL supply_id даёт NULL, не true (P2-15)."""
    packed_no_supply = Order(client_id=seller.id, wb_order_id="C-7", status=OrderStatus.PACKED, supply_id=None)
    db.add(packed_no_supply)
    db.commit()

    counters = get_order_counters(db, client_id=seller.id)
    assert counters["packed"] == 1
    assert counters["new"] == counters["assembly"] == counters["problems"] == 0

    assert {o.wb_order_id for o in list_orders(db, client_id=seller.id, group="packed")} == {"C-7"}
    assert list_orders(db, client_id=seller.id, group="archive") == []


# --- Поставки: статус выводится из done/scanDt (Этап 4, п.6 problems.txt) ----


def test_create_supply_persists_name(db, seller):
    """Имя раньше уходило только в WB и терялось у нас (Этап 3, «Что вышло
    иначе» №2) — теперь персистится."""
    supply = create_supply(db, seller, WBMockClient(client_id=seller.id))
    assert supply.name == f"{seller.name} {dt.date.today().isoformat()}"


def test_close_supply_rejects_unassembled_orders(db, seller):
    wb = WBMockClient(client_id=seller.id)
    supply = create_supply(db, seller, wb)
    unassembled = Order(
        client_id=seller.id, wb_order_id="SUP-1", status=OrderStatus.CONFIRMED, supply_id=supply.id
    )
    db.add(unassembled)
    db.commit()

    with pytest.raises(AppError) as exc_info:
        close_supply(db, supply, wb)

    assert exc_info.value.reason_code == "unassembled_orders"
    assert exc_info.value.extra["orders"] == [{"id": unassembled.id, "wbOrderId": "SUP-1"}]
    db.refresh(supply)
    assert supply.status == SupplyStatus.OPEN  # ничего не изменилось


def test_close_supply_ships_packed_orders(db, seller):
    wb = WBMockClient(client_id=seller.id)
    supply = create_supply(db, seller, wb)
    packed = Order(client_id=seller.id, wb_order_id="SUP-2", status=OrderStatus.PACKED, supply_id=supply.id)
    db.add(packed)
    db.commit()

    result = close_supply(db, supply, wb)

    assert result.status == SupplyStatus.IN_DELIVERY
    assert result.wb_done is True
    assert result.closed_at is not None
    db.refresh(packed)
    assert packed.status == OrderStatus.SHIPPED


def test_close_supply_wrong_status_rejected(db, seller):
    supply = Supply(client_id=seller.id, status=SupplyStatus.IN_DELIVERY)
    db.add(supply)
    db.commit()

    with pytest.raises(AppError) as exc_info:
        close_supply(db, supply, WBMockClient(client_id=seller.id))
    assert exc_info.value.reason_code == "wrong_status"


def test_sync_supplies_reflects_done_and_scan_dt(db, seller):
    """done=false -> На сборке, done=true без scanDt -> В доставке, scanDt
    заполнен -> Принята (Этап 4, п.4.1 плана №3)."""
    wb = WBMockClient(client_id=seller.id)
    supply = create_supply(db, seller, wb)

    sync_supplies(db, seller, wb)
    db.refresh(supply)
    assert supply.status == SupplyStatus.OPEN

    wb.close_supply(supply.wb_supply_id)  # деливери напрямую на стороне WB
    sync_supplies(db, seller, wb)
    db.refresh(supply)
    assert supply.status == SupplyStatus.IN_DELIVERY
    assert supply.wb_done is True
    assert supply.closed_at_wb is not None

    wb.mark_supply_accepted(supply.wb_supply_id)
    sync_supplies(db, seller, wb)
    db.refresh(supply)
    assert supply.status == SupplyStatus.ACCEPTED
    assert supply.scan_dt is not None


def test_sync_supplies_imports_foreign_supply_with_our_order(db, seller):
    """Поставка, собранная селлером в личном кабинете WB (не через create_supply),
    но содержащая наш заказ — импортируется (Этап 4, п.4.2)."""
    _with_warehouse(seller)
    order = Order(client_id=seller.id, wb_order_id="FOREIGN-ORD-1", status=OrderStatus.CONFIRMED)
    db.add(order)
    db.commit()

    wb = WBMockClient(client_id=seller.id)
    foreign_supply_id = wb.create_supply("Собрана в кабинете WB")
    wb.add_order_to_supply(foreign_supply_id, "FOREIGN-ORD-1")

    result = sync_supplies(db, seller, wb)

    assert result["imported"] == 1
    supply = db.scalar(select(Supply).where(Supply.wb_supply_id == foreign_supply_id))
    assert supply is not None
    assert supply.client_id == seller.id


def test_sync_supplies_ignores_foreign_supply_without_our_orders(db, seller):
    """Поставка другого склада селлера, где наших заказов нет, игнорируется —
    не наша зона интересов (Этап 4, п.4.2)."""
    wb = WBMockClient(client_id=seller.id)
    foreign_supply_id = wb.create_supply("Совсем чужая")
    wb.add_order_to_supply(foreign_supply_id, "NOT-OUR-ORDER")

    result = sync_supplies(db, seller, wb)

    assert result["imported"] == 0
    assert db.scalar(select(Supply).where(Supply.wb_supply_id == foreign_supply_id)) is None


def test_sync_supplies_does_not_reask_ignored_foreign_supply(db, seller):
    """P1-4: поставка, однажды опознанная как "не наша", не должна снова
    запрашивать GET .../{id}/orders на следующих циклах — иначе каждый проход
    фонового опроса заново дёргает WB по всем чужим/историческим поставкам
    продавца и упирается в лимиты."""
    calls = {"n": 0}

    class _CountingClient(WBMockClient):
        def get_supply_orders(self, supply_id):
            calls["n"] += 1
            return super().get_supply_orders(supply_id)

    wb = _CountingClient(client_id=seller.id)
    foreign_supply_id = wb.create_supply("Совсем чужая")
    wb.add_order_to_supply(foreign_supply_id, "NOT-OUR-ORDER")

    sync_supplies(db, seller, wb)
    assert calls["n"] == 1

    sync_supplies(db, seller, wb)
    assert calls["n"] == 1  # не спросили снова


def test_sync_supplies_ships_packed_orders_when_closed_outside_fulfil(db, seller):
    """Поставку закрыли в личном кабинете WB, минуя close_supply() — заказы
    внутри неё должны перейти в SHIPPED при следующем sync_supplies() (P1-6),
    иначе они навсегда остаются PACKED."""
    wb = WBMockClient(client_id=seller.id)
    supply = create_supply(db, seller, wb)
    packed = Order(client_id=seller.id, wb_order_id="OUTSIDE-CLOSE-1", status=OrderStatus.PACKED, supply_id=supply.id)
    db.add(packed)
    db.commit()

    wb.close_supply(supply.wb_supply_id)  # done=True напрямую на стороне WB
    sync_supplies(db, seller, wb)

    db.refresh(packed)
    assert packed.status == OrderStatus.SHIPPED
    db.refresh(supply)
    assert supply.status == SupplyStatus.IN_DELIVERY


def test_sync_supplies_skips_supply_when_orders_endpoint_404s(db, seller):
    """WB отвечает 404 "path not found" на GET .../{id}/orders для части
    поставок (проверено на реальном токене) — такая поставка-кандидат
    пропускается, а не роняет синк остальных поставок клиента: уже известные
    нам поставки этот вызов вообще не затрагивает (см. known_ids)."""

    class _BoomOnOrders(WBMockClient):
        def get_supply_orders(self, supply_id):
            raise WbApiError('WB вернул 404 "path not found"', upstream_status=404)

    wb = _BoomOnOrders(client_id=seller.id)
    broken_supply_id = wb.create_supply("Недоступная поставка")
    wb.add_order_to_supply(broken_supply_id, "SOME-ORDER")

    known = create_supply(db, seller, wb)
    wb.close_supply(known.wb_supply_id)  # done=True на стороне WB

    result = sync_supplies(db, seller, wb)

    assert result == {"imported": 0, "updated": 1}
    assert db.scalar(select(Supply).where(Supply.wb_supply_id == broken_supply_id)) is None
    db.refresh(known)
    assert known.status == SupplyStatus.IN_DELIVERY


def test_supply_counters_and_group_filters(db, seller):
    open_supply = Supply(client_id=seller.id, status=SupplyStatus.OPEN)
    in_delivery_supply = Supply(client_id=seller.id, status=SupplyStatus.IN_DELIVERY)
    accepted_supply = Supply(client_id=seller.id, status=SupplyStatus.ACCEPTED)
    failed_supply = Supply(client_id=seller.id, status=SupplyStatus.FAILED)
    db.add_all([open_supply, in_delivery_supply, accepted_supply, failed_supply])
    db.commit()

    counters = get_supply_counters(db, client_id=seller.id)
    assert counters == {"assembly": 1, "in_delivery": 1, "accepted": 1, "other": 1}

    assert {s.id for s in list_supplies(db, client_id=seller.id, group="assembly")} == {open_supply.id}
    assert {s.id for s in list_supplies(db, client_id=seller.id, group="in_delivery")} == {in_delivery_supply.id}
    assert {s.id for s in list_supplies(db, client_id=seller.id, group="accepted")} == {accepted_supply.id}
    assert {s.id for s in list_supplies(db, client_id=seller.id, group="other")} == {failed_supply.id}
