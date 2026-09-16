import datetime as dt

from fulfil.schemas.common import CamelModel


class ClientOut(CamelModel):
    id: int
    name: str
    wb_warehouse_id: str | None = None
    wb_warehouse_name: str | None = None
    has_api_key: bool = False
    wb_token_expires_at: dt.datetime | None = None
    wb_token_expiring_soon: bool = False
    wb_token_expired: bool = False
    last_sync_at: dt.datetime | None = None
    last_sync_error: str | None = None
    archived_at: dt.datetime | None = None
    # Заполнены только у удалённых — а их отдаёт только хосту (GET /clients?state=deleted).
    deleted_at: dt.datetime | None = None
    deleted_by: str | None = None
    # Счётчики — только у GET /clients (list_clients_with_counters), 0 по умолчанию
    # у ручек создания/правки, где их не считали.
    sku_count: int = 0
    stock_qty: int = 0
    active_orders: int = 0


class ClientCreateRequest(CamelModel):
    name: str
    api_key: str | None = None
    wb_warehouse_id: str | None = None


class ClientUpdateRequest(CamelModel):
    name: str | None = None
    # Пусто/не передано = не менять (Этап 1, п.1.4) — поле только на запись,
    # API никогда не отдаёт его обратно.
    api_key: str | None = None
    wb_warehouse_id: str | None = None
    wb_warehouse_name: str | None = None


class SetWbWarehouseRequest(CamelModel):
    """Выбор УЖЕ существующего в WB склада — id и name берутся из строки
    GET /clients/{id}/wb-warehouses, а не вводятся руками (Этап 2, п.2.2)."""

    warehouse_id: str
    warehouse_name: str


class CreateWbWarehouseRequest(CamelModel):
    name: str
    office_id: int
