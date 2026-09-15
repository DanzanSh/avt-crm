from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import AppError, NotFoundError
from fulfil.integrations.wb import get_wb_client
from fulfil.models.client import Client
from fulfil.models.product import Product
from fulfil.schemas.product import (
    ProductCreateRequest,
    ProductOut,
    ProductUpdateRequest,
    RevertManualFieldRequest,
)
from fulfil.services import clients as clients_service
from fulfil.services import products as products_service

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(get_current_user)])


def _actor(user: dict) -> str:
    return user.get("sub", "system")


@router.get("", response_model=list[ProductOut])
def list_products(
    search: str | None = None, include_archived: bool = False, client_id: int | None = None,
    name: str | None = None, size: str | None = None, color: str | None = None,
    brand: str | None = None, db: Session = Depends(get_db),
) -> list[Product]:
    return products_service.list_products(
        db, search=search, include_archived=include_archived, client_id=client_id,
        name=name, size=size, color=color, brand=brand,
    )


# Объявлено ДО "/{product_id}": иначе FastAPI разберёт "filter-options" как
# product_id и вернёт 422 вместо списка значений.
@router.get("/filter-options")
def filter_options(
    client_id: int | None = None, include_archived: bool = False, db: Session = Depends(get_db)
) -> dict[str, list[str]]:
    return products_service.filter_options(db, client_id=client_id, include_archived=include_archived)


def _get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Товар #{product_id} не найден.")
    return product


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)) -> Product:
    return _get_product(db, product_id)


@router.post("", response_model=ProductOut)
def create_product(
    body: ProductCreateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> Product:
    clients_service.get_live_client_or_404(db, body.client_id)
    return products_service.create_product(
        db, client_id=body.client_id, barcode=body.barcode, name=body.name, brand=body.brand, size=body.size,
        color=body.color, vendor_code=body.vendor_code, image_url=body.image_url, actor=_actor(user),
    )


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: int, body: ProductUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> Product:
    product = _get_product(db, product_id)
    return products_service.update_product(
        db, product, actor=_actor(user), name=body.name, brand=body.brand, size=body.size,
        color=body.color, vendor_code=body.vendor_code, image_url=body.image_url, barcode=body.barcode,
    )


@router.post("/{product_id}/revert-manual-field", response_model=ProductOut)
def revert_manual_field(
    product_id: int, body: RevertManualFieldRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> Product:
    product = _get_product(db, product_id)
    return products_service.revert_manual_field(db, product, body.field, actor=_actor(user))


@router.delete("/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    product = _get_product(db, product_id)
    outcome = products_service.delete_product(db, product, actor=_actor(user))
    return {"ok": True, "outcome": outcome}


@router.post("/{product_id}/restore", response_model=ProductOut)
def restore_product(product_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> Product:
    product = _get_product(db, product_id)
    return products_service.restore_product(db, product, actor=_actor(user))


@router.post("/sync-from-wb")
def sync_from_wb(client_id: int | None = None, full: bool = False, db: Session = Depends(get_db)) -> dict:
    """Синхронизация карточек WB. client_id не передан — синкает по очереди всех
    активных клиентов с заданным ключом API; ошибка одного клиента (WbApiError) не
    останавливает остальных (Этап 1, п.1.4). full=true — игнорировать сохранённый
    курсор и заново пройти весь каталог."""
    if client_id is not None:
        client = clients_service.get_live_client_or_404(db, client_id)
        targets = [client]
    else:
        targets = list(
            db.scalars(
                select(Client).where(Client.archived_at.is_(None), Client.wb_api_key_enc.is_not(None))
            )
        )

    results = []
    for client in targets:
        try:
            wb_client = get_wb_client(client)
            outcome = products_service.sync_products_from_wb(db, client, wb_client, full=full)
            results.append(
                {"clientId": client.id, "clientName": client.name, "imported": outcome["imported"], "error": None}
            )
        except AppError as exc:  # WbApiError и ошибки расшифровки ключа — не рушат синк остальных
            db.rollback()  # недописанное по этому клиенту не должно уехать с коммитом следующего
            results.append({"clientId": client.id, "clientName": client.name, "imported": 0, "error": exc.detail})
    return {"results": results}


@router.post("/{product_id}/generate-internal-barcode", response_model=ProductOut)
def generate_internal_barcode(
    product_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> Product:
    product = _get_product(db, product_id)
    products_service.generate_internal_barcode(db, product, actor=_actor(user))
    return product
