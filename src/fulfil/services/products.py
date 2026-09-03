"""Каталог: синхронизация карточек из WB, внутренний ШК, валидация GTIN
(Scope IN п.1; DEV-PLAN.md строки про LDX-000001 и валидацию длин {8,12,13,14}).

Изменение/удаление товара — Scope IN п.4, FEATURES-PLAN.md этап 3. Синхронизация с WB
не перезаписывает поля, изменённые вручную (Product.manual_fields) — иначе правка
молча исчезает при следующем "Синхронизировать с WB".
"""

import datetime as dt
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfil.errors import AppError
from fulfil.integrations.wb.base import WBClient
from fulfil.models import audit
from fulfil.models.product import Product
from fulfil.models.storage import Cell, CellAllowedBarcode

_VALID_GTIN_LENGTHS = {8, 12, 13, 14}
_INTERNAL_BARCODE_RE = re.compile(r"^LDX-\d{6}$")

# Поля, изменяемые через PATCH и подлежащие "заморозке" от синхронизации с WB.
_SIMPLE_PATCH_FIELDS = ("name", "brand", "size", "color", "vendor_code", "image_url")

# Активные статусы заказа — товар из них нельзя удалить/архивировать без разбора.
_ACTIVE_ORDER_STATUSES = {"new", "confirmed", "in_assembly", "packed", "in_supply"}


def validate_gtin(barcode: str) -> bool:
    if not barcode.isdigit():
        return False
    if len(barcode) not in _VALID_GTIN_LENGTHS:
        return False
    if len(barcode) == 14 and barcode.startswith("0"):
        return False  # EAN-13 с лишним ведущим нулём — тот же баг, что в эталоне
    return True


def _validate_any_barcode(barcode: str) -> bool:
    return validate_gtin(barcode) or bool(_INTERNAL_BARCODE_RE.match(barcode))


def generate_internal_barcode(db: Session, product: Product, prefix: str = "LDX") -> str:
    code = f"{prefix}-{product.id:06d}"
    product.barcode = code
    db.commit()
    return code


def sync_products_from_wb(db: Session, wb_client: WBClient) -> int:
    imported = 0
    cursor: str | None = None
    while True:
        page = wb_client.get_product_cards(cursor)
        for card in page["cards"]:
            barcode = card.get("barcode") or ""
            if not barcode:
                continue
            product = db.scalar(
                select(Product).where(Product.barcode == barcode, Product.archived_at.is_(None))
            )
            if product is None:
                product = Product(barcode=barcode, name=card.get("name", ""))
                db.add(product)
                db.flush()

            manual = set(product.manual_fields or [])
            field_values = {
                "name": card.get("name", product.name),
                "brand": card.get("brand"),
                "size": card.get("size"),
                "color": card.get("color"),
                "vendor_code": card.get("vendorCode"),
                "image_url": card.get("imageUrl"),
            }
            for field, value in field_values.items():
                if field in manual:
                    continue  # изменено вручную — синк не перезаписывает (Scope IN п.4)
                setattr(product, field, value)

            product.wb_nm_id = card.get("nmId")
            product.wb_imt_id = card.get("imtId")
            product.synced_at = dt.datetime.now(dt.timezone.utc)
            imported += 1

        cursor = page.get("cursor")
        if not cursor:
            break

    db.commit()
    return imported


def create_product(
    db: Session, *, barcode: str, name: str, brand: str | None = None, size: str | None = None,
    color: str | None = None, vendor_code: str | None = None, image_url: str | None = None,
    actor: str,
) -> Product:
    barcode = barcode.strip()
    if not _validate_any_barcode(barcode):
        raise AppError(
            f'Баркод «{barcode}» некорректен — ожидается GTIN (8/12/13/14 цифр) '
            f'либо внутренний формат LDX-XXXXXX.',
            status_code=400,
            reason_code="invalid_barcode",
        )
    clash = db.scalar(select(Product).where(Product.barcode == barcode, Product.archived_at.is_(None)))
    if clash is not None:
        raise AppError(
            f'Товар с баркодом «{barcode}» уже существует.', status_code=409, reason_code="barcode_exists",
        )

    product = Product(
        barcode=barcode, name=name, brand=brand, size=size, color=color,
        vendor_code=vendor_code, image_url=image_url, manual_fields=list(_SIMPLE_PATCH_FIELDS),
    )
    db.add(product)
    db.flush()
    audit.record(
        db, entity_type="product", entity_id=product.id, action="create", actor=actor,
        changes={"barcode": {"from": None, "to": barcode}, "name": {"from": None, "to": name}},
    )
    db.commit()
    db.refresh(product)
    return product


def _change_barcode(db: Session, product: Product, new_barcode: str) -> tuple[str, str]:
    """Смена баркода: запрещена для товара из карточки WB (источник правды — WB),
    иначе требует валидного GTIN/внутреннего формата и переносит допуски ячеек
    (cell_allowed_barcodes) на новое значение. order_items.barcode не трогаем —
    это снимок того, что прислал WB на момент заказа (FEATURES-PLAN.md, этап 3.2)."""
    if product.wb_nm_id is not None:
        raise AppError(
            "Нельзя менять баркод товара, привязанного к карточке WB — источник правды там.",
            status_code=400,
            reason_code="barcode_locked_by_wb",
        )
    new_barcode = new_barcode.strip()
    if not _validate_any_barcode(new_barcode):
        raise AppError(
            f'Баркод «{new_barcode}» некорректен — ожидается GTIN (8/12/13/14 цифр) '
            f'либо внутренний формат LDX-XXXXXX.',
            status_code=400,
            reason_code="invalid_barcode",
        )
    clash = db.scalar(
        select(Product).where(
            Product.barcode == new_barcode, Product.archived_at.is_(None), Product.id != product.id
        )
    )
    if clash is not None:
        raise AppError(
            f'Баркод «{new_barcode}» уже используется другим товаром.',
            status_code=409,
            reason_code="barcode_exists",
        )

    old_barcode = product.barcode
    product.barcode = new_barcode
    for allowed in db.scalars(select(CellAllowedBarcode).where(CellAllowedBarcode.barcode == old_barcode)):
        allowed.barcode = new_barcode
    return old_barcode, new_barcode


