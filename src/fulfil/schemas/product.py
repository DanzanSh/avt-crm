import datetime as dt

from fulfil.schemas.common import CamelModel


class ProductOut(CamelModel):
    id: int
    client_id: int
    client_name: str | None = None
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
