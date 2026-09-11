"""Мок WB API — фикстуры для разработки и тестов без боевого токена.

Этап 1: одна карточка с несколькими размерами (чтобы тесты и dev-стенд ловили
разворот sizes[] в отдельные товары), и баркоды/номера заказов зависят от client_id —
два клиента в мок-режиме не видят одинаковых данных, это и проверяют test_clients.py."""

import base64
import uuid

from fulfil.integrations.wb.base import WbCardsPage, WbCursor, WbOrder, WbSticker


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


class WBMockClient:
    def __init__(self, client_id: int = 0) -> None:
        self._client_id = client_id
        self._orders: list[dict] = []
        self._supplies: dict[str, dict] = {}
        self._cards_served = False

    def ping(self) -> dict:
        return {
            "marketplace": {"ok": True, "error": None},
            "content": {"ok": True, "error": None},
        }

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

    def update_fbs_stock(self, warehouse_id: str, barcode: str, qty: int) -> dict:
        return {"ok": True, "warehouseId": warehouse_id, "barcode": barcode, "qty": qty}

    def get_new_orders(self) -> list[WbOrder]:
        if self._orders:
            return []  # мок отдаёт фикстуру один раз — второй sync не дублирует
        order: WbOrder = {
            "orderId": f"WB-ORDER-DEMO-{self._client_id}",
            "supplyId": None,
            "createdAt": "2026-08-28T10:00:00+00:00",
            "deadlineAt": "2026-08-29T10:00:00+00:00",
            "items": [{"barcode": _fixture_cards(self._client_id)[0]["barcode"], "qty": 1}],
        }
        self._orders.append(order)
        return [order]

    def get_order_sticker(self, order_id: str) -> WbSticker:
        return {"type": "png", "data": _PNG_1PX}

    def send_marking_codes(self, order_id: str, codes: list[str]) -> dict:
        return {"ok": True, "accepted": len(codes)}

    def create_supply(self) -> str:
        supply_id = f"WB-SUPPLY-{uuid.uuid4().hex[:8]}"
        self._supplies[supply_id] = {"status": "open", "orders": []}
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