def update_product(
    db: Session, product: Product, *, actor: str, name: str | None = None, brand: str | None = None,
    size: str | None = None, color: str | None = None, vendor_code: str | None = None,
    image_url: str | None = None, barcode: str | None = None,
) -> Product:
    changes: dict = {}
    manual = set(product.manual_fields or [])

    for field, new_value in (
        ("name", name), ("brand", brand), ("size", size), ("color", color),
        ("vendor_code", vendor_code), ("image_url", image_url),
    ):
        if new_value is None:
            continue
        old_value = getattr(product, field)
        if new_value != old_value:
            changes[field] = {"from": old_value, "to": new_value}
            setattr(product, field, new_value)
        manual.add(field)  # поле явно прислано в PATCH — считаем "изменённым вручную"

    if barcode is not None and barcode.strip() != product.barcode:
        old_barcode, new_barcode = _change_barcode(db, product, barcode)
        changes["barcode"] = {"from": old_barcode, "to": new_barcode}
        manual.add("barcode")

    product.manual_fields = sorted(manual)
    if changes:
        audit.record(db, entity_type="product", entity_id=product.id, action="update", actor=actor, changes=changes)
        db.commit()
        db.refresh(product)
    return product


def revert_manual_field(db: Session, product: Product, field: str, actor: str) -> Product:
    """«Вернуть значение WB» — убирает поле из manual_fields; фактическое значение
    восстановится ближайшей синхронизацией (Scope IN п.4)."""
    manual = set(product.manual_fields or [])
    if field in manual:
        manual.discard(field)
        product.manual_fields = sorted(manual)
        audit.record(
            db, entity_type="product", entity_id=product.id, action="update", actor=actor,
            changes={"manualFieldReverted": {"from": field, "to": None}},
        )
        db.commit()
        db.refresh(product)
    return product


def delete_product(db: Session, product: Product, actor: str) -> str:
    """Удаление товара. Возвращает 'archived' или 'deleted' — какое из двух произошло.
    Порядок проверок — первая сработавшая даёт 409 (FEATURES-PLAN.md, этап 3.3)."""
    from fulfil.models.fbs import Order, OrderItem
    from fulfil.models.receiving import ReceiptLine
    from fulfil.models.stock import StockByCell, StockMove

    total_qty = db.scalar(
        select(func.coalesce(func.sum(StockByCell.qty), 0)).where(StockByCell.product_id == product.id)
    ) or 0
    if total_qty > 0:
        row = db.execute(
            select(Cell.address)
            .join(StockByCell, StockByCell.cell_id == Cell.id)
            .where(StockByCell.product_id == product.id, StockByCell.qty > 0)
            .limit(1)
        ).first()
        where = row[0] if row else "?"
        raise AppError(
            f"Сначала спишите или переместите остаток: {total_qty} шт в {where}.",
            status_code=409,
            reason_code="stock_not_empty",
        )

    active_count = db.scalar(
        select(func.count())
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.product_id == product.id, Order.status.in_(_ACTIVE_ORDER_STATUSES))
    ) or 0
    if active_count:
        raise AppError(
            f"Товар в {active_count} активных заказах ФБС — сначала завершите или отмените их.",
            status_code=409,
            reason_code="product_in_active_orders",
        )

    has_history = (
        db.scalar(select(StockMove.id).where(StockMove.product_id == product.id).limit(1)) is not None
        or db.scalar(select(ReceiptLine.id).where(ReceiptLine.product_id == product.id).limit(1)) is not None
        or db.scalar(select(OrderItem.id).where(OrderItem.product_id == product.id).limit(1)) is not None
    )

    if has_history:
        product.archived_at = dt.datetime.now(dt.timezone.utc)
        audit.record(db, entity_type="product", entity_id=product.id, action="delete", actor=actor, comment="archived: есть история")
        db.commit()
        return "archived"

    audit.record(db, entity_type="product", entity_id=product.id, action="delete", actor=actor, comment="physically deleted: истории нет")
    db.commit()
    db.delete(product)
    db.commit()
    return "deleted"


def restore_product(db: Session, product: Product, actor: str) -> Product:
    if product.archived_at is None:
        raise AppError("Товар не архивирован.", status_code=400, reason_code="not_archived")
    clash = db.scalar(
        select(Product).where(
            Product.barcode == product.barcode, Product.archived_at.is_(None), Product.id != product.id
        )
    )
    if clash is not None:
        raise AppError(
            f'Баркод «{product.barcode}» с тех пор занял другой товар — восстановление невозможно.',
            status_code=409,
            reason_code="barcode_taken",
        )
    product.archived_at = None
    audit.record(db, entity_type="product", entity_id=product.id, action="restore", actor=actor)
    db.commit()
    db.refresh(product)
    return product
