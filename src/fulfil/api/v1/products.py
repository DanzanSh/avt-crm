from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import NotFoundError
from fulfil.integrations.wb import get_wb_client
from fulfil.models.product import Product
from fulfil.schemas.product import (
    ProductCreateRequest,
    ProductOut,
    ProductUpdateRequest,
    RevertManualFieldRequest,
)
from fulfil.services import products as products_service

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(get_current_user)])


def _actor(user: dict) -> str:
    return user.get("sub", "system")


@router.get("", response_model=list[ProductOut])
def list_products(
    search: str | None = None, include_archived: bool = False, db: Session = Depends(get_db)
) -> list[Product]:
    stmt = select(Product).order_by(Product.id.desc())
    if not include_archived:
        stmt = stmt.where(Product.archived_at.is_(None))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(Product.name.ilike(like) | Product.barcode.ilike(like))
    return list(db.scalars(stmt))


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
    return products_service.create_product(
        db, barcode=body.barcode, name=body.name, brand=body.brand, size=body.size,
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
def sync_from_wb(full: bool = False, db: Session = Depends(get_db)) -> dict:
    """Инкрементальный синк по кнопке. full=true — полная пересинхронизация
    (игнорирует сохранённый курсор)."""
    wb_client = get_wb_client()
    return products_service.sync_products_from_wb(db, wb_client, full=full)


@router.post("/{product_id}/generate-internal-barcode", response_model=ProductOut)
def generate_internal_barcode(product_id: int, db: Session = Depends(get_db)) -> Product:
    product = _get_product(db, product_id)
    products_service.generate_internal_barcode(db, product)
    return product
