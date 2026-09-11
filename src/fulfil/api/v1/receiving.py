from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import NotFoundError
from fulfil.schemas.receiving import PlaceRequest, ReceiptLineHistoryOut, ReceiptOut
from fulfil.services import clients as clients_service
from fulfil.services import idempotency
from fulfil.services import receiving as receiving_service
from fulfil.services.scan import resolve_scan
from fulfil.services.storage import resolve_location

router = APIRouter(prefix="/receiving", tags=["receiving"], dependencies=[Depends(get_current_user)])

_ENDPOINT = "receiving.place"


@router.get("/current", response_model=ReceiptOut)
def current_receipt(client_id: int, db: Session = Depends(get_db)):
    client = clients_service.get_live_client_or_404(db, client_id)
    return receiving_service.get_or_create_open_receipt(db, client)


@router.get("/history", response_model=list[ReceiptLineHistoryOut])
def history(
    limit: int = 50, offset: int = 0, client_id: int | None = None, db: Session = Depends(get_db)
) -> list[dict]:
    """Постоянная история приёмок (newest-first) — переживает F5 и архивацию товара."""
    return receiving_service.list_receipt_lines(db, limit=limit, offset=offset, client_id=client_id)


@router.post("/place")
def place(
    body: PlaceRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    x_idempotency_key: str | None = Header(default=None),
) -> dict:
    """Скан баркода товара -> количество -> скан ячейки. Ячейка принимается ЛЮБОЙ
    отсканированной строкой (id / CELL-xxx / адрес) — см. resolve_location.

    clientId не передан — товар ищется среди всех клиентов; если баркод есть у
    нескольких сразу, resolve_scan бросает 409 со списком кандидатов (Этап 1, п.1.4).

    X-Idempotency-Key — повтор с тем же ключом (обрыв сети, двойной скан) не удваивает
    приёмку, а возвращает тот же ответ (FEATURES-PLAN.md, дефект №5)."""
    cached = idempotency.begin_idempotent(db, _ENDPOINT, x_idempotency_key)
    if cached:
        return cached

    kind, product = resolve_scan(db, body.product_barcode, client_id=body.client_id)
    if kind != "product":
        raise NotFoundError(
            f"Товар с баркодом «{body.product_barcode}» не найден в справочнике."
        )
    cell = resolve_location(db, body.cell_code)

    client = clients_service.get_client_or_404(db, product.client_id)
    receipt = receiving_service.get_or_create_open_receipt(db, client)
    line = receiving_service.add_receipt_line(db, receipt, product, cell, body.qty, actor=user.get("sub", "system"))

    result = {
        "ok": True,
        "lineId": line.id,
        "receiptNumber": receipt.number,
        "clientId": client.id,
        "clientName": client.name,
        "cellAddress": cell.address,
        "productName": product.name,
        "barcode": product.barcode,
        "qty": line.qty,
        "actor": line.actor,
        "createdAt": line.created_at.isoformat() if line.created_at else None,
    }
    idempotency.complete_idempotent(db, _ENDPOINT, x_idempotency_key, result)
    return result


@router.post("/finish", response_model=ReceiptOut)
def finish(client_id: int, db: Session = Depends(get_db)):
    client = clients_service.get_live_client_or_404(db, client_id)
    receipt = receiving_service.get_or_create_open_receipt(db, client)
    return receiving_service.finish_receipt(db, receipt)
