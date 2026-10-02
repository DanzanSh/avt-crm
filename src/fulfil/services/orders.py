"""Синхронизация заказов ФБС и необратимое «взятие в работу» (Scope IN п.8, дни 8-10;
Этап 3 плана №3, пп.4.1, 4.2 problems.txt).

Система никогда не отменяет заказ на маркетплейсе сама (см. DEV-PLAN.md, конвейер ФБС) —
при нехватке товара выставляется REJECTED_NO_STOCK, а не CANCELLED. Отмену ставит
только WB (через refresh_order_statuses) — обратной операции у WB тоже нет.
"""

import datetime as dt
import logging

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from fulfil.errors import AppError, NotFoundError
from fulfil.integrations.wb import get_wb_client
from fulfil.integrations.wb.base import WBClient, parse_wb_datetime
from fulfil.models.client import Client, deleted_client_ids
from fulfil.models.fbs import Order, OrderItem, OrderStatus, PickLine, Supply, SupplyStatus
from fulfil.models.product import Product
from fulfil.models.storage import Cell
from fulfil.models.stock import MoveReason
from fulfil.services import supplies as supplies_service
from fulfil.services.clients import list_syncable_clients
from fulfil.services.products import sync_products_from_wb
from fulfil.services.stock_ledger import apply_move

logger = logging.getLogger(__name__)

# Заказы, чей исход на WB ещё не известен — опрашиваются статусом отдельно от
# /orders/new (Этап 3, п.3.1): без этого отмена покупателем, «в доставке» и
# «принята» до нас не доходят вообще. SHIPPED тоже опрашивается: отгруженный
# нами заказ должен дойти до DELIVERED, а раньше выборка кончалась на IN_SUPPLY
# и ветка "sold" для него была недостижима.
_INCOMPLETE_STATUSES = (
    OrderStatus.NEW,
    OrderStatus.CONFIRMED,
    OrderStatus.IN_ASSEMBLY,
    OrderStatus.PACKED,
    OrderStatus.IN_SUPPLY,
    OrderStatus.SHIPPED,
)
_WB_CANCELLED_STATUSES = {"canceled", "canceled_by_client", "declined_by_client"}

# Стадия, до которой заказ дошёл ЛОКАЛЬНО. Словарь WB грубее нашего (CONFIRMED,
# IN_ASSEMBLY и PACKED — все они supplierStatus "confirm"), поэтому статусы WB
# только ПОДТЯГИВАЮТ заказ вперёд по стадиям и никогда не откатывают назад:
# иначе опрос статусов сбрасывал бы собранный заказ обратно в «подтверждён».
_STAGE_RANK = {
    OrderStatus.NEW: 0,
    OrderStatus.CONFIRMED: 1,
    OrderStatus.IN_ASSEMBLY: 2,
    OrderStatus.PACKED: 3,
    OrderStatus.IN_SUPPLY: 4,
    OrderStatus.SHIPPED: 5,
    OrderStatus.DELIVERED: 6,
}
# best-effort (см. предупреждение в шапке integrations/wb/http.py и
# docs/wb-api-contract.md): набор значений WB не проверен против боевого токена,
# поэтому неизвестное значение НЕ двигает статус, а не падает.
_WB_STATUS_STAGE = {
    "sorted": OrderStatus.SHIPPED,
    "ready_for_pickup": OrderStatus.SHIPPED,
    "sold": OrderStatus.DELIVERED,
}
_SUPPLIER_STATUS_STAGE = {
    "confirm": OrderStatus.CONFIRMED,
    "complete": OrderStatus.SHIPPED,
}
# Стадии, на которых заказ у нас ещё не собран: если WB говорит, что он уже
# отгружен, значит его провели в личном кабинете WB минуя нас, и остаток со
# склада не списывался (problem='shipped_outside', списывает кладовщик).
_UNPICKED_STAGES = (OrderStatus.NEW, OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY)
PROBLEM_SHIPPED_OUTSIDE = "shipped_outside"

_ARCHIVE_STATUSES = (
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
    OrderStatus.CANCELLED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED_NO_STOCK,
)


