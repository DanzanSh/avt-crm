"""Каталог: синхронизация карточек из WB, внутренний ШК, валидация GTIN
(Scope IN п.1; DEV-PLAN.md строки про LDX-000001 и валидацию длин {8,12,13,14}).

Изменение/удаление товара — Scope IN п.4, FEATURES-PLAN.md этап 3. Синхронизация с WB
не перезаписывает поля, изменённые вручную (Product.manual_fields) — иначе правка
молча исчезает при следующем "Синхронизировать с WB".
"""

import datetime as dt
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from fulfil.errors import AppError
from fulfil.integrations.wb.base import WBClient
from fulfil.models import audit
from fulfil.models.client import Client, deleted_client_ids
from fulfil.models.integration_state import IntegrationState
from fulfil.models.product import Product
from fulfil.models.storage import Cell, CellAllowedBarcode

_WB_STATE_KEY_PREFIX = "wb.product_cards"
_WB_PAGE_LIMIT = 100


def _wb_state_key(client_id: int) -> str:
    # Курсор синка — на каждого клиента отдельно (Этап 1, п.1.1): у каждого кабинета
    # WB своя история updatedAt/nmID, общий курсор перепутал бы их между собой.
    return f"{_WB_STATE_KEY_PREFIX}:{client_id}"


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


def generate_internal_barcode(db: Session, product: Product, actor: str, prefix: str = "LDX") -> str:
    """Генерирует внутренний ШК для товара без баркода WB (P3): раньше не проверяла
    ни привязку к карточке WB, ни уникальность результата в каталоге клиента, и не
    писала факт правки в аудит — в отличие от остальных изменений баркода
    (_change_barcode)."""
    if product.wb_nm_id is not None:
        raise AppError(
            "Нельзя сгенерировать внутренний ШК — товар привязан к карточке WB, источник правды там.",
            status_code=400,
            reason_code="barcode_locked_by_wb",
        )
    code = f"{prefix}-{product.id:06d}"
    clash = db.scalar(
        select(Product).where(
            Product.client_id == product.client_id, Product.barcode == code,
            Product.archived_at.is_(None), Product.id != product.id,
        )
    )
    if clash is not None:
        raise AppError(
            f'Баркод «{code}» уже используется другим товаром этого клиента.',
            status_code=409,
            reason_code="barcode_exists",
        )

    old_barcode = product.barcode
    product.barcode = code
    manual = set(product.manual_fields or [])
    manual.add("barcode")
    product.manual_fields = sorted(manual)
    audit.record(
        db, entity_type="product", entity_id=product.id, action="update", actor=actor,
        changes={"barcode": {"from": old_barcode, "to": code}},
    )
    db.commit()
    return code


def _upsert_card(db: Session, client: Client, card: dict) -> bool:
    """Апсёрт одного размера карточки WB (Этап 1: товар = размер карточки, п.1.1).
    Порядок матчинга — В ПРЕДЕЛАХ КЛИЕНТА: сперва по (client_id, wb_chrt_id), затем
    по (client_id, barcode) — уже существующие до этапа товары ещё не имеют chrt_id
    и находятся по баркоду, иначе создать новый. Возвращает True, если обработан."""
    barcode = card.get("barcode") or ""
    chrt_id = card.get("chrtId")

    product = None
    if chrt_id is not None:
        product = db.scalar(
            select(Product).where(
                Product.client_id == client.id, Product.wb_chrt_id == chrt_id, Product.archived_at.is_(None),
            )
        )
    if product is None and barcode:
        product = db.scalar(
            select(Product).where(
                Product.client_id == client.id, Product.barcode == barcode, Product.archived_at.is_(None),
            )
        )
    if product is None:
        if not barcode:
            return False  # нечем идентифицировать новый товар
        product = Product(client_id=client.id, barcode=barcode, name=card.get("name", ""))
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

    # Баркод карточки WB мог перевыпуститься (Этап P2-11): товар найден по
    # chrt_id/старому баркоду, но если WB теперь отдаёт другое значение и баркод
    # не правился вручную, подхватываем актуальное — иначе заказы по новому
    # баркоду навсегда получали бы problem='unknown_sku', хотя товар в каталоге
    # есть, просто под другим значением barcode. Не переносим, если баркод уже
    # занят другим живым товаром этого клиента (редкая коллизия данных WB) —
    # тогда лучше не трогать, чем упасть на INSERT/UPDATE constraint.
    if barcode and "barcode" not in manual and barcode != product.barcode:
        clash = db.scalar(
            select(Product).where(
                Product.client_id == client.id, Product.barcode == barcode,
                Product.archived_at.is_(None), Product.id != product.id,
            )
        )
        if clash is None:
            product.barcode = barcode

    if "barcode" not in manual:
        # Доп. skus того же размера (P2-11) — иначе заказ по второму баркоду
        # размера навсегда получал бы problem='unknown_sku', хотя товар в
        # каталоге есть под этим же chrtId, просто под другим значением barcode.
        product.extra_barcodes = [b for b in (card.get("extraBarcodes") or []) if b != product.barcode]

    product.wb_nm_id = card.get("nmId")
    product.wb_imt_id = card.get("imtId")
    product.wb_chrt_id = chrt_id
    product.synced_at = dt.datetime.now(dt.timezone.utc)
    return True


