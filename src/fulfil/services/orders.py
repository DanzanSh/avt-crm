"""Синхронизация заказов ФБС и необратимое «взятие в работу» (Scope IN п.8, дни 8-10).

Система никогда не отменяет заказ на маркетплейсе сама (см. DEV-PLAN.md, конвейер ФБС) —
при нехватке товара выставляется REJECTED_NO_STOCK, а не CANCELLED.
"""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.errors import AppError, NotFoundError
from fulfil.integrations.wb.base import WBClient
from fulfil.models.client import Client
from fulfil.models.fbs import Order, OrderItem, OrderStatus
from fulfil.models.product import Product


def sync_orders_from_wb(db: Session, client: Client, wb_client: WBClient) -> list[Order]:
    """Синхронизация заказов ОДНОГО клиента — товар в позициях ищется в пределах
    этого же клиента (Этап 1, п.1.1)."""
    wb_orders = wb_client.get_new_orders()
    created: list[Order] = []
    for wo in wb_orders:
        existing = db.scalar(select(Order).where(Order.wb_order_id == wo["orderId"]))
        if existing is not None:
            continue

        order = Order(
            client_id=client.id,
            wb_order_id=wo["orderId"],
            wb_supply_id=wo.get("supplyId"),
            status=OrderStatus.NEW,
            created_at_wb=_parse_wb_dt(wo.get("createdAt")),
            deadline_at=_parse_wb_dt(wo.get("deadlineAt")),
        )
        db.add(order)
        db.flush()

        for it in wo.get("items", []):
            product = db.scalar(
                select(Product).where(Product.client_id == client.id, Product.barcode == it["barcode"])
            )
            if product is None:
                continue  # товар не синхронизирован из каталога — пропускаем строку
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    barcode=it["barcode"],
                    qty=it.get("qty", 1),
                )
            )
        created.append(order)

    db.commit()
    for o in created:
        db.refresh(o)
    return created


def _parse_wb_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def take_to_work(db: Session, order: Order, wb_client: WBClient) -> Order:
    """Необратимо: подтверждает заказ на WB. Обратной операции у WB нет —
    предупреждение показывается фронтом до вызова, не здесь."""
    if order.status != OrderStatus.NEW:
        raise AppError(
            f'Заказ в статусе "{order.status.value}" нельзя взять в работу.',
            status_code=409,
            reason_code="wrong_status",
        )
    order.status = OrderStatus.CONFIRMED
    db.commit()
    db.refresh(order)
    return order


def get_order_or_404(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"Заказ #{order_id} не найден.")
    return order
