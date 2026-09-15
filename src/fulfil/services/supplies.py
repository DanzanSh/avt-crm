"""Поставки ФБС (Этап 4 плана №3, п.6 problems.txt): статус выводится из полей
WB (done/scanDt), а не назначается произвольно в разных местах кода; короба
и QR — доступны только после передачи в доставку (Scope IN п.12).

QR поставки WB выдаёт только после её закрытия — этот порядок закладываем в API,
а не притворяемся, что можем получить QR когда захотим (см. DEV-PLAN.md).
"""

import datetime as dt
import logging

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from fulfil.errors import AppError, WbApiError
from fulfil.integrations.wb import get_wb_client
from fulfil.integrations.wb.base import WBClient
from fulfil.models.client import Client
from fulfil.models.fbs import Order, OrderStatus, Supply, SupplyBox, SupplyStatus

logger = logging.getLogger(__name__)

# Заказы поставки, которые ещё не прошли сборку — закрыть (передать в доставку)
# поставку с такими заказами нельзя: WB не даст собрать их отдельно после этого.
_UNASSEMBLED_ORDER_STATUSES = (OrderStatus.CONFIRMED, OrderStatus.IN_ASSEMBLY)

# «Прочие» на вкладках фронта (Этап 4, п.4.3) — legacy CLOSED (поставки, заведённые
# до этого этапа) и явные сбои.
_OTHER_STATUSES = (SupplyStatus.CLOSED, SupplyStatus.PARTIAL, SupplyStatus.FAILED, SupplyStatus.STALE)


def _parse_wb_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _status_from_wb(done: bool, scan_dt: dt.datetime | None) -> SupplyStatus:
    """done=false — «На сборке», done=true без scanDt — «В доставке», scanDt
    заполнен — «Принята» (Этап 4, п.4.1 плана №3)."""
    if scan_dt is not None:
        return SupplyStatus.ACCEPTED
    if done:
        return SupplyStatus.IN_DELIVERY
    return SupplyStatus.OPEN


def create_supply(db: Session, client: Client, wb_client: WBClient, *, name: str | None = None) -> Supply:
    if name is None:
        name = f"{client.name} {dt.date.today().isoformat()}"
    wb_supply_id = wb_client.create_supply(name)
    supply = Supply(client_id=client.id, wb_supply_id=wb_supply_id, name=name, status=SupplyStatus.OPEN)
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
    if order.status == OrderStatus.NEW:
        # Добавление в поставку — это и есть подтверждение заказа на WB (Этап 3,
        # п.3.2, problems.txt №3): раньше здесь стоял IN_SUPPLY, статус, до которого
        # в новой модели дело не доходит — подтверждённый заказ идёт в сборку
        # (CONFIRMED/IN_ASSEMBLY -> confirm-assembly -> PACKED), а не «в поставку».
        order.status = OrderStatus.CONFIRMED
    db.commit()


def close_supply(db: Session, supply: Supply, wb_client: WBClient) -> Supply:
    """Необратимо на стороне WB: передаёт поставку в доставку (PATCH
    .../deliver). Отказывает 409, если в поставке остались несобранные
    заказы (Этап 4, п.4.2) — иначе они бы «уехали» на WB несобранными и
    собрать их после этого было бы уже нельзя."""
    if supply.status != SupplyStatus.OPEN:
        raise AppError(
            f'Поставка в статусе "{supply.status.value}" — закрыть можно только открытую поставку.',
            status_code=409,
            reason_code="wrong_status",
        )
    unassembled = list(
        db.scalars(
            select(Order).where(Order.supply_id == supply.id, Order.status.in_(_UNASSEMBLED_ORDER_STATUSES))
        )
    )
    if unassembled:
        raise AppError(
            "В поставке есть несобранные заказы — сначала соберите их.",
            status_code=409,
            reason_code="unassembled_orders",
            what_to_do="Соберите оставшиеся заказы («Сборка») либо уберите их из поставки в личном кабинете WB.",
            extra={"orders": [{"id": o.id, "wbOrderId": o.wb_order_id} for o in unassembled]},
        )

    resp = wb_client.close_supply(supply.wb_supply_id)
    if resp.get("ok"):
        now = dt.datetime.now(dt.timezone.utc)
        supply.wb_done = True
        supply.status = SupplyStatus.IN_DELIVERY
        supply.closed_at = now
        supply.closed_at_wb = now
        packed = list(
            db.scalars(select(Order).where(Order.supply_id == supply.id, Order.status == OrderStatus.PACKED))
        )
        for order in packed:
            order.status = OrderStatus.SHIPPED
    else:
        supply.status = SupplyStatus.FAILED
    db.commit()
    db.refresh(supply)
    return supply


# Статусы, после которых WB отдаёт QR/короба поставки — «закрыто» в новой модели
# означает «передано в доставку» (IN_DELIVERY) или дальше (ACCEPTED); CLOSED/
# PARTIAL — терминальные статусы до Этапа 4, оставлены ради уже существующих
# записей (иначе старая поставка внезапно потеряла бы доступ к своему QR).
_QR_AVAILABLE_STATUSES = (
    SupplyStatus.IN_DELIVERY, SupplyStatus.ACCEPTED, SupplyStatus.CLOSED, SupplyStatus.PARTIAL,
)


def get_supply_qr(db: Session, supply: Supply, wb_client: WBClient) -> dict:
    if supply.status not in _QR_AVAILABLE_STATUSES:
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
    if supply.status not in _QR_AVAILABLE_STATUSES:
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