def sync_products_from_wb(db: Session, client: Client, wb_client: WBClient, *, full: bool = False) -> dict:
    """Инкрементальная синхронизация карточек WB одного клиента. Курсор
    {updatedAt, nmID} последнего ответа хранится в integration_state под ключом
    "wb.product_cards:<client_id>" — на каждого клиента отдельно (п.1.1).
    full=True — игнорировать сохранённый курсор (полная пересинхронизация)."""
    state_key = _wb_state_key(client.id)
    state = db.get(IntegrationState, state_key)
    cursor = None if full or not state or not (state.cursor or {}).get("updatedAt") else state.cursor

    imported = 0
    last = cursor
    try:
        while True:
            page = wb_client.get_product_cards(cursor)
            for card in page["cards"]:
                if _upsert_card(db, client, card):
                    imported += 1
            c = page.get("cursor") or {}
            last = {"updatedAt": c.get("updatedAt"), "nmID": c.get("nmID")}
            if c.get("total", 0) < _WB_PAGE_LIMIT:
                break
            cursor = last
    except AppError as exc:
        client.last_sync_error = exc.detail
        db.commit()
        raise

    if state is None:
        db.add(IntegrationState(key=state_key, cursor=last or {}))
    else:
        state.cursor = last or {}
    client.last_sync_at = dt.datetime.now(dt.timezone.utc)
    client.last_sync_error = None
    db.commit()
    return {"imported": imported, "cursor": last}


# Поля фильтров на странице «Товары» (feature.txt, п.3.1). Порядок значим — он же
# порядок выпадающих списков в интерфейсе: товар → размер → цвет → бренд.
FILTER_FIELDS = ("name", "size", "color", "brand")


def _live_only(stmt):
    """Живой товар = не архивирован сам и его клиент не в архиве (problems.txt, п.2):
    archive_client товары не трогает — иначе при восстановлении клиента нельзя было бы
    отличить товары, архивированные вручную раньше, — поэтому клиента проверяем join'ом."""
    return stmt.join(Client, Client.id == Product.client_id).where(
        Product.archived_at.is_(None), Client.archived_at.is_(None)
    )


def list_products(
    db: Session, *, search: str | None = None, include_archived: bool = False,
    client_id: int | None = None, name: str | None = None, size: str | None = None,
    color: str | None = None, brand: str | None = None,
) -> list[Product]:
    stmt = (
        select(Product).options(selectinload(Product.client))
        .where(Product.client_id.not_in(deleted_client_ids())).order_by(Product.id.desc())
    )
    if not include_archived:
        stmt = _live_only(stmt)
    if client_id is not None:
        stmt = stmt.where(Product.client_id == client_id)
    if search:
        # Поиск по названию, ШК ИЛИ артикулу — нужен ручному режиму приёмки (Этап 5,
        # п.5.2: "поиск товара клиента по названию, ШК или артикулу").
        like = f"%{search}%"
        stmt = stmt.where(
            Product.name.ilike(like) | Product.barcode.ilike(like) | Product.vendor_code.ilike(like)
        )
    # Значения фильтров приходят из выпадающих списков, а не вводятся руками,
    # поэтому сравнение точное — в отличие от search с его ilike.
    for field, value in zip(FILTER_FIELDS, (name, size, color, brand)):
        if value:
            stmt = stmt.where(getattr(Product, field) == value)
    return list(db.scalars(stmt))


