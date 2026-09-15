"""Сборка заказа: QR/этикетка, код «Честный знак», подтверждение (Scope IN п.10-11).

Код маркировки хранится РОВНО как выдал сканер (с разделителем GS и криптохвостом) —
на бэке нет парсера формата, только проверка на повтор через уникальный индекс БД.
Списание остатка проводится и коммитится ДО отправки марок в WB (P1-8): если бы
марки ушли в WB раньше коммита и сам коммит потом упал, в WB марки уже есть, а у
нас заказ не собран — рассинхрон, который нечем откатить (WB не отменяет отправленные
марки). В обратном порядке худший исход — WB отклонил уже списанный подбор, тогда
оператор просто повторяет отправку марок, не пересобирая лист.
"""

import datetime as dt

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fulfil.errors import AppError, DuplicateMarkError
from fulfil.integrations.wb.base import WBClient
from fulfil.models.fbs import Order, OrderItem, OrderItemMark, OrderStatus
from fulfil.services.picking import build_pick_list, commit_pick_lines, rebuild_pick_list


def scan_mark(db: Session, order_item: OrderItem, raw_code: str) -> OrderItemMark:
    if not raw_code:
        raise AppError("Пустой код.", status_code=400, reason_code="empty_code")

    order = order_item.order
    if order.status not in (OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY):
        raise AppError(
            f'Заказ в статусе "{order.status.value}" — сканировать марки нельзя.',
            status_code=409,
            reason_code="wrong_status",
        )
    already = len(order_item.marks)
    if already >= order_item.qty:
        raise AppError(
            f"Для этой позиции уже отсканировано {already} из {order_item.qty} марок — больше не требуется.",
            status_code=409,
            reason_code="marks_limit_reached",
        )

    mark = OrderItemMark(order_item_id=order_item.id, mark_code=raw_code)
    db.add(mark)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DuplicateMarkError() from None
    db.refresh(mark)
    return mark


def fetch_order_sticker(db: Session, order: Order, wb_client: WBClient) -> dict:
    first_item = order.items[0] if order.items else None
    if first_item is None:
        raise AppError("В заказе нет позиций.", status_code=400)
    if not first_item.sticker_data:
        sticker = wb_client.get_order_sticker(order.wb_order_id)
        first_item.sticker_data = sticker["data"]
        first_item.sticker_type = sticker["type"]
        db.commit()
    return {"type": first_item.sticker_type, "data": first_item.sticker_data}


def confirm_assembly(db: Session, order: Order, wb_client: WBClient) -> Order:
    if order.status not in (OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY):
        raise AppError(
            f'Заказ в статусе "{order.status.value}" нельзя собрать.',
            status_code=409,
            reason_code="wrong_status",
        )

    all_codes = [m.mark_code for item in order.items for m in item.marks]
    expected_marks = sum(item.qty for item in order.items)
    if all_codes and len(all_codes) != expected_marks:
        # Частично отсканированные марки — подтверждать нечем (P1-8): раньше
        # confirm-assembly отправлял в WB столько марок, сколько успели
        # отсканировать, даже если это меньше/больше количества товара в заказе.
        raise AppError(
            f"Отсканировано {len(all_codes)} марок «Честный знак», а нужно {expected_marks} — "
            f"досканируйте оставшиеся марки перед подтверждением сборки.",
            status_code=409,
            reason_code="marks_count_mismatch",
        )

    build_pick_list(db, order)
    try:
        commit_pick_lines(db, order)
    except AppError as exc:
        if exc.reason_code != "stock_changed":
            raise
        # Лист устарел (P0-1: остаток заняла другая сборка) — пересобираем по
        # актуальному остатку и пробуем списать один раз ещё, вместо того чтобы
        # заказ навсегда застрял с протухшим листом.
        rebuild_pick_list(db, order)
        commit_pick_lines(db, order)

    # Списание остатка фиксируем ДО обращения к WB — см. пояснение в шапке файла.
    db.commit()

    if all_codes:
        resp = wb_client.send_marking_codes(order.wb_order_id, all_codes)
        if not resp.get("ok"):
            raise AppError(
                "WB не принял коды маркировки — сборка не подтверждена.",
                status_code=502,
                reason_code="wb_marking_rejected",
                what_to_do="Повторите сканирование кодов и попробуйте снова.",
            )
        now = dt.datetime.now(dt.timezone.utc)
        for item in order.items:
            for m in item.marks:
                m.sent_to_wb_at = now

    order.status = OrderStatus.PACKED
    db.commit()
    db.refresh(order)
    return order
