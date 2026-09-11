from fulfil.schemas.common import CamelModel


class OrderItemOut(CamelModel):
    id: int
    product_id: int
    barcode: str
    qty: int
    picked_qty: int
    status: str


class OrderOut(CamelModel):
    id: int
    client_id: int
    client_name: str | None = None
    wb_order_id: str
    status: str
    items: list[OrderItemOut] = []


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
