"""Экран «Склад»: агрегация по SKU, передача остатка в ФБС (Scope IN п.7),
изменение/списание/перемещение остатка с журналом (Scope IN п.3, FEATURES-PLAN.md этап 2).
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfil.errors import AppError, CellOccupiedError, StockChangedError, WbApiError
from fulfil.integrations.wb.base import WBClient
from fulfil.models.client import Client
from fulfil.models.fbs import Order, OrderItem, OrderStatus
from fulfil.models.product import Product
from fulfil.models.stock import FbsTransfer, FbsTransferStatus, MoveReason, StockByCell, StockMove, new_move_group_id
from fulfil.models.storage import Cell
from fulfil.services.stock_ledger import apply_move

# Заказы, чья позиция ещё не списана с полки (Этап 2 плана №3, п.2.3). PACKED и
# позже уже прошли commit_pick_lines() (services/picking.py) — остаток по ним
# списан из stock_by_cell, поэтому считать их здесь ещё раз значило бы вычесть
# одно и то же количество дважды.
_UNASSEMBLED_ORDER_STATUSES = (OrderStatus.NEW, OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY)


def _in_orders_qty(db: Session, product_id: int) -> int:
    return db.scalar(
        select(func.coalesce(func.sum(OrderItem.qty), 0))
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.product_id == product_id, Order.status.in_(_UNASSEMBLED_ORDER_STATUSES))
    ) or 0


def _summary_from_parts(product_id: int, total: int, in_orders: int, wb_amount: int) -> dict:
    # Новая формула (Этап 2, п.2.3) вместо total − Σ уже отправленного: та сумма не
    # уменьшается при сборке заказа, поэтому как только заказ собран, появлялся
    # ложный fbsOversold. Здесь и «в заказах», и «на WB» вычитаются из того, что
    # физически лежит на полках, — оба уменьшают именно доступное к передаче.
    return {
        "productId": product_id,
        "total": total,
        "inOrders": in_orders,
        "wbFbsAmount": wb_amount,
        "availableToTransfer": max(total - in_orders - wb_amount, 0),
        # WB считает остаток больше, чем у нас физически есть (например, списали
        # товар на полке, а на WB значение ещё не обновили) — видно тут явно.
        "fbsOversold": max(wb_amount - (total - in_orders), 0),
    }


def get_stock_summary(db: Session, product: Product) -> dict:
    total = db.scalar(
        select(func.coalesce(func.sum(StockByCell.qty), 0)).where(
            StockByCell.product_id == product.id
        )
    ) or 0
    in_orders = _in_orders_qty(db, product.id)
    return _summary_from_parts(product.id, total, in_orders, product.wb_fbs_amount or 0)


def list_stock_summaries(db: Session, *, client_id: int | None = None) -> dict[int, dict]:
    """Тот же агрегат, что get_stock_summary(), но для ВСЕХ товаров сразу — три
    GROUP BY запроса вместо одного на каждый товар (Этап 2, п.2.3: список «Остатки»
    раньше делал get_stock_summary()+get_stock_by_cell() в цикле по каждой строке).
    Возвращает {productId: summary} — товары без остатка и без заказов в словаре
    просто не появятся, вызывающая сторона решает, показывать ли нулевую строку."""
    totals_stmt = select(StockByCell.product_id, func.sum(StockByCell.qty)).group_by(StockByCell.product_id)
    orders_stmt = (
        select(OrderItem.product_id, func.sum(OrderItem.qty))
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.status.in_(_UNASSEMBLED_ORDER_STATUSES))
        .group_by(OrderItem.product_id)
    )
    wb_stmt = select(Product.id, Product.wb_fbs_amount)
    if client_id is not None:
        totals_stmt = totals_stmt.join(Product, Product.id == StockByCell.product_id).where(
            Product.client_id == client_id
        )
        orders_stmt = orders_stmt.join(Product, Product.id == OrderItem.product_id).where(
            Product.client_id == client_id
        )
        wb_stmt = wb_stmt.where(Product.client_id == client_id)

    totals = dict(db.execute(totals_stmt).all())
    in_orders = dict(db.execute(orders_stmt).all())
    wb_amounts = dict(db.execute(wb_stmt).all())

    product_ids = set(totals) | set(in_orders) | {pid for pid, amt in wb_amounts.items() if amt}
    return {
        pid: _summary_from_parts(pid, totals.get(pid, 0), in_orders.get(pid, 0), wb_amounts.get(pid, 0) or 0)
        for pid in product_ids
    }


def get_stock_by_cell(db: Session, product: Product) -> list[dict]:
    rows = db.execute(
        select(StockByCell, Cell)
        .join(Cell, Cell.id == StockByCell.cell_id)
        .where(StockByCell.product_id == product.id, StockByCell.qty > 0)
        .order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no)
    ).all()
    return [
        {"cellId": c.id, "cellAddress": c.address, "cellBarcode": c.barcode, "qty": s.qty}
        for s, c in rows
    ]


def list_stock_by_cell(db: Session, *, client_id: int | None = None) -> dict[int, list[dict]]:
    """Разбивка по ячейкам для ВСЕХ товаров сразу — один запрос вместо
    get_stock_by_cell() в цикле по каждой строке (Этап 2, п.2.3)."""
    stmt = (
        select(StockByCell, Cell)
        .join(Cell, Cell.id == StockByCell.cell_id)
        .where(StockByCell.qty > 0)
        .order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no)
    )
    if client_id is not None:
        stmt = stmt.join(Product, Product.id == StockByCell.product_id).where(Product.client_id == client_id)

    by_product: dict[int, list[dict]] = {}
    for s, c in db.execute(stmt).all():
        by_product.setdefault(s.product_id, []).append(
            {"cellId": c.id, "cellAddress": c.address, "cellBarcode": c.barcode, "qty": s.qty}
        )
    return by_product


def transfer_to_fbs(
    db: Session, product: Product, qty: int, idempotency_key: str, wb_client: WBClient
) -> FbsTransfer:
    existing = db.scalar(select(FbsTransfer).where(FbsTransfer.idempotency_key == idempotency_key))
    if existing is not None:
        return existing  # повтор того же запроса — не передаём второй раз

    # Склад берётся из клиента товара (Этап 1, п.1.4) — settings.wb_warehouse_id
    # устарел, читает его только миграция.
    warehouse_id = product.client.wb_warehouse_id
    if not warehouse_id:
        raise AppError(
            f'У клиента «{product.client.name}» не указан склад WB — передать остаток некуда.',
            status_code=409,
            reason_code="no_wb_warehouse",
            what_to_do="Укажите склад WB в разделе «Клиенты».",
        )

    # Блокируем строку товара на время всей операции — проверка + чтение с WB +
    # запись на WB — одной транзакцией (P0-3/P1-7). Раньше это не было защищено:
    # две параллельные передачи (две вкладки, либо "Передать всё" вперемешку с
    # ручной) обе проходили проверку availableToTransfer по одному и тому же
    # остатку и обе отправляли своё "before + qty" на WB — итоговое значение на
    # WB завышалось на величину одной из передач (перепродажа).
    locked_product = db.execute(
        select(Product).where(Product.id == product.id).with_for_update()
    ).scalar_one()

    summary = get_stock_summary(db, locked_product)
    if qty > summary["availableToTransfer"]:
        raise AppError(
            f"Нельзя передать {qty} шт — доступно только {summary['availableToTransfer']}.",
            status_code=409,
            reason_code="not_enough_stock",
        )

    transfer = FbsTransfer(
        product_id=locked_product.id,
        qty=qty,
        wb_warehouse_id=warehouse_id,
        status=FbsTransferStatus.PENDING,
        idempotency_key=idempotency_key,
    )
    db.add(transfer)
    db.flush()  # transfer.id нужен ниже, но коммитить рано — иначе снимется блокировка товара

    try:
        # WB ЗАДАЁТ остаток на складе (не прибавляет к нему) — читаем, что там
        # сейчас, и отправляем текущее + qty. Раньше здесь отправлялся голый qty,
        # и вторая передача «+3» поверх уже переданных «5» стирала остаток на WB
        # тройкой вместо восьмёрки (Этап 2, п.2.3).
        before = wb_client.get_fbs_stocks(warehouse_id, [locked_product.barcode]).get(locked_product.barcode, 0)
        after = before + qty
        resp = wb_client.set_fbs_stocks(warehouse_id, {locked_product.barcode: after})
        transfer.wb_amount_before = before
        transfer.wb_amount_after = after
        transfer.wb_response = str(resp)
        if resp.get("ok"):
            transfer.status = FbsTransferStatus.SENT
            locked_product.wb_fbs_amount = after
        else:
            transfer.status = FbsTransferStatus.FAILED
    except WbApiError as exc:
        # Узкий except: WbApiError — это WB сказал «нет» (лимиты/сеть/токен), и это
        # ожидаемый исход, который стоит записать как FAILED с текстом причины.
        # Баг в самом коде (не WbApiError) должен падать наружу, а не тихо
        # прятаться под тем же статусом (было `except Exception` — см. Этап 2, п.2.3).
        transfer.status = FbsTransferStatus.FAILED
        transfer.wb_response = exc.detail

    db.commit()
    db.refresh(transfer)
    return transfer


def refresh_wb_fbs_amounts(db: Session, client: Client, wb_client: WBClient) -> int:
    """Синхронизирует кэш Product.wb_fbs_amount с фактическим остатком на складе WB
    (P0-3). Раньше это поле обновлялось ТОЛЬКО при transfer_to_fbs — а WB сам
    уменьшает остаток при каждой продаже, поэтому кэш расходился с реальностью:
    формула availableToTransfer = total - inOrders - wbFbsAmount вычитала уже
    проданное дважды (один раз как inOrders, второй раз спрятанным в устаревшем
    wbFbsAmount), занижая доступное к передаче и рисуя ложный fbsOversold.
    Вызывается из фонового опроса (jobs.py) по каждому клиенту."""
    if not client.wb_warehouse_id:
        return 0
    products = list(
        db.scalars(
            select(Product).where(Product.client_id == client.id, Product.archived_at.is_(None))
        )
    )
    if not products:
        return 0
    by_barcode = {p.barcode: p for p in products}
    amounts = wb_client.get_fbs_stocks(client.wb_warehouse_id, list(by_barcode))
    updated = 0
    for barcode, amount in amounts.items():
        product = by_barcode.get(barcode)
        if product is not None and product.wb_fbs_amount != amount:
            product.wb_fbs_amount = amount
            updated += 1
    db.commit()
    return updated


def _lock_cell(db: Session, cell: Cell) -> Cell:
    return db.execute(select(Cell).where(Cell.id == cell.id).with_for_update()).scalar_one()


def _current_qty(db: Session, product: Product, cell: Cell) -> int:
    row = db.scalar(
        select(StockByCell).where(StockByCell.product_id == product.id, StockByCell.cell_id == cell.id)
    )
    return row.qty if row else 0


def adjust_stock(
    db: Session, product: Product, cell: Cell, new_qty: int, expected_qty: int, actor: str,
    comment: str | None = None,
) -> StockByCell:
    """CAS-корректировка: expected_qty — то, что оператор видел на экране. Если фактический
    остаток уже другой (кто-то принял/собрал заказ параллельно), 409 stock_changed вместо
    молчаливой перезаписи (DEV-PLAN.md, приём stock.html эталона)."""
    if new_qty < 0:
        raise AppError("Остаток не может быть отрицательным.", status_code=400, reason_code="invalid_qty")

    locked_cell = _lock_cell(db, cell)
    current = _current_qty(db, product, locked_cell)
    if current != expected_qty:
        raise StockChangedError(
            f"Остаток изменился: вы видели {expected_qty}, сейчас в ячейке {locked_cell.address} — {current}.",
            actual_qty=current,
        )

    delta = new_qty - current
    if delta == 0:
        row = db.scalar(
            select(StockByCell).where(StockByCell.product_id == product.id, StockByCell.cell_id == locked_cell.id)
        )
        return row

    if delta < 0 and not comment:
        raise AppError(
            "Уменьшение остатка требует комментария — он попадёт в журнал.",
            status_code=400,
            reason_code="comment_required",
        )

    row = apply_move(
        db, product=product, cell=locked_cell, qty_delta=delta, reason=MoveReason.ADJUST,
        actor=actor, comment=comment,
    )
    db.commit()
    db.refresh(row)
    return row


def write_off_stock(
    db: Session, product: Product, cell: Cell, expected_qty: int, actor: str, comment: str,
) -> None:
    """«Удаление» количества — списание в ноль, а не удаление строки: stock_by_cell
    остаётся проекцией stock_moves (DEV-PLAN.md, инвариант №1)."""
    if not comment:
        raise AppError(
            "Списание требует комментария — он попадёт в журнал.",
            status_code=400,
            reason_code="comment_required",
        )

    locked_cell = _lock_cell(db, cell)
    current = _current_qty(db, product, locked_cell)
    if current != expected_qty:
        raise StockChangedError(
            f"Остаток изменился: вы видели {expected_qty}, сейчас в ячейке {locked_cell.address} — {current}.",
            actual_qty=current,
        )
    if current == 0:
        raise AppError("Списывать нечего — остаток уже 0.", status_code=400, reason_code="nothing_to_write_off")

    apply_move(
        db, product=product, cell=locked_cell, qty_delta=-current, reason=MoveReason.WRITE_OFF,
        actor=actor, comment=comment,
    )
    db.commit()


def move_stock(
    db: Session, product: Product, from_cell: Cell, to_cell: Cell, qty: int, expected_qty: int,
    actor: str, comment: str | None = None,
) -> dict:
    """Перемещение между ячейками. Обе ячейки блокируются в порядке возрастания id —
    иначе два встречных перемещения дают дедлок (FEATURES-PLAN.md, этап 2.3)."""
    from fulfil.services.storage import check_placement_allowed, find_free_cell_suggestions

    if from_cell.id == to_cell.id:
        raise AppError("Нельзя переместить в ту же ячейку.", status_code=400, reason_code="same_cell")

    first_id, second_id = sorted((from_cell.id, to_cell.id))
    locked_by_id = {
        c.id: c
        for c in db.scalars(
            select(Cell).where(Cell.id.in_([first_id, second_id])).order_by(Cell.id).with_for_update()
        )
    }
    locked_from = locked_by_id[from_cell.id]
    locked_to = locked_by_id[to_cell.id]

    current = _current_qty(db, product, locked_from)
    if current != expected_qty:
        raise StockChangedError(
            f"Остаток изменился: вы видели {expected_qty}, сейчас в ячейке {locked_from.address} — {current}.",
            actual_qty=current,
        )
    if qty > current:
        raise AppError(
            f"Нельзя переместить {qty} шт — в ячейке {locked_from.address} только {current}.",
            status_code=409,
            reason_code="not_enough_stock",
        )

    check_placement_allowed(db, locked_to, product.barcode)
    other_sku = db.scalars(
        select(StockByCell).where(
            StockByCell.cell_id == locked_to.id,
            StockByCell.product_id != product.id,
            StockByCell.qty > 0,
        )
    ).all()
    if other_sku:
        suggestions = find_free_cell_suggestions(db, locked_to.zone_code)
        raise CellOccupiedError(
            f"Ячейка {locked_to.address} занята другим товаром.", suggestions=suggestions
        )

    group_id = new_move_group_id()
    apply_move(
        db, product=product, cell=locked_from, qty_delta=-qty, reason=MoveReason.MOVE_OUT,
        actor=actor, comment=comment, move_group_id=group_id,
    )
    apply_move(
        db, product=product, cell=locked_to, qty_delta=qty, reason=MoveReason.MOVE_IN,
        actor=actor, comment=comment, move_group_id=group_id,
    )
    db.commit()
    return {"fromCellAddress": locked_from.address, "toCellAddress": locked_to.address, "qty": qty}


def get_moves(
    db: Session,
    *,
    product_id: int | None = None,
    cell_id: int | None = None,
    actor: str | None = None,
    reason: str | None = None,
    from_date: dt.datetime | None = None,
    to_date: dt.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Журнал движений (Scope IN п.3: кто/что/когда изменил). qtyAfter считается оконной
    функцией по всей истории (product, cell) — не хранится, иначе разъехалось бы при
    любой правке истории (FEATURES-PLAN.md, этап 2.4)."""
    base = select(
        StockMove.id.label("move_id"),
        StockMove.product_id.label("product_id"),
        StockMove.cell_id.label("cell_id"),
        StockMove.qty_delta.label("qty_delta"),
        StockMove.reason.label("reason"),
        StockMove.actor.label("actor"),
        StockMove.comment.label("comment"),
        StockMove.move_group_id.label("move_group_id"),
        StockMove.ref_type.label("ref_type"),
        StockMove.ref_id.label("ref_id"),
        StockMove.created_at.label("created_at"),
        func.sum(StockMove.qty_delta)
        .over(partition_by=(StockMove.product_id, StockMove.cell_id), order_by=(StockMove.created_at, StockMove.id))
        .label("qty_after"),
    ).subquery()

    stmt = (
        select(base, Product.name.label("product_name"), Cell.address.label("cell_address"))
        .join(Product, Product.id == base.c.product_id)
        .join(Cell, Cell.id == base.c.cell_id)
    )
    if product_id is not None:
        stmt = stmt.where(base.c.product_id == product_id)
    if cell_id is not None:
        stmt = stmt.where(base.c.cell_id == cell_id)
    if actor is not None:
        stmt = stmt.where(base.c.actor == actor)
    if reason is not None:
        stmt = stmt.where(base.c.reason == reason)
    if from_date is not None:
        stmt = stmt.where(base.c.created_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(base.c.created_at <= to_date)

    stmt = stmt.order_by(base.c.created_at.desc(), base.c.move_id.desc()).limit(limit).offset(offset)

    return [
        {
            "id": row.move_id,
            "createdAt": row.created_at,
            "actor": row.actor,
            "reason": row.reason.value if hasattr(row.reason, "value") else row.reason,
            "qtyDelta": row.qty_delta,
            "qtyAfter": row.qty_after,
            "comment": row.comment,
            "moveGroupId": row.move_group_id,
            "refType": row.ref_type,
            "refId": row.ref_id,
            "productId": row.product_id,
            "productName": row.product_name,
            "cellId": row.cell_id,
            "cellAddress": row.cell_address,
        }
        for row in db.execute(stmt)
    ]
