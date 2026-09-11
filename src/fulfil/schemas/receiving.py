import datetime as dt

from fulfil.schemas.common import CamelModel


class PlaceRequest(CamelModel):
    product_barcode: str
    cell_code: str  # то, что отсканировали: id / CELL-xxx / адрес — сервер сам разберёт
    qty: int
    # Не передан и в фильтре выбраны «Все клиенты» — фронт обязан спросить клиента
    # до скана; если баркод неоднозначен между клиентами — 409 со списком кандидатов.
    client_id: int | None = None


class ReceiptLineHistoryOut(CamelModel):
    id: int
    receipt_number: str
    client_id: int | None = None
    client_name: str | None = None
    product_name: str
    barcode: str
    cell_address: str
    qty: int
    actor: str
    created_at: dt.datetime


class ReceiptOut(CamelModel):
    id: int
    client_id: int
    client_name: str | None = None
    number: str
    status: str
