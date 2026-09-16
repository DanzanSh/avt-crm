from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user, is_host, require_host
from fulfil.errors import AppError, NotFoundError
from fulfil.db import get_db
from fulfil.integrations.wb import get_wb_client
from fulfil.models.client import Client
from fulfil.schemas.client import (
    ClientCreateRequest,
    ClientOut,
    ClientUpdateRequest,
    CreateWbWarehouseRequest,
    SetWbWarehouseRequest,
)
from fulfil.services import clients as clients_service

router = APIRouter(prefix="/clients", tags=["clients"], dependencies=[Depends(get_current_user)])


def _actor(user: dict) -> str:
    return user.get("sub", "system")


def _get_client(db: Session, client_id: int, user: dict) -> Client:
    """Удалённый клиент для всех, кроме хоста, — как будто его нет (problems.txt, п.5)."""
    client = clients_service.get_client_or_404(db, client_id)
    if client.deleted_at is not None and not is_host(user):
        raise NotFoundError(f"Клиент #{client_id} не найден.")
    return client


def _client_out(client: Client, counters: dict | None = None) -> ClientOut:
    status = clients_service.key_status(client)
    data = {
        "id": client.id,
        "name": client.name,
        "wb_warehouse_id": client.wb_warehouse_id,
        "wb_warehouse_name": client.wb_warehouse_name,
        "last_sync_at": client.last_sync_at,
        "last_sync_error": client.last_sync_error,
        "archived_at": client.archived_at,
        "deleted_at": client.deleted_at,
        "deleted_by": client.deleted_by,
        **status,
    }
    if counters:
        data.update(
            sku_count=counters.get("skuCount", 0),
            stock_qty=counters.get("stockQty", 0),
            active_orders=counters.get("activeOrders", 0),
        )
    return ClientOut.model_validate(data)


@router.get("", response_model=list[ClientOut])
def list_clients(
    state: str | None = None, include_archived: bool = False, db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> list[ClientOut]:
    """state=live|archived|deleted|all; include_archived=true — прежний алиас state=archived."""
    state = state or ("archived" if include_archived else "live")
    if state not in clients_service.CLIENT_STATES:
        raise AppError(f"Неизвестный state «{state}».", status_code=400, reason_code="invalid_state")
    if state in ("deleted", "all") and not is_host(user):
        raise AppError(
            "Удалённых клиентов видит только владелец системы.", status_code=403, reason_code="forbidden",
        )
    rows = clients_service.list_clients_with_counters(db, state=state)
    return [
        _client_out(
            row["client"],
            {"skuCount": row["skuCount"], "stockQty": row["stockQty"], "activeOrders": row["activeOrders"]},
        )
        for row in rows
    ]


@router.post("", response_model=ClientOut)
def create_client(
    body: ClientCreateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> ClientOut:
    client = clients_service.create_client(
        db, name=body.name, api_key=body.api_key, wb_warehouse_id=body.wb_warehouse_id, actor=_actor(user),
    )
    return _client_out(client)


@router.patch("/{client_id}", response_model=ClientOut)
def update_client(
    client_id: int, body: ClientUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> ClientOut:
    client = _get_client(db, client_id, user)
    client = clients_service.update_client(
        db, client, name=body.name, api_key=body.api_key, wb_warehouse_id=body.wb_warehouse_id,
        wb_warehouse_name=body.wb_warehouse_name, actor=_actor(user),
    )
    return _client_out(client)


@router.delete("/{client_id}")
def archive_client(client_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    """Архивация — любой вошедший. Вернуть из архива может только хост (problems.txt, п.5)."""
    client = _get_client(db, client_id, user)
    clients_service.archive_client(db, client, actor=_actor(user))
    return {"ok": True}


@router.post("/{client_id}/restore", response_model=ClientOut)
def restore_client(client_id: int, db: Session = Depends(get_db), user: dict = Depends(require_host)) -> ClientOut:
    client = clients_service.get_client_or_404(db, client_id)
    client = clients_service.restore_client(db, client, actor=_actor(user))
    return _client_out(client)


@router.post("/{client_id}/delete", response_model=ClientOut)
def delete_client(client_id: int, db: Session = Depends(get_db), user: dict = Depends(require_host)) -> ClientOut:
    """Мягкое удаление — только хост: клиент скрыт от сотрудников, история остаётся."""
    client = clients_service.get_client_or_404(db, client_id)
    client = clients_service.delete_client(db, client, actor=_actor(user))
    return _client_out(client)


@router.delete("/{client_id}/purge")
def purge_client(client_id: int, db: Session = Depends(get_db), user: dict = Depends(require_host)) -> dict:
    """Безвозвратно — только хост и только у клиента без истории (иначе 409)."""
    client = clients_service.get_client_or_404(db, client_id)
    clients_service.purge_client(db, client, actor=_actor(user))
    return {"ok": True, "outcome": "deleted"}


@router.post("/{client_id}/check")
def check_connection(
    client_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> dict:
    """«Проверить подключение» — best-effort ping обоих хостов WB API (Этап 1, п.1.4)."""
    client = _get_client(db, client_id, user)
    wb_client = get_wb_client(client)
    return clients_service.check_connection(wb_client)


# --- Склад WB клиента (Этап 2, п.2.2) ---------------------------------------


@router.get("/{client_id}/wb-offices")
def wb_offices(client_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """Пункты приёма продавца — нужны только для создания НОВОГО склада."""
    client = clients_service.get_live_client_or_404(db, client_id)
    wb_client = get_wb_client(client)
    return clients_service.list_wb_offices(wb_client)


@router.get("/{client_id}/wb-warehouses")
def wb_warehouses(client_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """Склады FBS, уже существующие у продавца в WB — для варианта «выбрать
    существующий» (Этап 2, п.2.2)."""
    client = clients_service.get_live_client_or_404(db, client_id)
    wb_client = get_wb_client(client)
    return clients_service.list_wb_warehouses(wb_client)


@router.post("/{client_id}/wb-warehouses/select", response_model=ClientOut)
def select_wb_warehouse(
    client_id: int, body: SetWbWarehouseRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> ClientOut:
    client = clients_service.get_live_client_or_404(db, client_id)
    client = clients_service.set_wb_warehouse(
        db, client, warehouse_id=body.warehouse_id, warehouse_name=body.warehouse_name, actor=_actor(user),
    )
    return _client_out(client)


@router.post("/{client_id}/wb-warehouses", response_model=ClientOut)
def create_wb_warehouse(
    client_id: int, body: CreateWbWarehouseRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> ClientOut:
    client = clients_service.get_live_client_or_404(db, client_id)
    wb_client = get_wb_client(client)
    client = clients_service.create_wb_warehouse(
        db, client, wb_client, name=body.name, office_id=body.office_id, actor=_actor(user),
    )
    return _client_out(client)
