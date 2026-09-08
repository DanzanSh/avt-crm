import datetime as dt

from fulfil.schemas.common import CamelModel


class PlaceRequest(CamelModel):
    product_barcode: str
    cell_code: str  # то, что отсканировали: id / CELL-xxx / адрес — сервер сам разберёт
    qty: int


class ReceiptLineHistoryOut(CamelModel):
    id: int
    receipt_number: str
    product_name: str
    barcode: str
    cell_address: str
    qty: int
    actor: str
    created_at: dt.datetime


class ReceiptOut(CamelModel):
    id: int
    number: str
    status: str
