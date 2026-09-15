from pydantic import Field

from fulfil.schemas.common import CamelModel


class TransferFbsRequest(CamelModel):
    # gt=0 (P2-10): отрицательное количество проходило проверку availableToTransfer
    # (отрицательное <= доступному) и в итоге УМЕНЬШАЛО остаток на WB вместо передачи.
    qty: int = Field(gt=0)
    idempotency_key: str


class AdjustStockRequest(CamelModel):
    cell_id: int
    new_qty: int = Field(ge=0)
    expected_qty: int = Field(ge=0)
    comment: str | None = None


class WriteOffStockRequest(CamelModel):
    cell_id: int
    expected_qty: int = Field(ge=0)
    comment: str


class MoveStockRequest(CamelModel):
    from_cell_id: int
    to_cell_code: str
    # gt=0 (P2-10): отрицательное количество проходило проверку "qty > current" и
    # переносило остаток в обратную сторону между ячейками.
    qty: int = Field(gt=0)
    expected_qty: int = Field(ge=0)
    comment: str | None = None
