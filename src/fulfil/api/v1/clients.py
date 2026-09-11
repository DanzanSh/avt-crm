from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.integrations.wb import get_wb_client
from fulfil.models.client import Client
from fulfil.schemas.client import ClientCreateRequest, ClientOut, ClientUpdateRequest
from fulfil.services import clients as clients_service

router = APIRouter(prefix="/clients", tags=["clients"], dependencies=[Depends(get_current_user)])


def _actor(user: dict) -> str:
    return user.get("sub", "system")


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
def list_clients(include_archived: bool = False, db: Session = Depends(get_db)) -> list[ClientOut]:
    rows = clients_service.list_clients_with_counters(db, include_archived=include_archived)
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
    client = clients_service.get_client_or_404(db, client_id)
    client = clients_service.update_client(
        db, client, name=body.name, api_key=body.api_key, wb_warehouse_id=body.wb_warehouse_id,
        wb_warehouse_name=body.wb_warehouse_name, actor=_actor(user),
    )
    return _client_out(client)


@router.delete("/{client_id}")
def archive_client(client_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    client = clients_service.get_client_or_404(db, client_id)
    clients_service.archive_client(db, client, actor=_actor(user))
    return {"ok": True}


@router.post("/{client_id}/restore", response_model=ClientOut)
def restore_client(client_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> ClientOut:
    client = clients_service.get_client_or_404(db, client_id)
    client = clients_service.restore_client(db, client, actor=_actor(user))
    return _client_out(client)


@router.post("/{client_id}/check")
def check_connection(client_id: int, db: Session = Depends(get_db)) -> dict:
    """«Проверить подключение» — best-effort ping обоих хостов WB API (Этап 1, п.1.4)."""
    client = clients_service.get_client_or_404(db, client_id)
    wb_client = get_wb_client(client)
    return clients_service.check_connection(wb_client)
