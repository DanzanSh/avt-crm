"""Мок WB API — фикстуры для разработки и тестов без боевого токена."""

import base64
import uuid

from fulfil.integrations.wb.base import WbCardsPage, WbCursor, WbOrder, WbSticker

_FIXTURE_CARDS: list[dict] = [
    {
        "nmId": 100001,
        "imtId": 900001,
        "vendorCode": "TSHIRT-WHITE-M",
        "barcode": "2000000000017",
        "name": "Майка белая",
        "brand": "Demo Brand",
        "size": "M",
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
    def __init__(self) -> None:
        self._orders: list[dict] = []
        self._supplies: dict[str, dict] = {}

    def get_product_cards(self, cursor: WbCursor | None = None) -> WbCardsPage:
        # Инкрементальный синк: с сохранённым курсором мок отдаёт пусто (total 0 < limit),
        # так повторный клик «Синхронизировать» возвращает imported: 0.
        if cursor:
            return {"cards": [], "cursor": {**cursor, "total": 0}}
        # Первая выгрузка: одна страница (total 1 < limit 100 → цикл завершается).
        return {
            "cards": _FIXTURE_CARDS,
            "cursor": {"updatedAt": "2026-01-01T00:00:00Z", "nmID": 100001, "total": 1},
        }

    def update_fbs_stock(self, warehouse_id: str, barcode: str, qty: int) -> dict:
        return {"ok": True, "warehouseId": warehouse_id, "barcode": barcode, "qty": qty}

    def get_new_orders(self) -> list[WbOrder]:
        if self._orders:
            return []  # мок отдаёт фикстуру один раз — второй sync не дублирует
        order: WbOrder = {
            "orderId": "WB-ORDER-DEMO-1",
            "supplyId": None,
            "createdAt": "2026-08-28T10:00:00+00:00",
            "deadlineAt": "2026-08-29T10:00:00+00:00",
            "items": [{"barcode": "2000000000017", "qty": 1}],
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
