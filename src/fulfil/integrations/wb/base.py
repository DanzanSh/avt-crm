"""Интерфейс интеграции с Wildberries Seller API.

Два переключаемых по WB_MODE слоя: WBHttpClient (боевой) и WBMockClient (фикстуры).
Тесты сервисов всегда идут через мок — контракты WB нестабильны и лимитированы
(риск №1 бизнес-плана), поэтому бизнес-логика не должна зависеть от реальной сети.
"""

from typing import Protocol, TypedDict


class WbProductCard(TypedDict):
    nmId: int
    imtId: int
    chrtId: int | None  # id размера карточки — Этап 1: товар = размер, а не карточка целиком
    vendorCode: str
    barcode: str
    name: str
    brand: str
    size: str
    color: str
    imageUrl: str


class WbCursor(TypedDict):
    updatedAt: str
    nmID: int
    total: int


class WbCardsPage(TypedDict):
    cards: list[WbProductCard]
    cursor: WbCursor | None  # None = ответ без курсора


class WbOrder(TypedDict):
    orderId: str
    supplyId: str | None
    createdAt: str  # ISO-8601 с TZ
    deadlineAt: str | None
    items: list[dict]  # [{barcode, qty}]


class WbSticker(TypedDict):
    type: str  # 'png' | 'svg'
    data: str  # base64


class WBClient(Protocol):
    def ping(self) -> dict:
        """Best-effort проверка подключения — GET /ping на каждом хосте API клиента.
        Возвращает {"marketplace": {"ok": bool, "error": str|None}, "content": {...}}."""
        ...

    def get_product_cards(self, cursor: WbCursor | None = None) -> WbCardsPage: ...

    def update_fbs_stock(self, warehouse_id: str, barcode: str, qty: int) -> dict: ...

    def get_new_orders(self) -> list[WbOrder]: ...

    def get_order_sticker(self, order_id: str) -> WbSticker: ...

    def send_marking_codes(self, order_id: str, codes: list[str]) -> dict: ...

    def create_supply(self) -> str:
        """Возвращает wb_supply_id."""
        ...

    def add_order_to_supply(self, supply_id: str, order_id: str) -> dict: ...

    def close_supply(self, supply_id: str) -> dict: ...

    def get_supply_qr(self, supply_id: str) -> WbSticker: ...

    def get_supply_boxes_qr(self, supply_id: str, amount: int) -> list[WbSticker]: ...
