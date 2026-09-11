"""Поставка: короба и QR — доступны только после закрытия (Scope IN п.12).

QR поставки WB выдаёт только после её закрытия — этот порядок закладываем в API,
а не притворяемся, что можем получить QR когда захотим (см. DEV-PLAN.md).
"""

from sqlalchemy.orm import Session

from fulfil.errors import AppError
from fulfil.integrations.wb.base import WBClient
from fulfil.models.client import Client
from fulfil.models.fbs import Order, Supply, SupplyBox, SupplyStatus


def create_supply(db: Session, client: Client, wb_client: WBClient) -> Supply:
    wb_supply_id = wb_client.create_supply()
    supply = Supply(client_id=client.id, wb_supply_id=wb_supply_id, status=SupplyStatus.OPEN)
    db.add(supply)
    db.commit()
    db.refresh(supply)
    return supply


def add_order_to_supply(db: Session, supply: Supply, order: Order, wb_client: WBClient) -> None:
    if supply.status != SupplyStatus.OPEN:
        raise AppError(
            f'Поставка в статусе "{supply.status.value}" — заказ добавить нельзя.',
            status_code=409,
            reason_code="wrong_status",
        )
    if order.client_id != supply.client_id:
        raise AppError(
            "Заказ и поставка принадлежат разным клиентам — добавить нельзя.",
            status_code=409,
            reason_code="client_mismatch",
        )
    wb_client.add_order_to_supply(supply.wb_supply_id, order.wb_order_id)
    order.supply_id = supply.id
    from fulfil.models.fbs import OrderStatus

    order.status = OrderStatus.IN_SUPPLY
    db.commit()


def close_supply(db: Session, supply: Supply, wb_client: WBClient) -> Supply:
    resp = wb_client.close_supply(supply.wb_supply_id)
    supply.status = SupplyStatus.CLOSED if resp.get("ok") else SupplyStatus.FAILED
    import datetime as dt

    if supply.status == SupplyStatus.CLOSED:
        supply.closed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(supply)
    return supply


def get_supply_qr(db: Session, supply: Supply, wb_client: WBClient) -> dict:
    if supply.status not in (SupplyStatus.CLOSED, SupplyStatus.PARTIAL):
        raise AppError(
            "QR поставки доступен только после закрытия поставки.",
            status_code=409,
            reason_code="supply_not_closed",
            what_to_do="Сначала закройте поставку.",
        )
    if not supply.qr_data:
        sticker = wb_client.get_supply_qr(supply.wb_supply_id)
        supply.qr_data = sticker["data"]
        db.commit()
    return {"type": "png", "data": supply.qr_data}


def get_supply_boxes_qr(db: Session, supply: Supply, amount: int, wb_client: WBClient) -> list[SupplyBox]:
    if supply.status not in (SupplyStatus.CLOSED, SupplyStatus.PARTIAL):
        raise AppError(
            "QR коробов доступен только после закрытия поставки.",
            status_code=409,
            reason_code="supply_not_closed",
        )
    stickers = wb_client.get_supply_boxes_qr(supply.wb_supply_id, amount)
    boxes = []
    for st in stickers:
        box = SupplyBox(supply_id=supply.id, sticker_data=st["data"])
        db.add(box)
        boxes.append(box)
    db.commit()
    for b in boxes:
        db.refresh(b)
    return boxes
