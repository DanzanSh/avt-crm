import datetime as dt

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import AppError, NotFoundError
from fulfil.integrations.wb import get_wb_client
from fulfil.models.product import Product
from fulfil.schemas.stock import (
    AdjustStockRequest,
    MoveStockRequest,
    TransferFbsRequest,
    WriteOffStockRequest,
)
from fulfil.services import clients as clients_service
from fulfil.services import idempotency
from fulfil.services import stock as stock_service
from fulfil.services.storage import resolve_location

router = APIRouter(prefix="/stock", tags=["stock"], dependencies=[Depends(get_current_user)])


def _actor(user: dict) -> str:
    return user.get("sub", "system")


@router.get("")
def list_stock(client_id: int | None = None, db: Session = Depends(get_db)) -> list[dict]:
    """Три запроса на весь список, а не get_stock_summary()+get_stock_by_cell() в
    цикле по каждому товару (Этап 2, п.2.3: список «Остатки» был N+1)."""
    stmt = (
        select(Product)
        .options(selectinload(Product.client))
        .where(Product.archived_at.is_(None))
        .order_by(Product.name)
    )
    if client_id is not None:
        stmt = stmt.where(Product.client_id == client_id)
    products = db.scalars(stmt).all()

    summaries = stock_service.list_stock_summaries(db, client_id=client_id)
    by_cell = stock_service.list_stock_by_cell(db, client_id=client_id)

    result = []
    for p in products:
        summary = summaries.get(p.id)
        if summary is None or summary["total"] == 0:
            continue
        result.append(
            {
                **summary,
                "productName": p.name,
                "barcode": p.barcode,
                "clientId": p.client_id,
                "clientName": p.client_name,
                "byCell": by_cell.get(p.id, []),
            }
        )
    return result


def _get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Товар #{product_id} не найден.")
    return product


def _get_cell(db: Session, cell_id: int):
    from fulfil.models.storage import Cell

    cell = db.scalar(select(Cell).where(Cell.id == cell_id, Cell.deleted_at.is_(None)))
    if cell is None:
        raise NotFoundError(f"Ячейка #{cell_id} не найдена.")
    return cell


@router.get("/{product_id}")
def get_product_stock(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = _get_product(db, product_id)
    return _product_agg(db, product)


@router.post("/{product_id}/transfer-fbs")
def transfer_fbs(product_id: int, body: TransferFbsRequest, db: Session = Depends(get_db)) -> dict:
    product = _get_product(db, product_id)
    wb_client = get_wb_client(product.client)
    transfer = stock_service.transfer_to_fbs(db, product, body.qty, body.idempotency_key, wb_client)
    return {
        "id": transfer.id,
        "status": transfer.status.value,
        "qty": transfer.qty,
        # Раньше при FAILED текст причины оседал только в fbs_transfers.wb_response —
        # на экране был просто «WB не принял передачу» без объяснения (Этап 2, п.2.3).
        "error": transfer.wb_response if transfer.status.value == "failed" else None,
    }


@router.post("/transfer-fbs-all")
def transfer_all_available_fbs(
    client_id: int, db: Session = Depends(get_db)
) -> list[dict]:
    """«Передать всё свободное по клиенту» (Этап 2, п.2.3) — по одной передаче на
    товар с ненулевым availableToTransfer. Ошибка одного товара (WbApiError) не
    останавливает остальные — так же, как /products/sync-from-wb по клиентам."""
    client = clients_service.get_live_client_or_404(db, client_id)
    wb_client = get_wb_client(client)
    summaries = stock_service.list_stock_summaries(db, client_id=client_id)

    results = []
    for product_id, summary in summaries.items():
        available = summary["availableToTransfer"]
        if available <= 0:
            continue
        product = _get_product(db, product_id)
        idempotency_key = f"transfer-all-{product_id}-{int(dt.datetime.now().timestamp())}"
        try:
            transfer = stock_service.transfer_to_fbs(db, product, available, idempotency_key, wb_client)
            results.append(
                {
                    "productId": product_id,
                    "productName": product.name,
                    "qty": available,
                    "status": transfer.status.value,
                    "error": transfer.wb_response if transfer.status.value == "failed" else None,
                }
            )
        except AppError as exc:
            db.rollback()
            results.append(
                {"productId": product_id, "productName": product.name, "qty": available, "status": "failed", "error": exc.detail}
            )
    return results


def _product_agg(db: Session, product: Product) -> dict:
    return {
        **stock_service.get_stock_summary(db, product),
        "productName": product.name,
        "barcode": product.barcode,
        "clientId": product.client_id,
        "clientName": product.client_name,
        "byCell": stock_service.get_stock_by_cell(db, product),
    }


@router.post("/{product_id}/adjust")
def adjust_stock(
    product_id: int, body: AdjustStockRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> dict:
    """Изменение количества товара в ячейке (Scope IN п.3). Возвращает весь агрегат по
    товару — экран остатков перерисовывается им, а не инкрементирует своё (DEV-PLAN.md)."""
    product = _get_product(db, product_id)
    cell = _get_cell(db, body.cell_id)
    stock_service.adjust_stock(
        db, product, cell, body.new_qty, body.expected_qty, actor=_actor(user), comment=body.comment,
    )
    return _product_agg(db, product)


@router.post("/{product_id}/write-off")
def write_off_stock(
    product_id: int, body: WriteOffStockRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> dict:
    """Удаление количества = списание в ноль (Scope IN п.3) — строка остатка не удаляется
    физически, это сломало бы проекцию stock_moves (DEV-PLAN.md, инвариант №1)."""
    product = _get_product(db, product_id)
    cell = _get_cell(db, body.cell_id)
    stock_service.write_off_stock(db, product, cell, body.expected_qty, actor=_actor(user), comment=body.comment)
    return _product_agg(db, product)


@router.post("/{product_id}/move")
def move_stock(
    product_id: int,
    body: MoveStockRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
    x_idempotency_key: str | None = Header(default=None),
) -> dict:
    """Перемещение между ячейками (Scope IN п.3) — целевая ячейка сканируется, не
    выбирается из списка (тот же сценарий, что на приёмке)."""
    endpoint = "stock.move"
    cached = idempotency.begin_idempotent(db, endpoint, x_idempotency_key)
    if cached:
        return cached

    product = _get_product(db, product_id)
    from_cell = _get_cell(db, body.from_cell_id)
    to_cell = resolve_location(db, body.to_cell_code)
    stock_service.move_stock(
        db, product, from_cell, to_cell, body.qty, body.expected_qty,
        actor=_actor(user), comment=body.comment,
    )
    result = _product_agg(db, product)
    idempotency.complete_idempotent(db, endpoint, x_idempotency_key, result)
    return result


@router.get("/moves/history")
def stock_moves(
    product_id: int | None = None,
    cell_id: int | None = None,
    actor: str | None = None,
    reason: str | None = None,
    from_date: dt.datetime | None = None,
    to_date: dt.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Журнал: кто/что/когда изменил (Scope IN п.3)."""
    return stock_service.get_moves(
        db, product_id=product_id, cell_id=cell_id, actor=actor, reason=reason,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )
