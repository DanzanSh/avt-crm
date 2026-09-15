"""Клиенты фулфилмента — несколько кабинетов WB (Этап 1 плана доработок №3, п.2.2).

Один клиент = один продавец со своим ключом API WB и своим складом отгрузки.
Архивация запрещена, пока у клиента есть остаток на складе или активные заказы
ФБС — переиспользуем набор активных статусов из services/products (не заводим
второй список "что считать заказом в работе").
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfil.errors import AppError, NotFoundError
from fulfil.integrations.wb.base import WBClient
from fulfil.models import audit
from fulfil.models.client import Client
from fulfil.models.fbs import Order
from fulfil.models.product import Product
from fulfil.models.stock import StockByCell
from fulfil.secrets import encrypt_api_key, jwt_expires_at
from fulfil.services.products import _ACTIVE_ORDER_STATUSES


def get_client_or_404(db: Session, client_id: int) -> Client:
    client = db.get(Client, client_id)
    if client is None:
        raise NotFoundError(f"Клиент #{client_id} не найден.")
    return client


def get_live_client_or_404(db: Session, client_id: int) -> Client:
    client = get_client_or_404(db, client_id)
    if client.archived_at is not None:
        raise NotFoundError(f"Клиент #{client_id} архивирован.")
    return client


def list_syncable_clients(db: Session) -> list[Client]:
    """Живые клиенты с заданным ключом API и складом — единственный набор условий
    "можно синкать" (P3), раньше скопированный по одному и тому же select()
    в jobs.py, services/orders.py и services/supplies.py по отдельности."""
    return list(
        db.scalars(
            select(Client).where(
                Client.archived_at.is_(None),
                Client.wb_api_key_enc.is_not(None),
                Client.wb_warehouse_id.is_not(None),
            )
        )
    )


def _check_warehouse_change_allowed(db: Session, client: Client) -> None:
    """Смена склада WB при активных заказах обрывает их синхронизацию (P2-16):
    sync_orders_from_wb фильтрует заказы строго по client.wb_warehouse_id, поэтому
    заказы, пришедшие на старый склад, после смены переставали бы находиться."""
    if not client.wb_warehouse_id:
        return
    active_count = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.client_id == client.id, Order.status.in_(_ACTIVE_ORDER_STATUSES))
    ) or 0
    if active_count:
        raise AppError(
            f'У клиента «{client.name}» есть {active_count} активных заказов ФБС на складе '
            f'«{client.wb_warehouse_name or client.wb_warehouse_id}» — сначала завершите или '
            f'отмените их, иначе синхронизация этого склада прекратится.',
            status_code=409,
            reason_code="client_has_active_orders",
        )


def _check_name_unique(db: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(Client).where(Client.name == name, Client.archived_at.is_(None))
    if exclude_id is not None:
        stmt = stmt.where(Client.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise AppError(
            f'Клиент «{name}» уже существует.', status_code=409, reason_code="client_exists",
        )


def create_client(
    db: Session, *, name: str, api_key: str | None = None, wb_warehouse_id: str | None = None,
    actor: str,
) -> Client:
    name = name.strip()
    if not name:
        raise AppError("Наименование клиента не может быть пустым.", status_code=400, reason_code="invalid_name")
    _check_name_unique(db, name)

    client = Client(name=name, wb_warehouse_id=wb_warehouse_id or None)
    if api_key:
        client.wb_api_key_enc = encrypt_api_key(api_key)
        client.wb_token_expires_at = jwt_expires_at(api_key)
    db.add(client)
    db.flush()
    audit.record(
        db, entity_type="client", entity_id=client.id, action="create", actor=actor,
        changes={"name": {"from": None, "to": name}},
    )
    db.commit()
    db.refresh(client)
    return client


def update_client(
    db: Session, client: Client, *, name: str | None = None, api_key: str | None = None,
    wb_warehouse_id: str | None = None, wb_warehouse_name: str | None = None, actor: str,
) -> Client:
    """api_key: пусто/None — не менять (Этап 1, п.1.4). Значение ключа никогда не
    попадает в audit_log — пишем только факт смены."""
    changes: dict = {}
    if name is not None:
        name = name.strip()
        if name and name != client.name:
            _check_name_unique(db, name, exclude_id=client.id)
            changes["name"] = {"from": client.name, "to": name}
            client.name = name

    if api_key:
        client.wb_api_key_enc = encrypt_api_key(api_key)
        client.wb_token_expires_at = jwt_expires_at(api_key)
        changes["apiKey"] = {"from": "***", "to": "***"}

    if wb_warehouse_id is not None and wb_warehouse_id != client.wb_warehouse_id:
        _check_warehouse_change_allowed(db, client)
        changes["wbWarehouseId"] = {"from": client.wb_warehouse_id, "to": wb_warehouse_id}
        client.wb_warehouse_id = wb_warehouse_id or None

    if wb_warehouse_name is not None and wb_warehouse_name != client.wb_warehouse_name:
        changes["wbWarehouseName"] = {"from": client.wb_warehouse_name, "to": wb_warehouse_name}
        client.wb_warehouse_name = wb_warehouse_name or None

    if changes:
        audit.record(db, entity_type="client", entity_id=client.id, action="update", actor=actor, changes=changes)
        db.commit()
        db.refresh(client)
    return client


def archive_client(db: Session, client: Client, actor: str) -> Client:
    total_qty = db.scalar(
        select(func.coalesce(func.sum(StockByCell.qty), 0))
        .select_from(StockByCell)
        .join(Product, Product.id == StockByCell.product_id)
        .where(Product.client_id == client.id)
    ) or 0
    if total_qty > 0:
        raise AppError(
            f'У клиента «{client.name}» есть остаток на складе ({total_qty} шт) — '
            f'сначала спишите или переместите его.',
            status_code=409,
            reason_code="client_has_stock",
        )

    active_count = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.client_id == client.id, Order.status.in_(_ACTIVE_ORDER_STATUSES))
    ) or 0
    if active_count:
        raise AppError(
            f'У клиента «{client.name}» есть {active_count} активных заказов ФБС — '
            f'сначала завершите или отмените их.',
            status_code=409,
            reason_code="client_has_active_orders",
        )

    client.archived_at = dt.datetime.now(dt.timezone.utc)
    audit.record(db, entity_type="client", entity_id=client.id, action="delete", actor=actor)
    db.commit()
    db.refresh(client)
    return client


def restore_client(db: Session, client: Client, actor: str) -> Client:
    if client.archived_at is None:
        raise AppError("Клиент не архивирован.", status_code=400, reason_code="not_archived")
    _check_name_unique(db, client.name, exclude_id=client.id)
    client.archived_at = None
    audit.record(db, entity_type="client", entity_id=client.id, action="restore", actor=actor)
    db.commit()
    db.refresh(client)
    return client


def key_status(client: Client) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = client.wb_token_expires_at
    return {
        "hasApiKey": bool(client.wb_api_key_enc),
        "wbTokenExpiresAt": expires_at,
        "wbTokenExpiringSoon": bool(expires_at and dt.timedelta() < expires_at - now < dt.timedelta(days=14)),
        "wbTokenExpired": bool(expires_at and expires_at < now),
    }


def list_clients_with_counters(db: Session, *, include_archived: bool = False) -> list[dict]:
    """Список клиентов со счётчиками — SKU живых товаров, штук на складе, активных
    заказов. Три агрегирующих запроса на весь список (без N+1 по клиентам)."""
    stmt = select(Client).order_by(Client.name)
    if not include_archived:
        stmt = stmt.where(Client.archived_at.is_(None))
    clients = list(db.scalars(stmt))
    if not clients:
        return []
    ids = [c.id for c in clients]

    sku_counts = dict(
        db.execute(
            select(Product.client_id, func.count())
            .where(Product.client_id.in_(ids), Product.archived_at.is_(None))
            .group_by(Product.client_id)
        ).all()
    )
    stock_qty = dict(
        db.execute(
            select(Product.client_id, func.coalesce(func.sum(StockByCell.qty), 0))
            .select_from(StockByCell)
            .join(Product, Product.id == StockByCell.product_id)
            .where(Product.client_id.in_(ids))
            .group_by(Product.client_id)
        ).all()
    )
    active_orders = dict(
        db.execute(
            select(Order.client_id, func.count())
            .where(Order.client_id.in_(ids), Order.status.in_(_ACTIVE_ORDER_STATUSES))
            .group_by(Order.client_id)
        ).all()
    )

    return [
        {
            "client": c,
            "skuCount": sku_counts.get(c.id, 0),
            "stockQty": int(stock_qty.get(c.id, 0) or 0),
            "activeOrders": active_orders.get(c.id, 0),
            **key_status(c),
        }
        for c in clients
    ]


def check_connection(wb_client: WBClient) -> dict:
    return wb_client.ping()


# --- Склад WB клиента (Этап 2 плана №3, п.2.2) -------------------------------
# «Оба варианта — выбрать существующий из списка или создать через API» —
# согласовано с заказчиком (см. шапку план-доработок-3.md). Свободный ввод
# ID/названия склада убран из формы клиента: он давал опечатки, которые
# всплывали только при первой реальной передаче остатка.


def list_wb_offices(wb_client: WBClient) -> list[dict]:
    return wb_client.list_offices()


def list_wb_warehouses(wb_client: WBClient) -> list[dict]:
    return wb_client.list_warehouses()


def create_wb_warehouse(
    db: Session, client: Client, wb_client: WBClient, *, name: str, office_id: int, actor: str,
) -> Client:
    """Создаёт склад в WB и сразу привязывает его к клиенту — раздельного «создать,
    а потом выбрать из списка» шага в интерфейсе нет, это одно действие."""
    name = name.strip()
    if not name:
        raise AppError("Название склада не может быть пустым.", status_code=400, reason_code="invalid_name")
    _check_warehouse_change_allowed(db, client)

    warehouse = wb_client.create_warehouse(name, office_id)
    changes = {
        "wbWarehouseId": {"from": client.wb_warehouse_id, "to": warehouse["id"]},
        "wbWarehouseName": {"from": client.wb_warehouse_name, "to": warehouse["name"]},
    }
    client.wb_warehouse_id = warehouse["id"]
    client.wb_warehouse_name = warehouse["name"]
    audit.record(db, entity_type="client", entity_id=client.id, action="update", actor=actor, changes=changes)
    db.commit()
    db.refresh(client)
    return client


def set_wb_warehouse(
    db: Session, client: Client, *, warehouse_id: str, warehouse_name: str, actor: str,
) -> Client:
    """Привязывает УЖЕ существующий в WB склад (выбор из GET .../wb-warehouses) —
    в отличие от create_wb_warehouse, ничего не создаёт на стороне WB."""
    _check_warehouse_change_allowed(db, client)
    changes = {
        "wbWarehouseId": {"from": client.wb_warehouse_id, "to": warehouse_id},
        "wbWarehouseName": {"from": client.wb_warehouse_name, "to": warehouse_name},
    }
    client.wb_warehouse_id = warehouse_id
    client.wb_warehouse_name = warehouse_name
    audit.record(db, entity_type="client", entity_id=client.id, action="update", actor=actor, changes=changes)
    db.commit()
    db.refresh(client)
    return client