def _find_product(db: Session, client: Client, barcode: str) -> Product | None:
    product = db.scalar(
        select(Product).where(
            Product.client_id == client.id, Product.barcode == barcode, Product.archived_at.is_(None),
        )
    )
    if product is not None:
        return product
    # WB может отдать любой из нескольких skus одного размера карточки — Product
    # хранит только один как основной barcode, остальные в extra_barcodes
    # (P2-11, см. services.products._upsert_card). Без этой проверки заказ по
    # второму баркоду того же товара навсегда получал бы unknown_sku.
    for candidate in db.scalars(
        select(Product).where(Product.client_id == client.id, Product.archived_at.is_(None))
    ):
        if barcode in (candidate.extra_barcodes or []):
            return candidate
    return None


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
                created_at_wb=parse_wb_datetime(wo.get("createdAt")),
                deadline_at=parse_wb_datetime(wo.get("deadlineAt")),
            )
            # SAVEPOINT на каждый заказ (P2-12): фоновый опрос и ручная кнопка
            # «Обновить из WB» могут синкать одновременно и попытаться вставить
            # один и тот же wb_order_id — уникальный индекс ловит гонку через
            # IntegrityError. Без begin_nested() rollback() отменил бы ВСЮ пачку
            # уже обработанных заказов этого вызова, а не только дублирующийся;
            # так откатывается только этот один заказ, а не 500 на всю синхронизацию.
            try:
                with db.begin_nested():
                    db.add(order)
                    db.flush()
                    for product_id, barcode, qty in resolved_items:
                        db.add(OrderItem(order_id=order.id, product_id=product_id, barcode=barcode, qty=qty))
            except IntegrityError:
                continue  # уже создан параллельно — не наша забота в этом вызове
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


def _release_unpicked_pick_lines(db: Session, order: Order) -> None:
    """Удаляет ещё не списанные (picked_at IS NULL) строки листа подбора отменённого
    заказа (P1-5) — иначе они висят навсегда и занимают "резерв" в
    services.picking._reserved_qty_by_cell, мешая распределению под другие заказы."""
    db.execute(delete(PickLine).where(PickLine.order_id == order.id, PickLine.picked_at.is_(None)))


def _return_cancelled_order_stock(db: Session, order: Order, actor: str) -> None:
    """Возврат остатка при отмене уже СОБРАННОГО заказа (P1-5): подбор списывает
    остаток в services.picking.commit_pick_lines (PickLine.picked_at заполняется) —
    если WB отменяет такой заказ (покупатель отказался после сборки), товар физически
    остаётся на складе и должен вернуться на тот же остаток, а не потеряться."""
    picked_lines = db.scalars(
        select(PickLine).where(PickLine.order_id == order.id, PickLine.picked_at.is_not(None))
    ).all()
    for line in picked_lines:
        product = db.get(Product, line.product_id)
        cell = db.get(Cell, line.cell_id)
        if product is None or cell is None:
            continue
        apply_move(
            db, product=product, cell=cell, qty_delta=line.qty, reason=MoveReason.ADJUST,
            actor=actor, ref_type="order_cancel", ref_id=order.id,
            comment=f"Возврат остатка: заказ #{order.id} отменён WB после сборки.",
        )


def _wb_target_status(
    current: OrderStatus, wb_status: str | None, supplier_status: str | None
) -> OrderStatus | None:
    """Локальный статус, выведенный из статусов WB, или None, если двигать нечего.

    Отмена бьёт любую стадию — её ставит только WB, и обратной операции нет. Всё
    остальное — движение ВПЕРЁД по _STAGE_RANK: WB не различает наши CONFIRMED /
    IN_ASSEMBLY / PACKED, поэтому его "confirm" не должен сбрасывать собранный
    заказ обратно в «подтверждён»."""
    if wb_status in _WB_CANCELLED_STATUSES:
        return None if current == OrderStatus.CANCELLED else OrderStatus.CANCELLED
    stages = [
        s
        for s in (_WB_STATUS_STAGE.get(wb_status or ""), _SUPPLIER_STATUS_STAGE.get(supplier_status or ""))
        if s is not None
    ]
    if not stages:
        return None
    target = max(stages, key=lambda s: _STAGE_RANK[s])
    if _STAGE_RANK[target] <= _STAGE_RANK.get(current, 0):
        return None
    return target


