"""Приёмка по плану (Этап 5 плана №3, п.3.1 problems.txt).

Карточка приёмки заводится заранее — клиент, ожидаемая дата, комментарий — и план
заполняется до того, как кто-то берёт сканер. Раньше (Этап 0-4) приёмка была одна
"текущая открытая" на клиента и появлялась неявно первым сканом; тот путь и
глобальный get_or_create_open_receipt() убраны — теперь Receipt всегда создаётся
явно через create_receipt(), а приход товара идёт по конкретному receipt_id.

place_stock() по-прежнему делегирует запись остатка services.stock_ledger.apply_move()
(единственный путь изменения stock_by_cell) — здесь остаются только проверки,
специфичные для приёмки, плюс учёт плана.
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from fulfil.errors import AppError, CellOccupiedError, NotFoundError
from fulfil.exports.receipt_xlsx import build_template_xlsx, parse_plan_rows
from fulfil.models import audit
from fulfil.models.client import Client, deleted_client_ids
from fulfil.models.product import Product
from fulfil.models.receiving import Receipt, ReceiptLine, ReceiptPlanLine, ReceiptStatus
from fulfil.models.storage import Cell
from fulfil.models.stock import MoveReason, StockByCell
from fulfil.services.clients import get_live_client_or_404
from fulfil.services.storage import check_placement_allowed, find_free_cell_suggestions, resolve_location
from fulfil.services.stock import list_stock_by_cell
from fulfil.services.stock_ledger import apply_move

_GROUP_STATUS = {
    "expected": ReceiptStatus.DRAFT,
    "in_progress": ReceiptStatus.IN_PROGRESS,
    "done": ReceiptStatus.DONE,
}


# --- Приёмка: карточка ---


def get_receipt_or_404(db: Session, receipt_id: int) -> Receipt:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise NotFoundError(f"Приёмка #{receipt_id} не найдена.")
    return receipt


def _receipt_totals(db: Session, receipt_id: int) -> tuple[int, int, int]:
    plan_count, expected = db.execute(
        select(func.count(), func.coalesce(func.sum(ReceiptPlanLine.expected_qty), 0))
        .where(ReceiptPlanLine.receipt_id == receipt_id)
    ).one()
    accepted = db.scalar(
        select(func.coalesce(func.sum(ReceiptLine.qty), 0)).where(ReceiptLine.receipt_id == receipt_id)
    )
    return plan_count, expected, accepted


def receipt_out(db: Session, receipt: Receipt) -> dict:
    plan_count, expected, accepted = _receipt_totals(db, receipt.id)
    return {
        "id": receipt.id,
        "clientId": receipt.client_id,
        "clientName": receipt.client_name,
        "number": receipt.number,
        "status": receipt.status.value,
        "expectedDate": receipt.expected_date,
        "comment": receipt.comment,
        "createdBy": receipt.created_by,
        "createdAt": receipt.created_at,
        "startedAt": receipt.started_at,
        "finishedAt": receipt.finished_at,
        "planLinesCount": plan_count,
        "expectedTotal": expected,
        "acceptedTotal": accepted,
    }


def list_receipts(db: Session, *, client_id: int | None = None, group: str | None = None) -> list[dict]:
    """Список с колонками плана (№, клиент, дата, позиций, заявлено, принято) —
    заявлено/принято считаются двумя сгруппированными подзапросами и джойнятся к
    receipts одним SQL-запросом, а не в цикле по каждой приёмке (по образцу
    services.stock.list_stock_by_cell, Этап 2, п.2.3)."""
    plan_sub = (
        select(
            ReceiptPlanLine.receipt_id.label("receipt_id"),
            func.count().label("plan_count"),
            func.sum(ReceiptPlanLine.expected_qty).label("expected"),
        )
        .group_by(ReceiptPlanLine.receipt_id)
        .subquery()
    )
    accepted_sub = (
        select(
            ReceiptLine.receipt_id.label("receipt_id"),
            func.sum(ReceiptLine.qty).label("accepted"),
        )
        .group_by(ReceiptLine.receipt_id)
        .subquery()
    )
    stmt = (
        select(Receipt, plan_sub.c.plan_count, plan_sub.c.expected, accepted_sub.c.accepted)
        .options(selectinload(Receipt.client))
        .outerjoin(plan_sub, plan_sub.c.receipt_id == Receipt.id)
        .outerjoin(accepted_sub, accepted_sub.c.receipt_id == Receipt.id)
        # удалённый клиент скрыт отовсюду (problems.txt, п.5)
        .where(Receipt.client_id.not_in(deleted_client_ids()))
        .order_by(Receipt.id.desc())
    )
    if client_id is not None:
        stmt = stmt.where(Receipt.client_id == client_id)
    if group in _GROUP_STATUS:
        stmt = stmt.where(Receipt.status == _GROUP_STATUS[group])

    out = []
    for receipt, plan_count, expected, accepted in db.execute(stmt).all():
        out.append(
            {
                "id": receipt.id,
                "clientId": receipt.client_id,
                "clientName": receipt.client_name,
                "number": receipt.number,
                "status": receipt.status.value,
                "expectedDate": receipt.expected_date,
                "comment": receipt.comment,
                "createdBy": receipt.created_by,
                "createdAt": receipt.created_at,
                "startedAt": receipt.started_at,
                "finishedAt": receipt.finished_at,
                "planLinesCount": plan_count or 0,
                "expectedTotal": expected or 0,
                "acceptedTotal": accepted or 0,
            }
        )
    return out


def get_receipt_counters(db: Session, *, client_id: int | None = None) -> dict:
    stmt = (
        select(Receipt.status, func.count()).select_from(Receipt)
        .where(Receipt.client_id.not_in(deleted_client_ids()))
    )
    if client_id is not None:
        stmt = stmt.where(Receipt.client_id == client_id)
    counts = dict(db.execute(stmt.group_by(Receipt.status)).all())
    return {
        "expected": counts.get(ReceiptStatus.DRAFT, 0),
        "inProgress": counts.get(ReceiptStatus.IN_PROGRESS, 0),
        "done": counts.get(ReceiptStatus.DONE, 0),
    }


_CREATE_RECEIPT_RETRIES = 5


def create_receipt(
    db: Session, client: Client, *, expected_date: dt.date | None, comment: str | None, actor: str
) -> Receipt:
    """Номер — RCPT-{seq}, seq = max(id)+1. Два одновременных запроса могут прочитать
    один и тот же last_id и попытаться вставить одинаковый number — уникальный индекс
    ловит гонку через IntegrityError (P2-12): retry с пересчитанным номером вместо
    необработанного 500."""
    for _ in range(_CREATE_RECEIPT_RETRIES):
        last_id = db.scalar(select(Receipt.id).order_by(Receipt.id.desc())) or 0
        receipt = Receipt(
            client_id=client.id,
            number=f"RCPT-{last_id + 1:06d}",
            status=ReceiptStatus.DRAFT,
            expected_date=expected_date,
            comment=comment,
            created_by=actor,
        )
        db.add(receipt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        db.refresh(receipt)
        return receipt

    raise AppError(
        "Не удалось создать приёмку — слишком много одновременных попыток, повторите ещё раз.",
        status_code=409,
        reason_code="receipt_number_conflict",
    )


def update_receipt(db: Session, receipt: Receipt, *, changes: dict, actor: str) -> Receipt:
    """Частичное изменение шапки: changes — только переданные поля
    (expected_date / comment / client_id), остальные не трогаем.

    Смена клиента (problems.txt, п.3) — только в «Ожидается»: товары плана
    принадлежат старому клиенту, поэтому план очищается целиком."""
    audit_changes: dict = {}
    if changes.get("client_id") is not None and changes["client_id"] != receipt.client_id:
        _require_draft(receipt)
        new_client = get_live_client_or_404(db, changes["client_id"])
        old_name = receipt.client_name
        plan_cleared = bool(receipt.plan_lines)
        receipt.plan_lines.clear()
        receipt.client_id = new_client.id
        receipt.client = new_client
        audit_changes["client"] = [old_name, new_client.name]
        if plan_cleared:
            audit_changes["planCleared"] = True
    for field in ("expected_date", "comment"):
        if field in changes and changes[field] != getattr(receipt, field):
            old, new = getattr(receipt, field), changes[field]
            setattr(receipt, field, new)
            # changes — JSON-колонка: дату сериализуем строкой
            audit_changes[field] = [None if v is None else str(v) for v in (old, new)]
    if audit_changes:
        audit.record(
            db, entity_type="receipt", entity_id=receipt.id, action="update", actor=actor,
            changes=audit_changes,
        )
    db.commit()
    db.refresh(receipt)
    return receipt


def delete_receipt(db: Session, receipt: Receipt, actor: str) -> None:
    """Удаление ошибочно созданной приёмки (problems.txt, п.3). Можно, пока по ней
    ничего не принято: в «Ожидается» всегда, в «В работе» — без строк приёмки.
    Физическое удаление: принятого товара нет, значит нет и движений остатка,
    которые ссылались бы на приёмку; план уходит каскадом. След — в audit_log."""
    has_lines = db.scalar(select(ReceiptLine.id).where(ReceiptLine.receipt_id == receipt.id).limit(1))
    if receipt.status == ReceiptStatus.DONE or has_lines is not None:
        raise AppError(
            f"По приёмке {receipt.number} уже принят товар — удалить её нельзя.",
            status_code=409,
            reason_code="receipt_has_accepted_lines",
            what_to_do="Завершите приёмку; лишний товар спишите или переместите в «Остатках».",
        )
    audit.record(
        db, entity_type="receipt", entity_id=receipt.id, action="delete", actor=actor,
        comment=receipt.number,
    )
    db.delete(receipt)
    db.commit()


# --- План приёмки ---


def _require_draft(receipt: Receipt) -> None:
    if receipt.status != ReceiptStatus.DRAFT:
        raise AppError(
            f'Приёмка в статусе "{receipt.status.value}" — план можно менять только в "Ожидается".',
            status_code=409,
            reason_code="wrong_status",
            what_to_do='Редактирование плана доступно, пока приёмка не начата.',
        )


def _resolve_plan_products(
    db: Session, client_id: int, rows: list[tuple[str, str, int]]
) -> tuple[list[ReceiptPlanLine], list[str]]:
    """rows: [(label, barcode, qty)] — label уже готов для сообщения об ошибке
    ("строка 3"), чтобы и ручной ввод (позиция в таблице), и импорт xlsx (реальный
    номер строки в файле) указывали на то же место, что видит оператор. Возвращает
    (готовые строки плана, список ошибок). Одинаковый баркод дважды суммируется
    в одну строку плана — (receipt_id, product_id) уникален."""
    errors: list[str] = []
    qty_by_product: dict[int, int] = {}
    for label, barcode, qty in rows:
        if qty <= 0:
            errors.append(f"{label} — количество должно быть больше нуля")
            continue
        product = db.scalar(
            select(Product).where(
                Product.client_id == client_id, Product.barcode == barcode, Product.archived_at.is_(None)
            )
        )
        if product is None:
            errors.append(f'{label} — баркод «{barcode}» не найден в каталоге клиента')
            continue
        qty_by_product[product.id] = qty_by_product.get(product.id, 0) + qty
    if errors:
        return [], errors
    lines = [
        ReceiptPlanLine(product_id=product_id, expected_qty=qty)
        for product_id, qty in qty_by_product.items()
    ]
    return lines, []


def set_plan_lines(db: Session, receipt: Receipt, rows: list[tuple[str, int]]) -> Receipt:
    """rows: [(barcode, qty)] — план заменяется целиком (и для ручного ввода, и для
    импорта из xlsx используется один и тот же путь)."""
    _require_draft(receipt)
    labeled_rows = [(f"строка {i}", barcode, qty) for i, (barcode, qty) in enumerate(rows, start=1)]
    lines, errors = _resolve_plan_products(db, receipt.client_id, labeled_rows)
    if errors:
        raise AppError(
            "План не сохранён — есть ошибочные строки.",
            status_code=422,
            reason_code="invalid_plan_rows",
            what_to_do="Исправьте перечисленные строки и повторите.",
            extra={"errors": errors},
        )
    for old in list(receipt.plan_lines):
        db.delete(old)
    db.flush()
    for line in lines:
        line.receipt_id = receipt.id
        db.add(line)
    db.commit()
    db.refresh(receipt)
    return receipt


def build_template_for_client(db: Session, client: Client) -> bytes:
    products = db.scalars(
        select(Product)
        .where(Product.client_id == client.id, Product.archived_at.is_(None))
        .order_by(Product.name)
    ).all()
    rows = [
        {
            "barcode": p.barcode, "vendorCode": p.vendor_code, "name": p.name,
            "size": p.size, "color": p.color,
        }
        for p in products
    ]
    return build_template_xlsx(rows)


def import_plan_xlsx(db: Session, receipt: Receipt, file_bytes: bytes) -> dict:
    """Пропускает строки с пустым/нулевым количеством. Если хоть одна оставшаяся
    строка ошибочна (баркод не найден, количество не число) — план не трогаем,
    отдаём отчёт "строка N — причина"."""
    _require_draft(receipt)
    raw_rows = parse_plan_rows(file_bytes)
    parsed: list[tuple[str, str, int]] = []  # (label, barcode, qty)
    errors: list[str] = []
    for row in raw_rows:
        label = f'строка {row["rowNum"]}'
        qty_raw = row["qtyRaw"]
        if qty_raw is None or str(qty_raw).strip() == "":
            continue  # пустое количество — строка не заявлена, не ошибка
        try:
            qty = int(float(qty_raw))
        except (TypeError, ValueError):
            errors.append(f'{label} — количество «{qty_raw}» не число')
            continue
        if qty == 0:
            continue  # нулевое количество тоже пропускается молча
        if qty < 0:
            errors.append(f'{label} — количество не может быть отрицательным')
            continue
        if not row["barcode"]:
            errors.append(f'{label} — пустой баркод')
            continue
        parsed.append((label, row["barcode"], qty))

    if not parsed and not errors:
        raise AppError(
            "В файле нет ни одной строки с количеством.",
            status_code=422,
            reason_code="empty_plan",
        )

    # Резолюцию баркодов делаем всегда, даже если уже есть ошибки формата —
    # так оператор чинит все проблемные строки за одну итерацию, а не по одной.
    resolved_lines, resolve_errors = _resolve_plan_products(db, receipt.client_id, parsed)
    all_errors = errors + resolve_errors
    if all_errors:
        return {"ok": False, "imported": 0, "errors": all_errors}

    for old in list(receipt.plan_lines):
        db.delete(old)
    db.flush()
    for line in resolved_lines:
        line.receipt_id = receipt.id
        db.add(line)
    db.commit()
    return {"ok": True, "imported": len(resolved_lines), "errors": []}


def plan_with_suggestions(db: Session, receipt: Receipt) -> list[dict]:
    """План приёмки плюс подсказка ячейки для ручного режима: где товар уже лежит
    (list_stock_by_cell — один запрос на всех, без N+1), иначе первая свободная
    ячейка (один общий вызов find_free_cell_suggestions на все строки без своего
    места — это подсказка, не резервирование, коллизия исключена проверками
    place_stock в момент фактического проведения)."""
    stock_by_product = list_stock_by_cell(db, client_id=receipt.client_id)
    fallback = find_free_cell_suggestions(db)
    fallback_address = fallback[0]["address"] if fallback else None

    plan_lines = db.scalars(
        select(ReceiptPlanLine)
        .options(selectinload(ReceiptPlanLine.product))
        .where(ReceiptPlanLine.receipt_id == receipt.id)
        .order_by(ReceiptPlanLine.id)
    ).all()

    out = []
    for pl in plan_lines:
        existing = stock_by_product.get(pl.product_id)
        suggested_cell = existing[0]["cellAddress"] if existing else fallback_address
        out.append(
            {
                "productId": pl.product_id,
                "productName": pl.product.name,
                "barcode": pl.product.barcode,
                "size": pl.product.size,
                "color": pl.product.color,
                "expectedQty": pl.expected_qty,
                "suggestedCell": suggested_cell,
            }
        )
    return out


# --- Проведение приёмки ---


def start_receipt(db: Session, receipt: Receipt) -> Receipt:
    if receipt.status != ReceiptStatus.DRAFT:
        raise AppError(
            f'Приёмка в статусе "{receipt.status.value}" — начать можно только приёмку в "Ожидается".',
            status_code=409,
            reason_code="wrong_status",
        )
    receipt.status = ReceiptStatus.IN_PROGRESS
    receipt.started_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(receipt)
    return receipt


def place_stock(
    db: Session,
    product: Product,
    cell: Cell,
    qty: int,
    actor: str = "system",
    *,
    commit: bool = True,
    ref_type: str | None = None,
    ref_id: int | None = None,
) -> StockByCell:
    """Размещение остатка при приёмке. По умолчанию коммитит сам (публичный вызов);
    из add_receipt_line зовётся с commit=False, чтобы строка приёмки и движение
    остатка легли одной транзакцией. ref_type/ref_id пробрасываются в apply_move."""
    if qty <= 0:
        # AppError, а не ValueError (P2-10): раньше это было необработанным 500
        # вместо понятной ошибки — защита на уровне схемы (ScanPlaceRequest.qty,
        # ManualAcceptLine.qty) тоже добавлена, но вызов сервиса напрямую (не
        # только через эти две ручки) должен получать тот же внятный конверт.
        raise AppError("Количество должно быть больше нуля.", status_code=400, reason_code="invalid_qty")

    # Блокируем строку ячейки на время проверки+записи — гонка при параллельном
    # размещении в одну и ту же ячейку исключена.
    locked_cell = db.execute(
        select(Cell).where(Cell.id == cell.id).with_for_update()
    ).scalar_one()

    check_placement_allowed(db, locked_cell, product.barcode)

    other_sku_rows = db.scalars(
        select(StockByCell).where(
            StockByCell.cell_id == locked_cell.id,
            StockByCell.product_id != product.id,
            StockByCell.qty > 0,
        )
    ).all()
    if other_sku_rows:
        suggestions = find_free_cell_suggestions(db, locked_cell.zone_code)
        raise CellOccupiedError(
            f"Ячейка {locked_cell.address} занята другим товаром.", suggestions=suggestions
        )

    row = apply_move(
        db, product=product, cell=locked_cell, qty_delta=qty, reason=MoveReason.RECEIPT, actor=actor,
        ref_type=ref_type, ref_id=ref_id,
    )

    if commit:
        db.commit()
        db.refresh(row)
    return row


def add_receipt_line(
    db: Session, receipt: Receipt, product: Product, cell: Cell, qty: int, actor: str = "system",
    *, commit: bool = True,
) -> ReceiptLine:
    if receipt.status != ReceiptStatus.IN_PROGRESS:
        raise AppError(
            f'Приёмка в статусе "{receipt.status.value}" — принимать товар можно только в "Идёт приёмка".',
            status_code=409,
            reason_code="wrong_status",
        )
    if product.client_id != receipt.client_id:
        raise AppError(
            "Товар принадлежит другому клиенту — не найден в приёмке.",
            status_code=404,
            reason_code="not_found",
        )
    # Порядок важен: сперва строка приёмки (нужен line.id для ref_id движения),
    # затем размещение остатка без коммита — оба в одной транзакции. Падение между
    # шагами больше не оставляет товар на остатке без строки истории.
    line = ReceiptLine(
        receipt_id=receipt.id, product_id=product.id, cell_id=cell.id, qty=qty, actor=actor
    )
    db.add(line)
    db.flush()
    place_stock(
        db, product, cell, qty, actor=actor,
        commit=False, ref_type="receipt_line", ref_id=line.id,
    )
    if commit:
        db.commit()
        db.refresh(line)
    return line


def accept_manual(
    db: Session, receipt: Receipt, lines: list[dict], actor: str = "system",
) -> list[ReceiptLine]:
    """lines: [{productId, qty, cellCode}]. Одна транзакция на всю пачку — по образцу
    place_stock(commit=False): если строка N упала, откатывается вся пачка, а
    ошибка указывает на неё (AppError.extra["lineIndex"])."""
    if receipt.status != ReceiptStatus.IN_PROGRESS:
        raise AppError(
            f'Приёмка в статусе "{receipt.status.value}" — принимать товар можно только в "Идёт приёмка".',
            status_code=409,
            reason_code="wrong_status",
        )
    if not lines:
        raise AppError("Нет строк для проведения.", status_code=422, reason_code="empty_batch")

    created: list[ReceiptLine] = []
    for i, row in enumerate(lines):
        try:
            product = db.get(Product, row["productId"])
            if product is None or product.archived_at is not None:
                raise NotFoundError(f'Товар #{row["productId"]} не найден.')
            cell = resolve_location(db, row["cellCode"])
            line = add_receipt_line(db, receipt, product, cell, row["qty"], actor=actor, commit=False)
            created.append(line)
        except AppError as exc:
            db.rollback()
            raise AppError(
                f"Строка {i + 1}: {exc.detail}",
                status_code=exc.status_code,
                reason_code=exc.reason_code,
                what_to_do=exc.what_to_do,
                extra={**exc.extra, "lineIndex": i},
            ) from exc

    db.commit()
    for line in created:
        db.refresh(line)
    return created


def list_receipt_lines(
    db: Session, *, limit: int = 50, offset: int = 0, cell_id: int | None = None,
    client_id: int | None = None, receipt_id: int | None = None,
) -> list[dict]:
    """История приёмок, newest-first. Join по id без фильтра deleted_at/archived_at —
    строка истории должна пережить архивацию товара или удаление места.
    cell_id — фильтр по месту приёмки (карточка места). client_id — фильтр по
    клиенту. receipt_id — фильтр по конкретной приёмке (карточка приёмки)."""
    stmt = (
        select(
            ReceiptLine.id,
            Receipt.number,
            Receipt.client_id,
            Client.name.label("client_name"),
            Product.name,
            Product.barcode,
            Cell.address,
            ReceiptLine.qty,
            ReceiptLine.actor,
            ReceiptLine.created_at,
        )
        .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
        .join(Client, Client.id == Receipt.client_id)
        .join(Product, Product.id == ReceiptLine.product_id)
        .join(Cell, Cell.id == ReceiptLine.cell_id)
        .order_by(ReceiptLine.id.desc())
    )
    if cell_id is not None:
        stmt = stmt.where(ReceiptLine.cell_id == cell_id)
    if client_id is not None:
        stmt = stmt.where(Receipt.client_id == client_id)
    if receipt_id is not None:
        stmt = stmt.where(ReceiptLine.receipt_id == receipt_id)
    rows = db.execute(stmt.limit(limit).offset(offset)).all()
    return [
        {
            "id": r.id,
            "receiptNumber": r.number,
            "clientId": r.client_id,
            "clientName": r.client_name,
            "productName": r.name,
            "barcode": r.barcode,
            "cellAddress": r.address,
            "qty": r.qty,
            "actor": r.actor,
            "createdAt": r.created_at,
        }
        for r in rows
    ]


def receipt_progress(db: Session, receipt: Receipt) -> dict:
    """План против факта. Не единый запрос (план читал это как цель — на практике,
    как и с "остатками" в Этапе 2/3, "заявлено vs не заявлено" — две разные вещи,
    а не одна ветка CASE): сумма принятого по товару (одна агрегация), строки плана
    (один select), и, только если есть незаявленные товары, третий select ровно на
    них. Возвращает и заявленные, и незаявленные строки одним списком."""
    accepted_by_product = dict(
        db.execute(
            select(ReceiptLine.product_id, func.sum(ReceiptLine.qty))
            .where(ReceiptLine.receipt_id == receipt.id)
            .group_by(ReceiptLine.product_id)
        ).all()
    )

    plan_lines = db.scalars(
        select(ReceiptPlanLine)
        .options(selectinload(ReceiptPlanLine.product))
        .where(ReceiptPlanLine.receipt_id == receipt.id)
        .order_by(ReceiptPlanLine.id)
    ).all()

    lines = []
    seen_product_ids: set[int] = set()
    for pl in plan_lines:
        accepted = accepted_by_product.get(pl.product_id, 0)
        seen_product_ids.add(pl.product_id)
        lines.append(
            {
                "productId": pl.product_id,
                "productName": pl.product.name,
                "barcode": pl.product.barcode,
                "size": pl.product.size,
                "color": pl.product.color,
                "expectedQty": pl.expected_qty,
                "acceptedQty": accepted,
                "diff": accepted - pl.expected_qty,
                "planned": True,
            }
        )

    extra_ids = [pid for pid in accepted_by_product if pid not in seen_product_ids]
    if extra_ids:
        extra_products = db.scalars(select(Product).where(Product.id.in_(extra_ids))).all()
        by_id = {p.id: p for p in extra_products}
        for pid in extra_ids:
            p = by_id.get(pid)
            lines.append(
                {
                    "productId": pid,
                    "productName": p.name if p else "",
                    "barcode": p.barcode if p else "",
                    "size": p.size if p else None,
                    "color": p.color if p else None,
                    "expectedQty": 0,
                    "acceptedQty": accepted_by_product[pid],
                    "diff": accepted_by_product[pid],
                    "planned": False,
                }
            )

    totals = {
        "expectedQty": sum(l["expectedQty"] for l in lines),
        "acceptedQty": sum(l["acceptedQty"] for l in lines),
    }
    return {"lines": lines, "totals": totals}


def finish_receipt(db: Session, receipt: Receipt) -> Receipt:
    if receipt.status != ReceiptStatus.IN_PROGRESS:
        raise AppError(
            f'Приёмка в статусе "{receipt.status.value}" — завершить можно только приёмку в "Идёт приёмка".',
            status_code=409,
            reason_code="wrong_status",
        )
    receipt.status = ReceiptStatus.DONE
    receipt.finished_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(receipt)
    return receipt
