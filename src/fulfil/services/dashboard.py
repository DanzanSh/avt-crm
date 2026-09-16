"""Сводки на «Товарах» и дашборд (Этап 6 плана №3, пп.2.3, 2.4, 5).

Каждая сводка переиспользует уже существующие агрегаты этапов 2-5 — счётчики
заказов/поставок/приёмок берутся из services.orders/supplies/receiving, остаток
из services.stock/services.clients — а не заводит параллельный набор запросов.
Здесь только то, чего раньше не было: топ клиентов по остаткам, заполненность
карты склада по секциям и раздел «Требует внимания».
"""

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from fulfil.models.client import Client
from fulfil.models.fbs import Order
from fulfil.models.product import Product
from fulfil.models.storage import Cell, CellStatus, Zone
from fulfil.models.stock import StockByCell
from fulfil.schemas.fbs import OrderCountersOut, SupplyCountersOut
from fulfil.schemas.receiving import ReceiptCountersOut
from fulfil.services import clients as clients_service
from fulfil.services import orders as orders_service
from fulfil.services import receiving as receiving_service
from fulfil.services import supplies as supplies_service
from fulfil.services.stock import list_stock_summaries

_TOP_CLIENTS_LIMIT = 3


def stock_totals(db: Session, *, client_id: int | None = None) -> dict:
    """«Всего на складе» и «артикулов с остатком» — с учётом фильтра клиента
    (п.6.2). Заполненность мест (cellsOccupied/cellsTotal) фильтру не подчиняется:
    секции не закреплены за клиентами (Этап 1) — ячейка физически существует
    независимо от того, чей товар сейчас в ней лежит.

    Считается через list_stock_summaries() (Этап 2), а не отдельным SUM по
    stock_by_cell: строки с qty=0 там не удаляются (см. stock_ledger.apply_move),
    и голый COUNT(DISTINCT product_id) насчитал бы лишние товары без остатка."""
    summaries = list_stock_summaries(db, client_id=client_id)
    total_qty = sum(s["total"] for s in summaries.values())
    sku_with_stock = sum(1 for s in summaries.values() if s["total"] > 0)

    cells_total = db.scalar(select(func.count()).select_from(Cell).where(Cell.deleted_at.is_(None))) or 0
    cells_occupied = db.scalar(
        select(func.count())
        .select_from(Cell)
        .where(Cell.deleted_at.is_(None), Cell.status == CellStatus.OCCUPIED)
    ) or 0

    return {
        "totalQty": int(total_qty),
        "skuWithStock": sku_with_stock,
        "cellsOccupied": cells_occupied,
        "cellsTotal": cells_total,
        "cellsFillPercent": round(cells_occupied / cells_total * 100, 1) if cells_total else 0.0,
    }


def top_clients_by_stock(db: Session, *, limit: int = _TOP_CLIENTS_LIMIT) -> list[dict]:
    """Топ клиентов по штукам на складе — ВСЕГДА по всем клиентам (согласовано
    с заказчиком, см. шапку план-доработок-3.md), фильтр «Товаров» его не сужает."""
    rows = db.execute(
        select(Client.id, Client.name, func.sum(StockByCell.qty).label("qty"))
        .select_from(StockByCell)
        .join(Product, Product.id == StockByCell.product_id)
        .join(Client, Client.id == Product.client_id)
        .group_by(Client.id, Client.name)
        .having(func.sum(StockByCell.qty) > 0)
        .order_by(func.sum(StockByCell.qty).desc())
        .limit(limit)
    ).all()
    return [{"clientId": r.id, "clientName": r.name, "qty": int(r.qty)} for r in rows]


def zone_fill(db: Session) -> list[dict]:
    """Заполненность мест по секциям — та же пара (занято/всего), что уже
    считает клиент в сводной полосе карты склада (Этап 0, п.0.3), только на
    бэкенде и по всем секциям сразу (для дашборда, без похода за cell-map целиком)."""
    zones = list(db.scalars(select(Zone).where(Zone.deleted_at.is_(None)).order_by(Zone.position, Zone.id)))
    if not zones:
        return []
    codes = [z.code for z in zones]
    rows = db.execute(
        select(
            Cell.zone_code,
            func.count().label("total"),
            func.sum(case((Cell.status == CellStatus.OCCUPIED, 1), else_=0)).label("occupied"),
        )
        .where(Cell.zone_code.in_(codes), Cell.deleted_at.is_(None))
        .group_by(Cell.zone_code)
    ).all()
    by_code = {r.zone_code: (r.total, int(r.occupied or 0)) for r in rows}
    return [
        {
            "zoneCode": z.code,
            "zoneName": z.name,
            "total": by_code.get(z.code, (0, 0))[0],
            "occupied": by_code.get(z.code, (0, 0))[1],
        }
        for z in zones
    ]


def _attention_items(db: Session) -> list[dict]:
    """«Требует внимания» (п.6.3.4) — сквозь всех клиентов, фильтр «Товаров»/
    дашборда сюда не применяется: это список того, что нужно разобрать вручную,
    а не срез по одному кабинету."""
    items: list[dict] = []

    problems = db.scalar(select(func.count()).select_from(Order).where(Order.problem.is_not(None))) or 0
    if problems:
        items.append(
            {
                "type": "unknown_sku",
                "message": f"Заказов с нераспознанным товаром: {problems}",
                "link": "/fbs/orders.html",
            }
        )

    for row in clients_service.list_clients_with_counters(db):
        client: Client = row["client"]
        if client.last_sync_error:
            items.append(
                {
                    "type": "sync_error",
                    "message": f'«{client.name}»: ошибка синхронизации — {client.last_sync_error}',
                    "link": "/clients.html",
                }
            )
        if row["wbTokenExpired"]:
            items.append(
                {"type": "key_expired", "message": f'«{client.name}»: ключ API истёк', "link": "/clients.html"}
            )
        elif row["wbTokenExpiringSoon"]:
            items.append(
                {
                    "type": "key_expiring",
                    "message": f'«{client.name}»: ключ API скоро истекает',
                    "link": "/clients.html",
                }
            )

    summaries = list_stock_summaries(db)
    oversold = sum(1 for s in summaries.values() if s["fbsOversold"] > 0)
    if oversold:
        items.append(
            {
                "type": "fbs_oversold",
                "message": f"Товаров с перепроданным остатком на WB: {oversold}",
                "link": "/stock.html",
            }
        )

    blocked_cells = db.scalar(
        select(func.count()).select_from(Cell).where(Cell.deleted_at.is_(None), Cell.status == CellStatus.BLOCKED)
    ) or 0
    if blocked_cells:
        items.append(
            {
                "type": "blocked_cells",
                "message": f"Заблокировано мест на складе: {blocked_cells}",
                "link": "/storage-map.html",
            }
        )

    return items


def get_dashboard_summary(db: Session, *, client_id: int | None = None) -> dict:
    return {
        "stockTotals": stock_totals(db, client_id=client_id),
        "topClientsByStock": top_clients_by_stock(db),
        "zoneFill": zone_fill(db),
        "orders": OrderCountersOut.model_validate(
            orders_service.get_order_counters(db, client_id=client_id)
        ).model_dump(by_alias=True),
        "supplies": SupplyCountersOut.model_validate(
            supplies_service.get_supply_counters(db, client_id=client_id)
        ).model_dump(by_alias=True),
        "receipts": ReceiptCountersOut.model_validate(
            receiving_service.get_receipt_counters(db, client_id=client_id)
        ).model_dump(by_alias=True),
        "attention": _attention_items(db),
    }
