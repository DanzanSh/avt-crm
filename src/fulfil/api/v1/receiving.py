from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import NotFoundError
from fulfil.models.product import Product
from fulfil.schemas.receiving import PlaceRequest, ReceiptOut
from fulfil.services import idempotency
from fulfil.services import receiving as receiving_service
from fulfil.services.storage import resolve_location

router = APIRouter(prefix="/receiving", tags=["receiving"], dependencies=[Depends(get_current_user)])

_ENDPOINT = "receiving.place"


@router.get("/current", response_model=ReceiptOut)
def current_receipt(db: Session = Depends(get_db)):
    return receiving_service.get_or_create_open_receipt(db)


@router.post("/place")
def place(
    body: PlaceRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    x_idempotency_key: str | None = Header(default=None),
) -> dict:
    """Скан баркода товара -> количество -> скан ячейки. Ячейка принимается ЛЮБОЙ
    отсканированной строкой (id / CELL-xxx / адрес) — см. resolve_location.

    X-Idempotency-Key — повтор с тем же ключом (обрыв сети, двойной скан) не удваивает
    приёмку, а возвращает тот же ответ (FEATURES-PLAN.md, дефект №5)."""
    cached = idempotency.begin_idempotent(db, _ENDPOINT, x_idempotency_key)
    if cached:
        return cached

    product = db.scalar(select(Product).where(Product.barcode == body.product_barcode, Product.archived_at.is_(None)))
    if product is None:
        raise NotFoundError(
            f"Товар с баркодом «{body.product_barcode}» не найден в справочнике."
        )
    cell = resolve_location(db, body.cell_code)

    receipt = receiving_service.get_or_create_open_receipt(db)
    line = receiving_service.add_receipt_line(db, receipt, product, cell, body.qty, actor=user.get("sub", "system"))

    result = {
        "ok": True,
        "cellAddress": cell.address,
        "productName": product.name,
        "qty": line.qty,
    }
    idempotency.complete_idempotent(db, _ENDPOINT, x_idempotency_key, result)
    return result


@router.post("/finish", response_model=ReceiptOut)
def finish(db: Session = Depends(get_db)):
    receipt = receiving_service.get_or_create_open_receipt(db)
    return receiving_service.finish_receipt(db, receipt)
