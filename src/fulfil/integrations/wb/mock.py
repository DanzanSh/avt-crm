"""Мок WB API — фикстуры для разработки и тестов без боевого токена.

Этап 1: одна карточка с несколькими размерами (чтобы тесты и dev-стенд ловили
разворот sizes[] в отдельные товары), и баркоды/номера заказов зависят от client_id —
два клиента в мок-режиме не видят одинаковых данных, это и проверяют test_clients.py.

Этап 2: остатки FBS хранятся per-warehouse ({barcode: amount}) и PUT-семантика WB
(«задать», не «прибавить») воспроизведена честно — set_fbs_stocks() перезаписывает
значение целиком, поэтому тест «передать 5, потом 3» видит именно 8, только если
вызывающая сторона (services.stock.transfer_to_fbs) сама прочитала текущее значение
через get_fbs_stocks() и прибавила qty.

Этап 3: заказ по умолчанию приходит с warehouseId "WH-{client_id}" — тест должен
явно выставить client.wb_warehouse_id в то же значение, чтобы синхронизация его
приняла (иначе он "с чужого склада" и будет отфильтрован, как и положено).
add_order()/cancel_order() — тестовые хуки, а не часть протокола WBClient."""

import base64
import uuid

from fulfil.integrations.wb.base import (
    WbCardsPage,
    WbCursor,
    WbOffice,
    WbOrder,
    WbOrderStatus,
    WbSticker,
    WbWarehouse,
)


def _fixture_cards(client_id: int) -> list[dict]:
    # Баркоды валидны по validate_gtin (13 цифр). client_id зашит в середину номера,
    # чтобы у разных клиентов гарантированно не совпадали — реальная многоарендность
    # в моке, а не общая фикстура на всех.
    suffix = f"{client_id:03d}"
    return [
        {
            "nmId": 100001,
            "imtId": 900001,
            "chrtId": 800001,
            "vendorCode": "TSHIRT-WHITE",
            "barcode": f"20000{suffix}0017",
            "name": "Майка белая",
            "brand": "Demo Brand",
            "size": "M",
            "color": "белый",
            "imageUrl": "",
        },
        {
            "nmId": 100001,
            "imtId": 900001,
            "chrtId": 800002,
            "vendorCode": "TSHIRT-WHITE",
            "barcode": f"20000{suffix}0024",
            "name": "Майка белая",
            "brand": "Demo Brand",
            "size": "L",
            "color": "белый",
            "imageUrl": "",
        },
    ]


_PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100ffff03000006"
        "0005570d8e0e0000000049454e44ae426082"
    )
).decode()


def _default_order(client_id: int) -> WbOrder:
    barcode = _fixture_cards(client_id)[0]["barcode"]
    return {
        "orderId": f"WB-ORDER-DEMO-{client_id}",
        "supplyId": None,
        "warehouseId": f"WH-{client_id}",
        "nmId": 100001,
        "chrtId": 800001,
        "article": "TSHIRT-WHITE",
        "createdAt": "2026-08-28T10:00:00+00:00",
        "deadlineAt": "2026-08-29T10:00:00+00:00",
        "items": [{"barcode": barcode, "qty": 1}],
    }


