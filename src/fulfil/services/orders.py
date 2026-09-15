"""Синхронизация заказов ФБС и необратимое «взятие в работу» (Scope IN п.8, дни 8-10;
Этап 3 плана №3, пп.4.1, 4.2 problems.txt).

Система никогда не отменяет заказ на маркетплейсе сама (см. DEV-PLAN.md, конвейер ФБС) —
при нехватке товара выставляется REJECTED_NO_STOCK, а не CANCELLED. Отмену ставит
только WB (через refresh_order_statuses) — обратной операции у WB тоже нет.
"""

import datetime as dt

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from fulfil.errors import AppError, NotFoundError
from fulfil.integrations.wb import get_wb_client
from fulfil.integrations.wb.base import WBClient
from fulfil.models.client import Client
from fulfil.models.fbs import Order, OrderItem, OrderStatus, Supply, SupplyStatus
from fulfil.models.product import Product
from fulfil.services import supplies as supplies_service
from fulfil.services.products import sync_products_from_wb

# Заказы, чей исход на WB ещё не известен — опрашиваются статусом отдельно от
# /orders/new (Этап 3, п.3.1): без этого отмена покупателем, «в доставке» и
# «принята» до нас не доходят вообще.
_INCOMPLETE_STATUSES = (
    OrderStatus.NEW,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_ASSEMBLY,
    OrderStatus.PACKED,
    OrderStatus.IN_SUPPLY,
)
_WB_CANCELLED_STATUSES = {"canceled", "canceled_by_client", "declined_by_client"}

_ARCHIVE_STATUSES = (
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
    OrderStatus.CANCELLED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED_NO_STOCK,
)


def _find_product(db: Session, client: Client, barcode: str) -> Product | None:
    return db.scalar(
        select(Product).where(
            Product.client_id == client.id, Product.barcode == barcode, Product.archived_at.is_(None),
        )
    )


def sync_orders_from_wb(db: Session, client: Client, wb_client: WBClient) -> list[Order]:
    """Синхронизация заказов ОДНОГО клиента. Берутся только заказы с
    warehouseId == client.wb_warehouse_id — заказы с чужих складов продавца
    в базу не попадают (Этап 3, п.4.2 problems.txt). Товар в позициях ищется
    в пределах этого же клиента (Этап 1, п.1.1); если не найден — карточки
    клиента пересинхронизируются один раз за вызов и поиск повторяется, а
    если товар всё равно не найден, заказ сохраняется с problem='unknown_sku'
    вместо того, чтобы тихо потерять позицию (problems.txt, п.6)."""
    if not client.wb_warehouse_id:
        raise AppError(
            f'У клиента «{client.name}» не указан склад WB — непонятно, какие заказы наши.',
            status_code=409,
            reason_code="no_wb_warehouse",
            what_to_do="Укажите склад WB в разделе «Клиенты».",
        )

    created: list[Order] = []
    cards_resynced = False
    try:
        wb_orders = wb_client.get_new_orders()
        for wo in wb_orders:
            if wo.get("warehouseId") != client.wb_warehouse_id:
                continue  # чужой склад продавца — не наша зона интересов
            existing = db.scalar(select(Order).where(Order.wb_order_id == wo["orderId"]))
            if existing is not None:
                continue

            # Товар резолвится ДО db.add(order) — sync_products_from_wb() коммитит сессию
            # сама при ошибке (см. её собственный except AppError), а нам нельзя дать ей
            # закоммитить наполовину собранный заказ без единой позиции.
            has_unknown_sku = False
            resolved_items: list[tuple[int | None, str, int]] = []
            for it in wo.get("items", []):
                product = _find_product(db, client, it["barcode"])
                if product is None and not cards_resynced:
                    sync_products_from_wb(db, client, wb_client)
                    cards_resynced = True
                    product = _find_product(db, client, it["barcode"])
                if product is None:
                    has_unknown_sku = True
                resolved_items.append((product.id if product else None, it["barcode"], it.get("qty", 1)))

            order = Order(
                client_id=client.id,
                wb_order_id=wo["orderId"],
                wb_supply_id=wo.get("supplyId"),
                wb_warehouse_id=wo.get("warehouseId"),
                wb_nm_id=wo.get("nmId"),
                wb_chrt_id=wo.get("chrtId"),
                article=wo.get("article"),
                status=OrderStatus.NEW,
                problem="unknown_sku" if has_unknown_sku else None,
                created_at_wb=_parse_wb_dt(wo.get("createdAt")),
                deadline_at=_parse_wb_dt(wo.get("deadlineAt")),
            )
            db.add(order)
            db.flush()
            for product_id, barcode, qty in resolved_items:
                db.add(OrderItem(order_id=order.id, product_id=product_id, barcode=barcode, qty=qty))
            created.append(order)
    except AppError as exc:
        db.rollback()
        client.last_sync_error = exc.detail
        db.commit()
        raise

    client.last_sync_at = dt.datetime.now(dt.timezone.utc)
    client.last_sync_error = None
    db.commit()
    for o in created:
        db.refresh(o)
    return created


