from fulfil.schemas.common import CamelModel


class TransferFbsRequest(CamelModel):
    qty: int
    idempotency_key: str


class AdjustStockRequest(CamelModel):
    cell_id: int
    new_qty: int
    expected_qty: int
    comment: str | None = None


class WriteOffStockRequest(CamelModel):
    cell_id: int
    expected_qty: int
    comment: str


class MoveStockRequest(CamelModel):
    from_cell_id: int
    to_cell_code: str
    qty: int
    expected_qty: int
    comment: str | None = None
