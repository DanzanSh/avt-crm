"""Интерфейс интеграции с Wildberries Seller API.

Два переключаемых по WB_MODE слоя: WBHttpClient (боевой) и WBMockClient (фикстуры).
Тесты сервисов всегда идут через мок — контракты WB нестабильны и лимитированы
(риск №1 бизнес-плана), поэтому бизнес-логика не должна зависеть от реальной сети.
"""

import datetime as dt
from typing import Protocol, TypedDict


def parse_wb_datetime(value: str | None) -> dt.datetime | None:
    """ISO-8601 с 'Z' вместо смещения — формат дат в ответах WB. Общий парсер (P3):
    раньше был продублирован как `_parse_wb_dt` в services/orders.py и services/supplies.py."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class WbProductCard(TypedDict):
    nmId: int
    imtId: int
    chrtId: int | None  # id размера карточки — Этап 1: товар = размер, а не карточка целиком
    vendorCode: str
    barcode: str
    extraBarcodes: list[str]  # skus[1:] того же размера (P2-11) — см. Product.extra_barcodes
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
    warehouseId: str | None  # склад WB, куда пришёл заказ — фильтр "только наш склад" (Этап 3, п.4.2)
    nmId: int | None
    chrtId: int | None
    article: str | None
    createdAt: str  # ISO-8601 с TZ
    deadlineAt: str | None
    items: list[dict]  # [{barcode, qty}]


class WbOrderStatus(TypedDict):
    wbStatus: str | None
    supplierStatus: str | None


class WbOffice(TypedDict):
    """Пункт приёма WB — нужен только при создании нового склада (Этап 2, п.2.2):
    WB требует привязать склад к office_id."""

    id: int
    name: str
    address: str


class WbWarehouse(TypedDict):
    id: str
    name: str


class WbSticker(TypedDict):
    type: str  # 'png' | 'svg'
    data: str  # base64


class WbSupplyInfo(TypedDict):
    """Строка ответа GET /api/v3/supplies (Этап 4, п.4.2) — done/scanDt решают,
    какой статус получит наша Supply (см. services/supplies._status_from_wb)."""

    id: str
    name: str | None
    done: bool
    createdAt: str | None
    closedAt: str | None
    scanDt: str | None


class WbSuppliesPage(TypedDict):
    supplies: list[WbSupplyInfo]
    next: int | None  # курсор следующей страницы, None = последняя страница


class WBClient(Protocol):
    def ping(self) -> dict:
        """Best-effort проверка подключения — GET /ping на каждом хосте API клиента.
        Возвращает {"marketplace": {"ok": bool, "error": str|None}, "content": {...}}."""
        ...

    def get_product_cards(self, cursor: WbCursor | None = None) -> WbCardsPage: ...

    # --- Склад WB клиента (Этап 2, п.2.2/2.3) ---
    def list_offices(self) -> list[WbOffice]:
        """Пункты приёма продавца — нужны только для создания нового склада."""
        ...

    def list_warehouses(self) -> list[WbWarehouse]:
        """Склады FBS, уже существующие у продавца в WB."""
        ...

    def create_warehouse(self, name: str, office_id: int) -> WbWarehouse: ...

    def get_fbs_stocks(self, warehouse_id: str, barcodes: list[str]) -> dict[str, int]:
        """Текущий остаток на складе WB по каждому баркоду (0, если WB его не знает).
        Читается ПЕРЕД set_fbs_stocks — WB задаёт остаток, а не прибавляет к нему."""
        ...

    def set_fbs_stocks(self, warehouse_id: str, amounts: dict[str, int]) -> dict:
        """Пакетно задаёт остаток по каждому баркоду (WB ЗАДАЁТ значение целиком —
        вызывающая сторона обязана сама прибавить qty к уже известному остатку)."""
        ...

    def get_new_orders(self) -> list[WbOrder]: ...

    def get_order_statuses(self, order_ids: list[str]) -> dict[str, WbOrderStatus]:
        """Статусы НАШИХ заказов (Этап 3, п.3.1) — POST /api/v3/orders/status.
        Опрашивается отдельно от get_new_orders(): иначе отмена покупателем,
        «в доставке», «принята» до нас не доходят."""
        ...

    def get_order_sticker(self, order_id: str) -> WbSticker: ...

    def send_marking_codes(self, order_id: str, codes: list[str]) -> dict: ...

    def create_supply(self, name: str | None = None) -> str:
        """Возвращает wb_supply_id."""
        ...

    def add_order_to_supply(self, supply_id: str, order_id: str) -> dict: ...

    def close_supply(self, supply_id: str) -> dict: ...

    def list_supplies(self, next_cursor: int | None = None, limit: int = 1000) -> WbSuppliesPage:
        """Постраничный список ВСЕХ поставок продавца (Этап 4, п.4.2) —
        GET /api/v3/supplies, курсор `next` из предыдущего ответа."""
        ...

    def get_supply_orders(self, supply_id: str) -> list[str]:
        """orderId'шники, входящие в поставку — синхронизация использует это,
        чтобы решить, "наша" ли чужая поставка (создана не через Fulfil, но
        содержит наш заказ)."""
        ...

    def get_supply_qr(self, supply_id: str) -> WbSticker: ...

    def get_supply_boxes_qr(self, supply_id: str, amount: int) -> list[WbSticker]: ...
