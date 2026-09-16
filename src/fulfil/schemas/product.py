import datetime as dt

from fulfil.schemas.common import CamelModel


class ProductCellStockOut(CamelModel):
    cell_id: int
    cell_address: str
    qty: int


class ProductOut(CamelModel):
    id: int
    client_id: int
    client_name: str | None = None
    client_archived: bool = False
    barcode: str
    name: str
    brand: str | None = None
    size: str | None = None
    color: str | None = None
    vendor_code: str | None = None
    image_url: str | None = None
    wb_nm_id: int | None = None
    wb_chrt_id: int | None = None
    manual_fields: list[str] = []
    archived_at: dt.datetime | None = None
    # Остаток в карточке товара (Этап 6, п.6.2) — заполняется только списком
    # GET /products (см. api/v1/products.list_products): stockTotal — «на полках»
    # (services.stock.list_stock_summaries, без N+1), wbFbsAmount — уже существующий
    # кэш Product.wb_fbs_amount, cells — разбивка по ячейкам (до трёх показывает
    # фронт, остальное сворачивает в «+N»).
    stock_total: int = 0
    wb_fbs_amount: int = 0
    cells: list[ProductCellStockOut] = []


class ProductCreateRequest(CamelModel):
    client_id: int
    barcode: str
    name: str
    brand: str | None = None
    size: str | None = None
    color: str | None = None
    vendor_code: str | None = None
    image_url: str | None = None


class ProductUpdateRequest(CamelModel):
    name: str | None = None
    brand: str | None = None
    size: str | None = None
    color: str | None = None
    vendor_code: str | None = None
    image_url: str | None = None
    barcode: str | None = None


class RevertManualFieldRequest(CamelModel):
    field: str