class WBMockClient:
    def __init__(self, client_id: int = 0) -> None:
        self._client_id = client_id
        # Очередь "новых" заказов — get_new_orders() отдаёт и очищает её целиком,
        # как будто WB уведомил о них один раз (Этап 3, п.3.1).
        self._pending_orders: list[dict] = [_default_order(client_id)]
        self._order_statuses: dict[str, WbOrderStatus] = {}
        self._supplies: dict[str, dict] = {}
        self._cards_served = False
        # Склад WB клиента (Этап 2, п.2.2/2.3) — фикстура двух готовых пунктов приёма
        # и пустого списка складов, чтобы тест мог проверить и выбор существующего,
        # и создание нового. _stocks — per-warehouse {barcode: amount}, имитирует
        # реальное поведение WB: PUT задаёт значение целиком, а не прибавляет.
        self._offices: list[WbOffice] = [
            {"id": 1, "name": "Коледино", "address": "Московская обл., Коледино"},
            {"id": 2, "name": "Электросталь", "address": "Московская обл., Электросталь"},
        ]
        self._warehouses: list[WbWarehouse] = []
        self._stocks: dict[str, dict[str, int]] = {}

    def ping(self) -> dict:
        return {
            "marketplace": {"ok": True, "error": None},
            "content": {"ok": True, "error": None},
        }

    # --- Склад WB клиента ---
    def list_offices(self) -> list[WbOffice]:
        return list(self._offices)

    def list_warehouses(self) -> list[WbWarehouse]:
        return list(self._warehouses)

    def create_warehouse(self, name: str, office_id: int) -> WbWarehouse:
        warehouse: WbWarehouse = {"id": f"WH-{self._client_id}-{len(self._warehouses) + 1}", "name": name}
        self._warehouses.append(warehouse)
        return warehouse

    def get_product_cards(self, cursor: WbCursor | None = None) -> WbCardsPage:
        # Инкрементальный синк: с сохранённым курсором мок отдаёт пусто (total 0 < limit),
        # так повторный клик «Синхронизировать» возвращает imported: 0.
        if cursor:
            return {"cards": [], "cursor": {**cursor, "total": 0}}
        # Первая выгрузка: одна страница (total 1 < limit 100 → цикл завершается).
        # total — число КАРТОЧЕК у WB (одна карточка с двумя размерами), не число
        # получившихся товаров — это не меняется относительно Этапа 0.
        return {
            "cards": _fixture_cards(self._client_id),
            "cursor": {"updatedAt": "2026-01-01T00:00:00Z", "nmID": 100001, "total": 1},
        }

    def get_fbs_stocks(self, warehouse_id: str, barcodes: list[str]) -> dict[str, int]:
        stocks = self._stocks.get(warehouse_id, {})
        return {b: stocks.get(b, 0) for b in barcodes}

    def set_fbs_stocks(self, warehouse_id: str, amounts: dict[str, int]) -> dict:
        self._stocks.setdefault(warehouse_id, {}).update(amounts)
        return {"ok": True}

    def get_new_orders(self) -> list[WbOrder]:
        orders, self._pending_orders = self._pending_orders, []
        return orders

    def add_order(self, **overrides) -> WbOrder:
        """Тестовый хук — ставит в очередь ещё один заказ поверх дефолтного
        (Этап 3, п.3.1): например, add_order(warehouseId="WH-OTHER") — заказ
        с чужого склада продавца, который синхронизация должна отфильтровать."""
        order: WbOrder = {**_default_order(self._client_id), **overrides}
        if "orderId" not in overrides:
            order["orderId"] = f"WB-ORDER-{uuid.uuid4().hex[:8]}"
        self._pending_orders.append(order)
        return order

    def get_order_statuses(self, order_ids: list[str]) -> dict[str, WbOrderStatus]:
        default: WbOrderStatus = {"wbStatus": "sorted", "supplierStatus": "confirm"}
        return {oid: self._order_statuses.get(oid, default) for oid in order_ids}

    def cancel_order(self, order_id: str) -> None:
        """Тестовый хук — эмулирует отмену покупателем (Этап 3, п.3.1): следующий
        refresh_order_statuses() увидит wbStatus canceled_by_client."""
        self._order_statuses[order_id] = {"wbStatus": "canceled_by_client", "supplierStatus": "cancel"}

    def get_order_sticker(self, order_id: str) -> WbSticker:
        return {"type": "png", "data": _PNG_1PX}

    def send_marking_codes(self, order_id: str, codes: list[str]) -> dict:
        return {"ok": True, "accepted": len(codes)}

    def create_supply(self, name: str | None = None) -> str:
        supply_id = f"WB-SUPPLY-{uuid.uuid4().hex[:8]}"
        self._supplies[supply_id] = {"status": "open", "orders": [], "name": name}
        return supply_id

    def add_order_to_supply(self, supply_id: str, order_id: str) -> dict:
        self._supplies.setdefault(supply_id, {"status": "open", "orders": []})
        self._supplies[supply_id]["orders"].append(order_id)
        return {"ok": True}

    def close_supply(self, supply_id: str) -> dict:
        self._supplies.setdefault(supply_id, {"status": "open", "orders": []})
        self._supplies[supply_id]["status"] = "closed"
        return {"ok": True, "status": "closed"}

    def get_supply_qr(self, supply_id: str) -> WbSticker:
        return {"type": "png", "data": _PNG_1PX}

    def get_supply_boxes_qr(self, supply_id: str, amount: int) -> list[WbSticker]:
        return [{"type": "png", "data": _PNG_1PX} for _ in range(amount)]