def refresh_order_statuses(db: Session, client: Client, wb_client: WBClient) -> dict:
    """Опрашивает статус наших НЕЗАВЕРШЁННЫХ заказов (Этап 3, п.3.1) — раньше
    опрашивался только /orders/new, поэтому отмена покупателем до нас не доходила.

    Статусы WB не просто записываются в wb_status/supplier_status, но и двигают
    локальный статус: заказ, подтверждённый или отгруженный в личном кабинете WB
    минуя Fulfil, иначе навсегда оставался «Новым» и висел в плитке «Новые»."""
    orders = list(
        db.scalars(
            select(Order).where(Order.client_id == client.id, Order.status.in_(_INCOMPLETE_STATUSES))
        )
    )
    if not orders:
        return {"checked": 0, "cancelled": 0, "advanced": 0, "shippedOutside": 0}

    try:
        statuses = wb_client.get_order_statuses([o.wb_order_id for o in orders])
    except AppError as exc:
        client.last_sync_error = exc.detail
        db.commit()
        raise

    cancelled = 0
    advanced = 0
    shipped_outside = 0
    for order in orders:
        st = statuses.get(order.wb_order_id)
        if st is None:
            continue
        order.wb_status = st.get("wbStatus")
        order.supplier_status = st.get("supplierStatus")
        target = _wb_target_status(order.status, order.wb_status, order.supplier_status)
        if target is None:
            continue
        if target == OrderStatus.CANCELLED:
            was_packed = order.status == OrderStatus.PACKED
            _release_unpicked_pick_lines(db, order)
            order.status = OrderStatus.CANCELLED
            if was_packed:
                _return_cancelled_order_stock(db, order, actor="wb-sync")
            cancelled += 1
            continue
        if target in (OrderStatus.SHIPPED, OrderStatus.DELIVERED) and order.status in _UNPICKED_STAGES:
            # Заказ уехал на WB, а у нас сборки по нему не было — значит остаток
            # со склада не списывался. Сами его НЕ списываем (не знаем, из какой
            # ячейки физически ушёл товар): помечаем заказ, разбирается кладовщик.
            order.problem = PROBLEM_SHIPPED_OUTSIDE
            _release_unpicked_pick_lines(db, order)
            shipped_outside += 1
        order.status = target
        advanced += 1

    client.last_sync_at = dt.datetime.now(dt.timezone.utc)
    client.last_sync_error = None
    db.commit()
    return {
        "checked": len(orders),
        "cancelled": cancelled,
        "advanced": advanced,
        "shippedOutside": shipped_outside,
    }


def sync_client_orders(db: Session, client: Client, wb_client: WBClient) -> dict:
    """Один кабинет: импорт новых заказов + опрос статусов уже загруженных, одной
    строкой результата для тоста «Обновить из WB». Ошибка кабинета не бросается
    наружу — она уходит в строку результата, чтобы синк остальных не вставал."""
    try:
        created = sync_orders_from_wb(db, client, wb_client)
        refreshed = refresh_order_statuses(db, client, wb_client)
    except AppError as exc:
        db.rollback()
        return {
            "clientId": client.id,
            "clientName": client.name,
            "created": 0,
            "updated": 0,
            "error": exc.detail,
        }
    return {
        "clientId": client.id,
        "clientName": client.name,
        "created": len(created),
        # Сколько заказов подтянули статус из WB (подтверждён/отгружён/доставлен/
        # отменён в личном кабинете WB минуя нас) — без этого числа правка статусов
        # никак не видна в интерфейсе.
        "updated": refreshed["advanced"] + refreshed["cancelled"],
        "error": None,
    }


def sync_orders(db: Session) -> list[dict]:
    """Синхронизация заказов по ВСЕМ активным клиентам с заданным ключом API и
    выбранным складом (Этап 3, п.3.1) — используется и кнопкой «Обновить из WB»,
    и фоновым опросом (jobs.py). Ошибка одного клиента не останавливает остальных."""
    results = []
    for client in list_syncable_clients(db):
        try:
            wb_client = get_wb_client(client)
        except AppError as exc:
            results.append(
                {
                    "clientId": client.id,
                    "clientName": client.name,
                    "created": 0,
                    "updated": 0,
                    "error": exc.detail,
                }
            )
            continue
        results.append(sync_client_orders(db, client, wb_client))
    return results

def _find_open_supply(db: Session, client: Client) -> Supply | None:
    # Самая старая открытая поставка — «основная»: отдельные (срочные) поставки
    # создаются позже, и новые заказы не должны попадать в них сами собой.
    return db.scalar(
        select(Supply)
        .where(Supply.client_id == client.id, Supply.status == SupplyStatus.OPEN)
        .order_by(Supply.id.asc())
    )


def _check_can_take_to_work(order: Order) -> None:
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


def _take_orders_to_work(
    db: Session, client: Client, orders: list[Order], wb_client: WBClient, *, new_supply: bool = False,
) -> None:
    """Все заказы одного клиента — в его открытую поставку одним вызовом WB.
    Поставка, созданная здесь же, коммитится только вместе с заказами: если WB
    не принял заказы, она удаляется и в WB (best-effort), и локально — иначе
    оставалась пустая поставка, в которую потом безуспешно лезли все заказы."""
    supply = None if new_supply else _find_open_supply(db, client)
    created = supply is None
    if created:
        name = f"{client.name} {dt.date.today().isoformat()}" + (" срочная" if new_supply else "")
        supply = supplies_service.create_supply(db, client, wb_client, name=name, commit=False)
    wb_supply_id = supply.wb_supply_id
    try:
        supplies_service.add_orders_to_supply(db, supply, orders, wb_client)
    except AppError:
        db.rollback()
        if created and wb_supply_id:
            try:
                wb_client.delete_supply(wb_supply_id)
            except AppError:
                logger.warning("Не удалось удалить пустую поставку %s после сбоя добавления заказов", wb_supply_id)
        raise


