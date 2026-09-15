import datetime as dt

from pydantic import Field

from fulfil.schemas.common import CamelModel


class CreateReceiptRequest(CamelModel):
    client_id: int
    expected_date: dt.date | None = None
    comment: str | None = None


class UpdateReceiptRequest(CamelModel):
    expected_date: dt.date | None = None
    comment: str | None = None


class PlanLineIn(CamelModel):
    """Ручной ввод строки плана: товар ищут по баркоду (GET /products?search=),
    поэтому сюда приходит баркод, а не productId — так же, как скан."""

    barcode: str
    qty: int


class SetPlanRequest(CamelModel):
    lines: list[PlanLineIn]


class PlanLineOut(CamelModel):
    product_id: int
    product_name: str
    barcode: str
    size: str | None = None
    color: str | None = None
    expected_qty: int
    suggested_cell: str | None = None


class ImportPlanResultOut(CamelModel):
    ok: bool
    imported: int
    errors: list[str] = []


class ManualAcceptLine(CamelModel):
    product_id: int
    # gt=0 (P2-10): qty<=0 доходило до place_stock() и падало необработанным
    # ValueError-ом (500) вместо понятной ошибки 422 на входе.
    qty: int = Field(gt=0)
    cell_code: str


class AcceptManualRequest(CamelModel):
    lines: list[ManualAcceptLine]


class ScanPlaceRequest(CamelModel):
    product_barcode: str
    cell_code: str  # то, что отсканировали: id / CELL-xxx / адрес — сервер сам разберёт
    qty: int = Field(gt=0)  # P2-10: см. ManualAcceptLine.qty


class ProgressLineOut(CamelModel):
    product_id: int
    product_name: str
    barcode: str
    size: str | None = None
    color: str | None = None
    expected_qty: int
    accepted_qty: int
    diff: int
    planned: bool


class ProgressTotalsOut(CamelModel):
    expected_qty: int
    accepted_qty: int


class ProgressOut(CamelModel):
    lines: list[ProgressLineOut]
    totals: ProgressTotalsOut


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
    expected_date: dt.date | None = None
    comment: str | None = None
    created_by: str | None = None
    created_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    plan_lines_count: int = 0
    expected_total: int = 0
    accepted_total: int = 0


class ReceiptCountersOut(CamelModel):
    expected: int
    in_progress: int
    done: int