def filter_options(
    db: Session, *, client_id: int | None = None, include_archived: bool = False
) -> dict[str, list[str]]:
    """Значения для выпадающих фильтров каталога.

    Списки независимы друг от друга: выбор бренда не сужает размеры (так согласовано
    с заказчиком). Область — каталог выбранного клиента. Архивные товары дают значения
    ровно тогда, когда они показаны в списке: иначе в фильтре остался бы выбор, под
    который ничего не найдётся.
    """
    options: dict[str, list[str]] = {}
    for field in FILTER_FIELDS:
        column = getattr(Product, field)
        stmt = select(column).distinct().where(
            column.is_not(None), column != "", Product.client_id.not_in(deleted_client_ids())
        )
        if not include_archived:
            stmt = _live_only(stmt)
        if client_id is not None:
            stmt = stmt.where(Product.client_id == client_id)
        options[field] = sorted(db.scalars(stmt), key=str.casefold)
    return options


def create_product(
    db: Session, *, client_id: int, barcode: str, name: str, brand: str | None = None,
    size: str | None = None, color: str | None = None, vendor_code: str | None = None,
    image_url: str | None = None, actor: str,
) -> Product:
    barcode = barcode.strip()
    if not _validate_any_barcode(barcode):
        raise AppError(
            f'Баркод «{barcode}» некорректен — ожидается GTIN (8/12/13/14 цифр) '
            f'либо внутренний формат LDX-XXXXXX.',
            status_code=400,
            reason_code="invalid_barcode",
        )
    # Уникальность — В ПРЕДЕЛАХ КЛИЕНТА (Этап 1, п.1.1): у двух клиентов может
    # быть один и тот же баркод, это не конфликт.
    clash = db.scalar(
        select(Product).where(
            Product.client_id == client_id, Product.barcode == barcode, Product.archived_at.is_(None),
        )
    )
    if clash is not None:
        raise AppError(
            f'У этого клиента уже есть товар с баркодом «{barcode}».', status_code=409, reason_code="barcode_exists",
        )

    product = Product(
        client_id=client_id, barcode=barcode, name=name, brand=brand, size=size, color=color,
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
            Product.client_id == product.client_id, Product.barcode == new_barcode,
            Product.archived_at.is_(None), Product.id != product.id,
        )
    )
    if clash is not None:
        raise AppError(
            f'Баркод «{new_barcode}» уже используется другим товаром этого клиента.',
            status_code=409,
            reason_code="barcode_exists",
        )

    old_barcode = product.barcode
    product.barcode = new_barcode
    # Допуски переносим ДОБАВЛЕНИЕМ новой записи, а не переименованием старой
    # (P3): cell_allowed_barcodes.barcode — голая строка без привязки к клиенту,
    # баркоды уникальны только В ПРЕДЕЛАХ клиента, значит тот же старый баркод
    # мог совпасть с товаром ДРУГОГО клиента. Переименование строки задним числом
    # молча меняло бы чужой допуск; добавление новой записи ничего не отбирает.
    cell_ids = set(
        db.scalars(select(CellAllowedBarcode.cell_id).where(CellAllowedBarcode.barcode == old_barcode))
    )
    for cell_id in cell_ids:
        exists = db.scalar(
            select(CellAllowedBarcode).where(
                CellAllowedBarcode.cell_id == cell_id, CellAllowedBarcode.barcode == new_barcode,
            )
        )
        if exists is None:
            db.add(CellAllowedBarcode(cell_id=cell_id, barcode=new_barcode))
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
            Product.client_id == product.client_id, Product.barcode == product.barcode,
            Product.archived_at.is_(None), Product.id != product.id,
        )
    )
    if clash is not None:
        raise AppError(
            f'Баркод «{product.barcode}» с тех пор занял другой товар этого клиента — восстановление невозможно.',
            status_code=409,
            reason_code="barcode_taken",
        )
    product.archived_at = None
    audit.record(db, entity_type="product", entity_id=product.id, action="restore", actor=actor)
    db.commit()
    db.refresh(product)
    return product