def take_to_work(db: Session, order: Order, wb_client: WBClient) -> Order:
    """Необратимо: подтверждает заказ на WB, добавляя его в поставку клиента
    (Этап 3, п.3.2 — problems.txt №3: раньше take_to_work никогда не обращался
    к WB, и в модели WB заказ попадает «на сборку» только после добавления
    в поставку). Обратной операции у WB нет — предупреждение показывается
    фронтом до вызова, не здесь."""
    _check_can_take_to_work(order)
    _take_orders_to_work(db, order.client, [order], wb_client)
    db.refresh(order)
    return order


def take_to_work_bulk(db: Session, order_ids: list[int], *, new_supply: bool = False) -> list[dict]:
    """Массовое «Взять в работу выбранные» (Этап 3, п.3.2) — одна поставка и
    один вызов WB на каждого клиента. Непригодный заказ (не NEW, проблема)
    отсеивается отдельно и не мешает остальным; сбой WB — на всю группу клиента."""
    errors: dict[int, str | None] = {}
    by_client: dict[int, list[Order]] = {}
    order_ids = list(dict.fromkeys(order_ids))
    for order_id in order_ids:
        order = db.get(Order, order_id)
        if order is None:
            errors[order_id] = f"Заказ #{order_id} не найден."
            continue
        try:
            _check_can_take_to_work(order)
        except AppError as exc:
            errors[order_id] = exc.detail
            continue
        by_client.setdefault(order.client_id, []).append(order)

    for orders in by_client.values():
        ids = [o.id for o in orders]
        client = orders[0].client
        try:
            wb_client = get_wb_client(client)
            _take_orders_to_work(db, client, orders, wb_client, new_supply=new_supply)
            errors.update({oid: None for oid in ids})
        except AppError as exc:
            db.rollback()
            errors.update({oid: exc.detail for oid in ids})

    return [
        {"orderId": oid, "ok": errors.get(oid) is None, "error": errors.get(oid)}
        for oid in order_ids
    ]


def list_orders(
    db: Session, *, client_id: int | None = None, warehouse_id: str | None = None,
    group: str | None = None, sort: str | None = None, limit: int = 100, offset: int = 0,
) -> list[Order]:
    # Новые и «на сборке» по умолчанию — самые старые сверху: за позднюю отгрузку
    # WB штрафует, и срочные заказы не должны теряться внизу списка.
    if sort is None:
        sort = "oldest" if group in ("new", "assembly") else "newest"
    if sort == "oldest":
        order_by = (Order.created_at_wb.asc().nulls_last(), Order.id.asc())
    else:
        order_by = (Order.created_at_wb.desc().nulls_last(), Order.id.desc())
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.client))
        # удалённый клиент скрыт отовсюду (problems.txt, п.5)
        .where(Order.client_id.not_in(deleted_client_ids()))
        .order_by(*order_by)
    )
    if group in ("packed", "archive"):
        stmt = stmt.outerjoin(Supply, Supply.id == Order.supply_id)
    if group == "new":
        stmt = stmt.where(Order.status == OrderStatus.NEW)
    elif group == "assembly":
        stmt = stmt.where(Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY)))
    elif group == "packed":
        # PACKED без поставки (Order.supply_id IS NULL) — тоже "в сборке" (P2-15):
        # `Supply.status == OPEN` на NULL supply_id даёт NULL, а не true, и заказ
        # раньше молча пропадал из обеих вкладок ("Собранные" и "Архив").
        stmt = stmt.where(
            Order.status == OrderStatus.PACKED,
            (Supply.status == SupplyStatus.OPEN) | (Order.supply_id.is_(None)),
        )
    elif group == "archive":
        stmt = stmt.where(
            Order.status.in_(_ARCHIVE_STATUSES)
            | (
                (Order.status == OrderStatus.PACKED)
                & (Order.supply_id.is_not(None))
                & (Supply.status != SupplyStatus.OPEN)
            )
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
        (
            (Order.status == OrderStatus.PACKED)
            & ((Supply.status == SupplyStatus.OPEN) | (Order.supply_id.is_(None))),
            "packed",
        ),
        else_="archive",
    )
    hidden = Order.client_id.not_in(deleted_client_ids())
    stmt = select(group_expr.label("grp"), func.count()).select_from(Order).outerjoin(
        Supply, Supply.id == Order.supply_id
    ).where(hidden)
    problems_stmt = select(func.count()).select_from(Order).where(Order.problem.is_not(None), hidden)
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
