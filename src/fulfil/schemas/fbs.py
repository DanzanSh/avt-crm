import datetime as dt

from fulfil.schemas.common import CamelModel


class OrderItemOut(CamelModel):
    id: int
    product_id: int | None = None
    barcode: str
    qty: int
    picked_qty: int
    status: str
    product_name: str | None = None
    product_size: str | None = None
    product_image_url: str | None = None


class OrderOut(CamelModel):
    id: int
    client_id: int
    client_name: str | None = None
    wb_order_id: str
    wb_supply_id: str | None = None
    wb_warehouse_id: str | None = None
    status: str
    problem: str | None = None
    wb_status: str | None = None
    supplier_status: str | None = None
    created_at_wb: dt.datetime | None = None
    deadline_at: dt.datetime | None = None
    items: list[OrderItemOut] = []


class OrderCountersOut(CamelModel):
    new: int
    assembly: int
    packed: int
    problems: int


class TakeToWorkBulkRequest(CamelModel):
    order_ids: list[int]


class TakeToWorkBulkResultOut(CamelModel):
    order_id: int
    ok: bool
    error: str | None = None


class ScanMarkRequest(CamelModel):
    code: str


class PickLineOut(CamelModel):
    id: int
    product_id: int
    product_name: str
    cell_id: int
    cell_address: str
    qty: int
    seq: int
    picked_at: str | None = None


class CreateBoxesRequest(CamelModel):
    amount: int


class CreateSupplyRequest(CamelModel):
    client_id: int


class SupplyOut(CamelModel):
    id: int
    client_id: int
    client_name: str | None = None
    wb_supply_id: str | None
    status: str
