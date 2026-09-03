from fulfil.schemas.common import CamelModel


class PlaceRequest(CamelModel):
    product_barcode: str
    cell_code: str  # то, что отсканировали: id / CELL-xxx / адрес — сервер сам разберёт
    qty: int


class ReceiptLineOut(CamelModel):
    id: int
    product_id: int
    cell_id: int
    qty: int


class ReceiptOut(CamelModel):
    id: int
    number: str
    status: str