def refresh_order_statuses(db: Session, client: Client, wb_client: WBClient) -> dict:
    """Опрашивает статус наших НЕЗАВЕРШЁННЫХ заказов (Этап 3, п.3.1) — раньше
    опрашивался только /orders/new, поэтому отмена покупателем до нас не доходила."""
    orders = list(
        db.scalars(
            select(Order).where(Order.client_id == client.id, Order.status.in_(_INCOMPLETE_STATUSES))
        )
    )
    if not orders:
        return {"checked": 0, "cancelled": 0}

    try:
        statuses = wb_client.get_order_statuses([o.wb_order_id for o in orders])
    except AppError as exc:
        client.last_sync_error = exc.detail
        db.commit()
        raise

    cancelled = 0
    for order in orders:
        st = statuses.get(order.wb_order_id)
        if st is None:
            continue
        order.wb_status = st.get("wbStatus")
        order.supplier_status = st.get("supplierStatus")
        if order.wb_status in _WB_CANCELLED_STATUSES and order.status != OrderStatus.CANCELLED:
            order.status = OrderStatus.CANCELLED
            cancelled += 1

    client.last_sync_at = dt.datetime.now(dt.timezone.utc)
    client.last_sync_error = None
    db.commit()
    return {"checked": len(orders), "cancelled": cancelled}


def sync_orders(db: Session) -> list[dict]:
    """Синхронизация заказов по ВСЕМ активным клиентам с заданным ключом API и
    выбранным складом (Этап 3, п.3.1) — используется и кнопкой «Обновить из WB»,
    и фоновым опросом (jobs.py). Ошибка одного клиента не останавливает остальных."""
    clients = list(
        db.scalars(
            select(Client).where(
                Client.archived_at.is_(None),
                Client.wb_api_key_enc.is_not(None),
                Client.wb_warehouse_id.is_not(None),
            )
        )
    )
    results = []
    for client in clients:
        try:
            wb_client = get_wb_client(client)
            created = sync_orders_from_wb(db, client, wb_client)
            refresh_order_statuses(db, client, wb_client)
            results.append(
                {"clientId": client.id, "clientName": client.name, "created": len(created), "error": None}
            )
        except AppError as exc:
            db.rollback()
            results.append(
                {"clientId": client.id, "clientName": client.name, "created": 0, "error": exc.detail}
            )
    return results


def _parse_wb_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_or_create_open_supply(db: Session, client: Client, wb_client: WBClient) -> Supply:
    supply = db.scalar(
        select(Supply)
        .where(Supply.client_id == client.id, Supply.status == SupplyStatus.OPEN)
        .order_by(Supply.id.desc())
    )
    if supply is not None:
        return supply
    name = f"{client.name} {dt.date.today().isoformat()}"
    return supplies_service.create_supply(db, client, wb_client, name=name)