def sync_supplies(db: Session, client: Client, wb_client: WBClient) -> dict:
    """Постранично проходит поставки продавца (Этап 4, п.4.2) и апдейтит статус
    уже известных нам поставок по полям WB. Импортирует только поставки,
    созданные нашей системой (уже есть локальная запись по wb_supply_id), и
    чужие поставки, в которых есть хотя бы один НАШ заказ — продавец мог
    собрать поставку в личном кабинете WB, минуя Fulfil. Поставки других
    складов селлера, где наших заказов нет, не трогаются."""
    known_ids = set(
        db.scalars(
            select(Supply.wb_supply_id).where(
                Supply.client_id == client.id, Supply.wb_supply_id.is_not(None)
            )
        )
    )
    our_order_ids = set(db.scalars(select(Order.wb_order_id).where(Order.client_id == client.id)))

    imported = 0
    updated = 0
    cursor: int | None = None
    for _ in range(500):  # защита от зацикливания, если WB вернёт мусорный next
        page = wb_client.list_supplies(cursor)
        for s in page.get("supplies", []):
            wb_id = str(s["id"])
            is_ours = wb_id in known_ids
            if not is_ours:
                try:
                    order_ids = set(wb_client.get_supply_orders(wb_id))
                except WbApiError:
                    # На части кабинетов/поставок GET .../{id}/orders отвечает
                    # 404 "path not found" (проверено на реальном токене —
                    # см. docs/wb-api-contract.md) — WB не даёт заглянуть внутрь
                    # этой конкретной поставки, значит понять, "наша" ли она,
                    # нечем. Пропускаем именно эту поставку-кандидата, а не
                    # весь синк: уже известные нам поставки в этом же вызове
                    # (known_ids) такой проверки не проходят и не страдают.
                    logger.debug(
                        "sync_supplies: не удалось прочитать заказы поставки %s клиента %s — пропущена",
                        wb_id, client.id,
                    )
                    continue
                is_ours = bool(order_ids & our_order_ids)
            if not is_ours:
                continue

            supply = db.scalar(select(Supply).where(Supply.wb_supply_id == wb_id))
            if supply is None:
                supply = Supply(client_id=client.id, wb_supply_id=wb_id, status=SupplyStatus.OPEN)
                db.add(supply)
                known_ids.add(wb_id)
                imported += 1
            else:
                updated += 1

            supply.name = s.get("name") or supply.name
            supply.wb_done = bool(s.get("done"))
            supply.created_at_wb = _parse_wb_dt(s.get("createdAt"))
            supply.closed_at_wb = _parse_wb_dt(s.get("closedAt"))
            supply.scan_dt = _parse_wb_dt(s.get("scanDt"))
            if supply.status not in (SupplyStatus.FAILED, SupplyStatus.STALE):
                supply.status = _status_from_wb(supply.wb_done, supply.scan_dt)

        cursor = page.get("next")
        if not cursor:
            break

    client.last_sync_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return {"imported": imported, "updated": updated}


def sync_supplies_all(db: Session) -> list[dict]:
    """По всем активным клиентам со складом (Этап 4, п.4.2) — как
    orders_service.sync_orders: используется и кнопкой «Обновить из WB», и
    фоновым опросом (jobs.py). Ошибка одного клиента не останавливает остальных."""
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
            stats = sync_supplies(db, client, wb_client)
            results.append({"clientId": client.id, "clientName": client.name, "error": None, **stats})
        except AppError as exc:
            db.rollback()
            client.last_sync_error = exc.detail
            db.commit()
            results.append(
                {"clientId": client.id, "clientName": client.name, "imported": 0, "updated": 0, "error": exc.detail}
            )
    return results


_GROUP_STATUSES = {
    "assembly": (SupplyStatus.OPEN,),
    "in_delivery": (SupplyStatus.IN_DELIVERY,),
    "accepted": (SupplyStatus.ACCEPTED,),
    "other": _OTHER_STATUSES,
}


def list_supplies(db: Session, *, client_id: int | None = None, group: str | None = None) -> list[Supply]:
    stmt = (
        select(Supply)
        .options(selectinload(Supply.client), selectinload(Supply.orders))
        .order_by(Supply.id.desc())
    )
    if client_id is not None:
        stmt = stmt.where(Supply.client_id == client_id)
    if group in _GROUP_STATUSES:
        stmt = stmt.where(Supply.status.in_(_GROUP_STATUSES[group]))
    return list(db.scalars(stmt))


def get_supply_counters(db: Session, *, client_id: int | None = None) -> dict:
    """{assembly, in_delivery, accepted, other} — Этап 4, п.4.3, один GROUP BY."""
    group_expr = case(
        (Supply.status == SupplyStatus.OPEN, "assembly"),
        (Supply.status == SupplyStatus.IN_DELIVERY, "in_delivery"),
        (Supply.status == SupplyStatus.ACCEPTED, "accepted"),
        else_="other",
    )
    stmt = select(group_expr.label("grp"), func.count()).select_from(Supply)
    if client_id is not None:
        stmt = stmt.where(Supply.client_id == client_id)
    stmt = stmt.group_by(group_expr)
    counts = dict(db.execute(stmt).all())
    return {
        "assembly": counts.get("assembly", 0),
        "in_delivery": counts.get("in_delivery", 0),
        "accepted": counts.get("accepted", 0),
        "other": counts.get("other", 0),
    }
