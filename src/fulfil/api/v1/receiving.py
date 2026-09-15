import io

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import NotFoundError
from fulfil.exports.receipt_xlsx import build_discrepancy_report_xlsx
from fulfil.models.receiving import Receipt
from fulfil.schemas.receiving import (
    AcceptManualRequest,
    CreateReceiptRequest,
    ImportPlanResultOut,
    PlanLineOut,
    ProgressOut,
    ReceiptCountersOut,
    ReceiptLineHistoryOut,
    ReceiptOut,
    ScanPlaceRequest,
    SetPlanRequest,
    UpdateReceiptRequest,
)
from fulfil.services import clients as clients_service
from fulfil.services import idempotency
from fulfil.services import receiving as receiving_service
from fulfil.services.scan import resolve_scan
from fulfil.services.storage import resolve_location

router = APIRouter(prefix="/receiving", tags=["receiving"], dependencies=[Depends(get_current_user)])

_SCAN_ENDPOINT = "receiving.scan-place"
_ACCEPT_ENDPOINT = "receiving.accept-manual"


def _actor(user: dict) -> str:
    return user.get("sub", "system")


def _get_receipt(db: Session, receipt_id: int) -> Receipt:
    return receiving_service.get_receipt_or_404(db, receipt_id)


@router.get("", response_model=list[ReceiptOut])
def list_receipts(client_id: int | None = None, group: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    return receiving_service.list_receipts(db, client_id=client_id, group=group)


@router.get("/counters", response_model=ReceiptCountersOut)
def receipt_counters(client_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    return receiving_service.get_receipt_counters(db, client_id=client_id)


@router.get("/template.xlsx")
def template_xlsx(client_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    client = clients_service.get_live_client_or_404(db, client_id)
    xlsx_bytes = receiving_service.build_template_for_client(db, client)
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=receiving-template.xlsx"},
    )


# Постоянная история приёмок (newest-first) — переживает F5 и архивацию товара.
# Объявлено ДО "/{receipt_id}": иначе FastAPI разберёт "history" как receipt_id.
@router.get("/history", response_model=list[ReceiptLineHistoryOut])
def history(
    limit: int = Query(default=50, ge=1, le=500),  # P3: раньше без верхней границы
    offset: int = 0, client_id: int | None = None, receipt_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    return receiving_service.list_receipt_lines(
        db, limit=limit, offset=offset, client_id=client_id, receipt_id=receipt_id
    )


@router.post("", response_model=ReceiptOut)
def create_receipt(
    body: CreateReceiptRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> dict:
    client = clients_service.get_live_client_or_404(db, body.client_id)
    receipt = receiving_service.create_receipt(
        db, client, expected_date=body.expected_date, comment=body.comment, actor=_actor(user)
    )
    return receiving_service.receipt_out(db, receipt)


@router.get("/{receipt_id}", response_model=ReceiptOut)
def get_receipt(receipt_id: int, db: Session = Depends(get_db)) -> dict:
    receipt = _get_receipt(db, receipt_id)
    return receiving_service.receipt_out(db, receipt)


@router.patch("/{receipt_id}", response_model=ReceiptOut)
def update_receipt(receipt_id: int, body: UpdateReceiptRequest, db: Session = Depends(get_db)) -> dict:
    receipt = _get_receipt(db, receipt_id)
    receipt = receiving_service.update_receipt(
        db, receipt, expected_date=body.expected_date, comment=body.comment
    )
    return receiving_service.receipt_out(db, receipt)


@router.get("/{receipt_id}/plan", response_model=list[PlanLineOut])
def get_plan(receipt_id: int, db: Session = Depends(get_db)) -> list[dict]:
    receipt = _get_receipt(db, receipt_id)
    return receiving_service.plan_with_suggestions(db, receipt)


@router.post("/{receipt_id}/plan", response_model=list[PlanLineOut])
def set_plan(receipt_id: int, body: SetPlanRequest, db: Session = Depends(get_db)) -> list[dict]:
    receipt = _get_receipt(db, receipt_id)
    rows = [(line.barcode, line.qty) for line in body.lines]
    receiving_service.set_plan_lines(db, receipt, rows)
    return receiving_service.plan_with_suggestions(db, receipt)


@router.post("/{receipt_id}/plan/import", response_model=ImportPlanResultOut)
async def import_plan(receipt_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    receipt = _get_receipt(db, receipt_id)
    content = await file.read()
    return receiving_service.import_plan_xlsx(db, receipt, content)


@router.post("/{receipt_id}/start", response_model=ReceiptOut)
def start_receipt(receipt_id: int, db: Session = Depends(get_db)) -> dict:
    receipt = _get_receipt(db, receipt_id)
    receipt = receiving_service.start_receipt(db, receipt)
    return receiving_service.receipt_out(db, receipt)


@router.get("/{receipt_id}/progress", response_model=ProgressOut)
def progress(receipt_id: int, db: Session = Depends(get_db)) -> dict:
    receipt = _get_receipt(db, receipt_id)
    return receiving_service.receipt_progress(db, receipt)


@router.post("/{receipt_id}/scan-place")
def scan_place(
    receipt_id: int,
    body: ScanPlaceRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    x_idempotency_key: str | None = Header(default=None),
) -> dict:
    """Скан баркода товара -> количество -> скан ячейки, внутри конкретной приёмки.
    Товар ищется только среди товаров клиента этой приёмки (resolve_scan client_id).

    X-Idempotency-Key — повтор с тем же ключом (обрыв сети, двойной скан) не удваивает
    приёмку, а возвращает тот же ответ."""
    cached = idempotency.begin_idempotent(db, _SCAN_ENDPOINT, x_idempotency_key)
    if cached is not None:
        return cached

    receipt = _get_receipt(db, receipt_id)
    kind, product = resolve_scan(db, body.product_barcode, client_id=receipt.client_id)
    if kind != "product":
        raise NotFoundError(f"Товар с баркодом «{body.product_barcode}» не найден в справочнике.")
    cell = resolve_location(db, body.cell_code)

    line = receiving_service.add_receipt_line(
        db, receipt, product, cell, body.qty, actor=_actor(user)
    )

    result = {
        "ok": True,
        "lineId": line.id,
        "receiptNumber": receipt.number,
        "cellAddress": cell.address,
        "productName": product.name,
        "barcode": product.barcode,
        "qty": line.qty,
        "actor": line.actor,
        "createdAt": line.created_at.isoformat() if line.created_at else None,
    }
    idempotency.complete_idempotent(db, _SCAN_ENDPOINT, x_idempotency_key, result)
    return result


@router.post("/{receipt_id}/accept-manual", response_model=ProgressOut)
def accept_manual(
    receipt_id: int,
    body: AcceptManualRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    x_idempotency_key: str | None = Header(default=None),
) -> dict:
    """Пачка строк одной транзакцией — см. services.receiving.accept_manual: если
    строка упала, откатывается вся пачка, ошибка указывает на неё."""
    cached = idempotency.begin_idempotent(db, _ACCEPT_ENDPOINT, x_idempotency_key)
    if cached is not None:
        return cached

    receipt = _get_receipt(db, receipt_id)
    rows = [{"productId": l.product_id, "qty": l.qty, "cellCode": l.cell_code} for l in body.lines]
    receiving_service.accept_manual(db, receipt, rows, actor=_actor(user))

    result = receiving_service.receipt_progress(db, receipt)
    idempotency.complete_idempotent(db, _ACCEPT_ENDPOINT, x_idempotency_key, result)
    return result


@router.post("/{receipt_id}/finish", response_model=ReceiptOut)
def finish_receipt(receipt_id: int, db: Session = Depends(get_db)) -> dict:
    receipt = _get_receipt(db, receipt_id)
    receipt = receiving_service.finish_receipt(db, receipt)
    return receiving_service.receipt_out(db, receipt)


@router.get("/{receipt_id}/report.xlsx")
def report_xlsx(receipt_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    receipt = _get_receipt(db, receipt_id)
    progress_data = receiving_service.receipt_progress(db, receipt)
    rows = []
    for line in progress_data["lines"]:
        diff = line["diff"]
        status = "не заявлено" if not line["planned"] else ("недостача" if diff < 0 else ("излишек" if diff > 0 else "совпало"))
        rows.append({**line, "expectedQty": line["expectedQty"], "acceptedQty": line["acceptedQty"], "status": status})
    xlsx_bytes = build_discrepancy_report_xlsx(
        [
            {
                "barcode": r["barcode"], "name": r["productName"], "size": r["size"], "color": r["color"],
                "expectedQty": r["expectedQty"], "acceptedQty": r["acceptedQty"], "diff": r["diff"], "status": r["status"],
            }
            for r in rows
        ]
    )
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=receipt-{receipt.number}-report.xlsx"},
    )