def take_to_work(db: Session, order: Order, wb_client: WBClient) -> Order:
    """Необратимо: подтверждает заказ на WB, добавляя его в поставку клиента
    (Этап 3, п.3.2 — problems.txt №3: раньше take_to_work никогда не обращался
    к WB, и в модели WB заказ попадает «на сборку» только после добавления
    в поставку). Обратной операции у WB нет — предупреждение показывается
    фронтом до вызова, не здесь."""
    if order.status != OrderStatus.NEW:
        raise AppError(
            f'Заказ в статусе "{order.status.value}" нельзя взять в работу.',
            status_code=409,
            reason_code="wrong_status",
        )
    if order.problem:
        raise AppError(
            "У заказа не распознан товар — сначала разберитесь с позицией (обновите каталог или "
            "привяжите товар вручную), потом берите заказ в работу.",
            status_code=409,
            reason_code="order_has_problem",
        )
    supply = _find_or_create_open_supply(db, order.client, wb_client)
    supplies_service.add_order_to_supply(db, supply, order, wb_client)
    db.refresh(order)
    return order


def take_to_work_bulk(db: Session, order_ids: list[int]) -> list[dict]:
    """Массовое «Взять в работу выбранные» (Этап 3, п.3.2) — одна поставка на
    каждого клиента: find_or_create_open_supply находит уже созданную в этом
    же вызове поставку для повторного клиента, а не плодит новую на каждый заказ."""
    results = []
    for order_id in order_ids:
        order = db.get(Order, order_id)
        if order is None:
            results.append({"orderId": order_id, "ok": False, "error": f"Заказ #{order_id} не найден."})
            continue
        try:
            wb_client = get_wb_client(order.client)
            take_to_work(db, order, wb_client)
            results.append({"orderId": order_id, "ok": True, "error": None})
        except AppError as exc:
            db.rollback()
            results.append({"orderId": order_id, "ok": False, "error": exc.detail})
    return results


def list_orders(
    db: Session, *, client_id: int | None = None, warehouse_id: str | None = None,
    group: str | None = None, limit: int = 100, offset: int = 0,
) -> list[Order]:
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.client))
        .order_by(Order.id.desc())
    )
    if group in ("packed", "archive"):
        stmt = stmt.outerjoin(Supply, Supply.id == Order.supply_id)
    if group == "new":
        stmt = stmt.where(Order.status == OrderStatus.NEW)
    elif group == "assembly":
        stmt = stmt.where(Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY)))
    elif group == "packed":
        stmt = stmt.where(Order.status == OrderStatus.PACKED, Supply.status == SupplyStatus.OPEN)
    elif group == "archive":
        stmt = stmt.where(
            Order.status.in_(_ARCHIVE_STATUSES)
            | ((Order.status == OrderStatus.PACKED) & (Supply.status != SupplyStatus.OPEN))
        )
    if client_id is not None:
        stmt = stmt.where(Order.client_id == client_id)
    if warehouse_id is not None:
        stmt = stmt.where(Order.wb_warehouse_id == warehouse_id)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt))


def get_order_counters(db: Session, *, client_id: int | None = None) -> dict:
    """{new, assembly, packed, problems} — Этап 3, п.3.3. "problems" пересекается
    с остальными группами (проблемный заказ обычно ещё и NEW), поэтому считается
    отдельным запросом, а не веткой того же GROUP BY (было бы двойным счётом)."""
    group_expr = case(
        (Order.status == OrderStatus.NEW, "new"),
        (Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY)), "assembly"),
        ((Order.status == OrderStatus.PACKED) & (Supply.status == SupplyStatus.OPEN), "packed"),
        else_="archive",
    )
    stmt = select(group_expr.label("grp"), func.count()).select_from(Order).outerjoin(
        Supply, Supply.id == Order.supply_id
    )
    problems_stmt = select(func.count()).select_from(Order).where(Order.problem.is_not(None))
    if client_id is not None:
        stmt = stmt.where(Order.client_id == client_id)
        problems_stmt = problems_stmt.where(Order.client_id == client_id)
    stmt = stmt.group_by(group_expr)

    counts = dict(db.execute(stmt).all())
    return {
        "new": counts.get("new", 0),
        "assembly": counts.get("assembly", 0),
        "packed": counts.get("packed", 0),
        "problems": db.scalar(problems_stmt) or 0,
    }


def get_order_or_404(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"Заказ #{order_id} не найден.")
    return order
