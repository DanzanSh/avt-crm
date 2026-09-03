from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import NotFoundError
from fulfil.integrations.wb import get_wb_client
from fulfil.models.fbs import Order, OrderItem, Supply
from fulfil.schemas.fbs import (
    CreateBoxesRequest,
    OrderOut,
    PickLineOut,
    ScanMarkRequest,
    SupplyOut,
)
from fulfil.services import marking as marking_service
from fulfil.services import orders as orders_service
from fulfil.services import picking as picking_service
from fulfil.services import supplies as supplies_service

router = APIRouter(prefix="/fbs", tags=["fbs"], dependencies=[Depends(get_current_user)])


# --- Заказы ---


@router.post("/orders/sync")
def sync_orders(db: Session = Depends(get_db)) -> dict:
    wb_client = get_wb_client()
    created = orders_service.sync_orders_from_wb(db, wb_client)
    return {"created": len(created)}


@router.get("/orders", response_model=list[OrderOut])
def list_orders(db: Session = Depends(get_db)) -> list[Order]:
    return list(db.scalars(select(Order).order_by(Order.id.desc())))


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, db: Session = Depends(get_db)) -> Order:
    return orders_service.get_order_or_404(db, order_id)


@router.post("/orders/{order_id}/take-to-work", response_model=OrderOut)
def take_to_work(order_id: int, db: Session = Depends(get_db)) -> Order:
    """Необратимо на стороне WB — фронт обязан показать предупреждение до вызова."""
    order = orders_service.get_order_or_404(db, order_id)
    wb_client = get_wb_client()
    return orders_service.take_to_work(db, order, wb_client)


@router.get("/orders/{order_id}/pick-list", response_model=list[PickLineOut])
def get_pick_list(order_id: int, db: Session = Depends(get_db)) -> list[dict]:
    from fulfil.models.product import Product
    from fulfil.models.storage import Cell

    order = orders_service.get_order_or_404(db, order_id)
    lines = picking_service.build_pick_list(db, order)
    out = []
    for line in lines:
        product = db.get(Product, line.product_id)
        cell = db.get(Cell, line.cell_id)
        out.append(
            {
                "id": line.id,
                "productId": line.product_id,
                "productName": product.name if product else "",
                "cellId": line.cell_id,
                "cellAddress": cell.address if cell else "",
                "qty": line.qty,
                "seq": line.seq,
                "pickedAt": line.picked_at.isoformat() if line.picked_at else None,
            }
        )
    return out


@router.get("/orders/{order_id}/sticker")
def get_sticker(order_id: int, db: Session = Depends(get_db)) -> dict:
    order = orders_service.get_order_or_404(db, order_id)
    wb_client = get_wb_client()
    return marking_service.fetch_order_sticker(db, order, wb_client)


@router.post("/orders/{order_id}/items/{item_id}/scan-mark")
def scan_mark(order_id: int, item_id: int, body: ScanMarkRequest, db: Session = Depends(get_db)) -> dict:
    item = db.scalar(
        select(OrderItem).where(OrderItem.id == item_id, OrderItem.order_id == order_id)
    )
    if item is None:
        raise NotFoundError("Позиция заказа не найдена.")
    mark = marking_service.scan_mark(db, item, body.code)
    return {"ok": True, "markId": mark.id}


@router.post("/orders/{order_id}/confirm-assembly", response_model=OrderOut)
def confirm_assembly(order_id: int, db: Session = Depends(get_db)) -> Order:
    """Списание остатка и передача марок в WB — одной транзакцией (см. services.marking)."""
    order = orders_service.get_order_or_404(db, order_id)
    wb_client = get_wb_client()
    return marking_service.confirm_assembly(db, order, wb_client)


# --- Поставки ---


@router.post("/supplies", response_model=SupplyOut)
def create_supply(db: Session = Depends(get_db)) -> Supply:
    wb_client = get_wb_client()
    return supplies_service.create_supply(db, wb_client)


@router.get("/supplies", response_model=list[SupplyOut])
def list_supplies(db: Session = Depends(get_db)) -> list[Supply]:
    return list(db.scalars(select(Supply).order_by(Supply.id.desc())))


def _get_supply(db: Session, supply_id: int) -> Supply:
    supply = db.get(Supply, supply_id)
    if supply is None:
        raise NotFoundError(f"Поставка #{supply_id} не найдена.")
    return supply


@router.post("/supplies/{supply_id}/orders/{order_id}")
def add_order_to_supply(supply_id: int, order_id: int, db: Session = Depends(get_db)) -> dict:
    supply = _get_supply(db, supply_id)
    order = orders_service.get_order_or_404(db, order_id)
    wb_client = get_wb_client()
    supplies_service.add_order_to_supply(db, supply, order, wb_client)
    return {"ok": True}


@router.post("/supplies/{supply_id}/close", response_model=SupplyOut)
def close_supply(supply_id: int, db: Session = Depends(get_db)) -> Supply:
    supply = _get_supply(db, supply_id)
    wb_client = get_wb_client()
    return supplies_service.close_supply(db, supply, wb_client)


@router.get("/supplies/{supply_id}/qr")
def get_supply_qr(supply_id: int, db: Session = Depends(get_db)) -> dict:
    supply = _get_supply(db, supply_id)
    wb_client = get_wb_client()
    return supplies_service.get_supply_qr(db, supply, wb_client)


@router.post("/supplies/{supply_id}/boxes")
def create_boxes(supply_id: int, body: CreateBoxesRequest, db: Session = Depends(get_db)) -> dict:
    supply = _get_supply(db, supply_id)
    wb_client = get_wb_client()
    boxes = supplies_service.get_supply_boxes_qr(db, supply, body.amount, wb_client)
    return {"boxes": [{"id": b.id, "stickerData": b.sticker_data} for b in boxes]}
