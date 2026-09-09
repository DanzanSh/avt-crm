"""Адресное хранение: секции/стеллажи/полки/места, разбор адреса, resolve_location,
CRUD с проверкой занятости (FEATURES-PLAN.md, этап 1).

Формат адреса — 'A-1-1-10' = секция-стеллаж-полка-место (4 уровня). Внутренние имена
сущностей остались историческими: Zone == секция, Cell == место, zone_code == код
секции, cell_no == номер места на полке. Числовые сегменты хранятся и
сравниваются как int — см. DEV-PLAN.md про побайтовое совпадение сортировки
маршрута на фронте и бэке.

Код зоны создаётся ТОЛЬКО латиницей (Scope IN п.1): кириллическая «А» и латинская
«A» неразличимы на глаз, но это разные зоны с разными адресами. Скан и поиск при
этом кириллицу принимают — оператор в русской раскладке не должен биться в стену
(см. normalize_homoglyphs).
"""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfil.errors import (
    AppError,
    BarcodeNotAllowedError,
    CellBlockedError,
    CellsNotReleasableError,
    NotFoundError,
    ZoneCodeNotLatinError,
)
from fulfil.models import audit
from fulfil.models.product import Product
from fulfil.models.storage import Cell, CellAllowedBarcode, CellStatus, Rack, Shelf, Zone
from fulfil.models.stock import StockByCell
from fulfil.services.stock_ledger import recalc_cell_status

ADDRESS_RE = re.compile(r"^([A-Z0-9]{1,2})-(\d{1,4})-(\d{1,4})-(\d{1,4})$")
ZONE_CODE_RE = re.compile(r"^[A-Z0-9]{1,2}$")
MAX_CELLS_PER_GENERATE = 500  # согласовано с эталоном (storage.py:1099)

# Кириллические буквы, визуально/фонетически неотличимые от латинских — используются
# ТОЛЬКО чтобы (а) подсказать оператору верный латинский код при ошибке создания зоны
# и (б) распознать скан/ввод адреса, набранный в русской раскладке. Сами коды зон
# в БД — всегда латиница.
_HOMOGLYPHS = {
    "А": "A", "Б": "B", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
}
_CYRILLIC_RE = re.compile(r"[А-ЯЁ]")


def normalize_homoglyphs(s: str) -> str:
    return "".join(_HOMOGLYPHS.get(ch, ch) for ch in s)


def validate_zone_code_latin(raw: str) -> str:
    """Создание зоны — только латиница (Scope IN п.1). Кириллица отклоняется с
    подсказкой: `А` набрана кириллицей, предлагаем `A` (латинскую)."""
    code = raw.strip().upper()
    if ZONE_CODE_RE.match(code):
        return code
    if _CYRILLIC_RE.search(code):
        suggestion = normalize_homoglyphs(code)
        raise ZoneCodeNotLatinError(raw, suggestion=suggestion if ZONE_CODE_RE.match(suggestion) else None)
    raise AppError(
        f'Код зоны «{raw}» некорректен — ожидается 1-2 латинских буквы или цифры.',
        status_code=400,
        reason_code="invalid_zone_code",
    )


def parse_address(address: str) -> tuple[str, int, int, int]:
    """Разбирает адрес, попутно нормализуя кириллические гомоглифы в зоне —
    оператор в русской раскладке всё ещё попадает в нужную латинскую секцию.
    Возвращает (zone_code, rack_no, shelf_no, cell_no) — секция-стеллаж-полка-место."""
    normalized = normalize_homoglyphs(address.strip().upper())
    m = ADDRESS_RE.match(normalized)
    if not m:
        raise NotFoundError(f'Не удалось разобрать адрес «{address}». Ожидается формат "A-1-1-10".')
    zone_code, rack_no, shelf_no, cell_no = m.groups()
    return zone_code, int(rack_no), int(shelf_no), int(cell_no)


def format_address(zone_code: str, rack_no: int, shelf_no: int, cell_no: int) -> str:
    return f"{zone_code}-{rack_no}-{shelf_no}-{cell_no}"


def format_cell_barcode(cell_id: int) -> str:
    return f"CELL-{cell_id:06d}"


def _next_zone_position(db: Session) -> int:
    return (db.scalar(select(func.max(Zone.position))) or 0) + 1


def get_or_create_zone(db: Session, code: str, name: str | None = None) -> Zone:
    """Путь, используемый легаси-ручкой `/storage/cells/generate`: создаёт зону,
    если её ещё нет, с латинской валидацией и позицией "последняя"."""
    code = validate_zone_code_latin(code)
    zone = db.scalar(select(Zone).where(Zone.code == code, Zone.deleted_at.is_(None)))
    if zone is None:
        zone = Zone(code=code, name=name, position=_next_zone_position(db))
        db.add(zone)
        db.flush()
    return zone


def list_zones(db: Session) -> list[Zone]:
    stmt = select(Zone).where(Zone.deleted_at.is_(None)).order_by(Zone.position, Zone.id)
    return list(db.scalars(stmt))


def get_live_zone(db: Session, zone_id: int) -> Zone:
    zone = db.scalar(select(Zone).where(Zone.id == zone_id, Zone.deleted_at.is_(None)))
    if zone is None:
        raise NotFoundError(f"Зона #{zone_id} не найдена.")
    return zone


def get_live_rack(db: Session, rack_id: int) -> Rack:
    rack = db.scalar(select(Rack).where(Rack.id == rack_id, Rack.deleted_at.is_(None)))
    if rack is None:
        raise NotFoundError(f"Стеллаж #{rack_id} не найден.")
    return rack


def get_live_shelf(db: Session, shelf_id: int) -> Shelf:
    shelf = db.scalar(select(Shelf).where(Shelf.id == shelf_id, Shelf.deleted_at.is_(None)))
    if shelf is None:
        raise NotFoundError(f"Полка #{shelf_id} не найдена.")
    return shelf


def create_zone(db: Session, code: str, name: str | None, actor: str) -> Zone:
    """Явное создание зоны (в отличие от get_or_create_zone — падает, если зона уже есть)."""
    code = validate_zone_code_latin(code)
    existing = db.scalar(select(Zone).where(Zone.code == code, Zone.deleted_at.is_(None)))
    if existing is not None:
        raise AppError(
            f'Зона «{code}» уже существует.', status_code=409, reason_code="zone_exists",
            what_to_do="Выберите другой код либо измените существующую зону.",
        )
    zone = Zone(code=code, name=name, position=_next_zone_position(db))
    db.add(zone)
    db.flush()
    audit.record(
        db, entity_type="zone", entity_id=zone.id, action="create", actor=actor,
        changes={"code": {"from": None, "to": code}, "name": {"from": None, "to": name}},
    )
    db.commit()
    db.refresh(zone)
    return zone


def rename_zone(db: Session, zone: Zone, new_code: str | None, new_name: str | None, actor: str) -> Zone:
    """Переименование зоны — переписывает zone_code/address всех живых ячеек в одной
    транзакции. Штрихкоды ячеек не трогаем — они привязаны к id, не к адресу."""
    changes: dict = {}
    if new_code is not None:
        code = validate_zone_code_latin(new_code)
        if code != zone.code:
            clash = db.scalar(
                select(Zone).where(Zone.code == code, Zone.deleted_at.is_(None), Zone.id != zone.id)
            )
            if clash is not None:
                raise AppError(
                    f'Зона «{code}» уже существует.', status_code=409, reason_code="zone_exists",
                )
            changes["code"] = {"from": zone.code, "to": code}
            old_code = zone.code
            zone.code = code
            cells = db.scalars(
                select(Cell)
                .join(Rack, Rack.id == Cell.rack_id)
                .where(Rack.zone_id == zone.id, Cell.deleted_at.is_(None))
            ).all()
            for cell in cells:
                cell.zone_code = code
                cell.address = format_address(code, cell.rack_no, cell.shelf_no, cell.cell_no)
    if new_name is not None and new_name != zone.name:
        changes["name"] = {"from": zone.name, "to": new_name}
        zone.name = new_name

    if changes:
        audit.record(db, entity_type="zone", entity_id=zone.id, action="update", actor=actor, changes=changes)
        db.commit()
        db.refresh(zone)
    return zone


def _cell_qty(db: Session, cell_id: int) -> int:
    return db.scalar(
        select(func.coalesce(func.sum(StockByCell.qty), 0)).where(StockByCell.cell_id == cell_id)
    ) or 0


def assert_cells_releasable(db: Session, cells: list[Cell], *, what: str) -> None:
    """Ячейку можно удалить/уменьшить, если она не blocked и в ней нет остатка.
    Источник правды по остатку — SUM(stock_by_cell.qty), не cell.status: статус
    денормализован (см. FEATURES-PLAN.md, этап 1.4)."""
    blocking: list[dict] = []
    for cell in cells:
        qty = _cell_qty(db, cell.id)
        if cell.status != CellStatus.BLOCKED and qty <= 0:
            continue
        product_name = None
        if qty > 0:
            row = db.execute(
                select(Product.name)
                .join(StockByCell, StockByCell.product_id == Product.id)
                .where(StockByCell.cell_id == cell.id, StockByCell.qty > 0)
            ).first()
            product_name = row[0] if row else None
        blocking.append(
            {
                "address": cell.address,
                "status": cell.status.value,
                "qty": qty,
                "productName": product_name,
                "blockedReason": cell.blocked_reason,
            }
        )
    if blocking:
        raise CellsNotReleasableError(
            f"Нельзя {what}: {len(blocking)} ячеек заняты или заблокированы.",
            blocking_cells=blocking,
        )


def _make_cell(rack: Rack, shelf: Shelf, zone_code: str, rack_no: int, shelf_no: int, cell_no: int) -> Cell:
    return Cell(
        rack_id=rack.id,
        shelf_id=shelf.id,
        zone_code=zone_code,
        rack_no=rack_no,
        shelf_no=shelf_no,
        cell_no=cell_no,
        address=format_address(zone_code, rack_no, shelf_no, cell_no),
        barcode="",  # проставим после flush, когда появится id
        status=CellStatus.FREE,
    )


def _recount_rack(db: Session, rack: Rack) -> None:
    for shelf in db.scalars(
        select(Shelf).where(Shelf.rack_id == rack.id, Shelf.deleted_at.is_(None))
    ):
        shelf.places_count = db.scalar(
            select(func.count()).select_from(Cell).where(
                Cell.shelf_id == shelf.id, Cell.deleted_at.is_(None)
            )
        )
    rack.cells_count = db.scalar(
        select(func.count()).select_from(Cell).where(Cell.rack_id == rack.id, Cell.deleted_at.is_(None))
    )


def generate_cells(
    db: Session, zone_code: str, racks: int, cells_per_rack: int, *, shelves_per_rack: int = 1
) -> list[Cell]:
    """Создаёт структуру секции: стеллаж → полка → место. `cells_per_rack` —
    исторически имя параметра, семантически «мест на полку» (Секция/Полка/Место —
    в UI). Аддитивно: существующие места по совпадающему адресу не трогаем."""
    total = racks * shelves_per_rack * cells_per_rack
    if total > MAX_CELLS_PER_GENERATE:
        raise NotFoundError(
            f"За один вызов можно создать не больше {MAX_CELLS_PER_GENERATE} мест "
            f"(запрошено {total})."
        )

    zone = get_or_create_zone(db, zone_code)
    created: list[Cell] = []
    for rack_no in range(1, racks + 1):
        rack = db.scalar(
            select(Rack).where(Rack.zone_id == zone.id, Rack.number == rack_no, Rack.deleted_at.is_(None))
        )
        if rack is None:
            rack = Rack(zone_id=zone.id, number=rack_no, cells_count=0)
            db.add(rack)
            db.flush()

        for shelf_no in range(1, shelves_per_rack + 1):
            shelf = db.scalar(
                select(Shelf).where(
                    Shelf.rack_id == rack.id, Shelf.number == shelf_no, Shelf.deleted_at.is_(None)
                )
            )
            if shelf is None:
                shelf = Shelf(rack_id=rack.id, number=shelf_no, places_count=0)
                db.add(shelf)
                db.flush()

            for cell_no in range(1, cells_per_rack + 1):
                address = format_address(zone.code, rack_no, shelf_no, cell_no)
                existing = db.scalar(
                    select(Cell).where(Cell.address == address, Cell.deleted_at.is_(None))
                )
                if existing is not None:
                    continue  # генерация добавляет новые места, не трогая существующие
                cell = _make_cell(rack, shelf, zone.code, rack_no, shelf_no, cell_no)
                db.add(cell)
                db.flush()
                cell.barcode = format_cell_barcode(cell.id)
                created.append(cell)

        _recount_rack(db, rack)

    db.commit()
    return created


def add_racks_to_zone(
    db: Session, zone: Zone, racks: int, cells_per_rack: int, actor: str, *, shelves_per_rack: int = 1
) -> list[Cell]:
    """Добавление N новых стеллажей к существующей секции (POST /zones/{id}/racks)."""
    existing_max = db.scalar(
        select(func.max(Rack.number)).where(Rack.zone_id == zone.id, Rack.deleted_at.is_(None))
    ) or 0
    total = racks * shelves_per_rack * cells_per_rack
    if total > MAX_CELLS_PER_GENERATE:
        raise NotFoundError(
            f"За один вызов можно создать не больше {MAX_CELLS_PER_GENERATE} мест "
            f"(запрошено {total})."
        )

    created: list[Cell] = []
    for offset in range(1, racks + 1):
        rack_no = existing_max + offset
        rack = Rack(zone_id=zone.id, number=rack_no, cells_count=0)
        db.add(rack)
        db.flush()
        for shelf_no in range(1, shelves_per_rack + 1):
            shelf = Shelf(rack_id=rack.id, number=shelf_no, places_count=0)
            db.add(shelf)
            db.flush()
            for cell_no in range(1, cells_per_rack + 1):
                cell = _make_cell(rack, shelf, zone.code, rack_no, shelf_no, cell_no)
                db.add(cell)
                db.flush()
                cell.barcode = format_cell_barcode(cell.id)
                created.append(cell)
        _recount_rack(db, rack)

    audit.record(
        db, entity_type="zone", entity_id=zone.id, action="update", actor=actor,
        changes={"racksAdded": {"from": existing_max, "to": existing_max + racks}},
    )
    db.commit()
    return created


def _blocking_summary(db: Session, cells: list[Cell]) -> list[dict]:
    blocking = []
    for c in cells:
        qty = _cell_qty(db, c.id)
        if c.status == CellStatus.BLOCKED or qty > 0:
            blocking.append({"address": c.address, "status": c.status.value, "qty": qty})
    return blocking


def resize_rack(
    db: Session, rack: Rack, target_shelves_count: int, actor: str, *,
    places_per_shelf: int, dry_run: bool = False,
) -> dict:
    """Изменяет число ПОЛОК стеллажа до target_shelves_count.

    Растим — добавляем полки от текущего максимума до target, по places_per_shelf
    мест на каждой. Уменьшаем — удаляем полки с number > target вместе с их
    местами (после assert_cells_releasable по всем этим местам).
    dry_run=True — только считает, что произойдёт, ничего не меняя. План — в
    терминах МЕСТ (toCreate/toDelete)."""
    live_shelves = list(
        db.scalars(
            select(Shelf).where(Shelf.rack_id == rack.id, Shelf.deleted_at.is_(None)).order_by(Shelf.number)
        )
    )
    max_shelf_no = max((s.number for s in live_shelves), default=0)
    shelves_to_delete = [s for s in live_shelves if s.number > target_shelves_count]
    shelf_ids_to_delete = [s.id for s in shelves_to_delete]
    cells_to_delete = list(
        db.scalars(select(Cell).where(Cell.shelf_id.in_(shelf_ids_to_delete), Cell.deleted_at.is_(None)))
    ) if shelf_ids_to_delete else []
    to_create_count = max(0, target_shelves_count - max_shelf_no) * places_per_shelf

    if dry_run:
        return {
            "toCreate": to_create_count,
            "toDelete": [c.address for c in cells_to_delete],
            "blocking": _blocking_summary(db, cells_to_delete),
        }

    prev_cells_count = rack.cells_count
    if cells_to_delete:
        assert_cells_releasable(db, cells_to_delete, what="уменьшить стеллаж")
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    for c in cells_to_delete:
        c.deleted_at = now
    for s in shelves_to_delete:
        s.deleted_at = now

    zone_code = rack.zone.code
    for shelf_no in range(max_shelf_no + 1, target_shelves_count + 1):
        shelf = Shelf(rack_id=rack.id, number=shelf_no, places_count=0)
        db.add(shelf)
        db.flush()
        for cell_no in range(1, places_per_shelf + 1):
            cell = _make_cell(rack, shelf, zone_code, rack.number, shelf_no, cell_no)
            db.add(cell)
            db.flush()
            cell.barcode = format_cell_barcode(cell.id)

    _recount_rack(db, rack)
    audit.record(
        db, entity_type="rack", entity_id=rack.id, action="update", actor=actor,
        changes={"cellsCount": {"from": prev_cells_count, "to": rack.cells_count}},
    )
    db.commit()
    db.refresh(rack)
    return {"toCreate": to_create_count, "toDelete": [c.address for c in cells_to_delete], "blocking": []}


def resize_shelf(db: Session, shelf: Shelf, target_places_count: int, actor: str, dry_run: bool = False) -> dict:
    """Изменяет число МЕСТ на одной полке до target_places_count.

    Растим — добавляем cell_no от текущего максимума до target. Уменьшаем — удаляем
    места с cell_no > target (после assert_cells_releasable)."""
    live_cells = list(
        db.scalars(
            select(Cell).where(Cell.shelf_id == shelf.id, Cell.deleted_at.is_(None)).order_by(Cell.cell_no)
        )
    )
    max_cell_no = max((c.cell_no for c in live_cells), default=0)
    to_create_count = max(0, target_places_count - max_cell_no)
    to_delete_cells = [c for c in live_cells if c.cell_no > target_places_count]

    if dry_run:
        return {
            "toCreate": to_create_count,
            "toDelete": [c.address for c in to_delete_cells],
            "blocking": _blocking_summary(db, to_delete_cells),
        }

    if to_delete_cells:
        assert_cells_releasable(db, to_delete_cells, what="уменьшить полку")
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    for c in to_delete_cells:
        c.deleted_at = now

    rack = shelf.rack
    zone_code = rack.zone.code
    for cell_no in range(max_cell_no + 1, target_places_count + 1):
        cell = _make_cell(rack, shelf, zone_code, rack.number, shelf.number, cell_no)
        db.add(cell)
        db.flush()
        cell.barcode = format_cell_barcode(cell.id)

    _recount_rack(db, rack)
    audit.record(
        db, entity_type="shelf", entity_id=shelf.id, action="update", actor=actor,
        changes={"placesCount": {"from": len(live_cells), "to": shelf.places_count}},
    )
    db.commit()
    db.refresh(shelf)
    return {"toCreate": to_create_count, "toDelete": [c.address for c in to_delete_cells], "blocking": []}


def delete_rack(db: Session, rack: Rack, actor: str) -> None:
    live_cells = list(
        db.scalars(select(Cell).where(Cell.rack_id == rack.id, Cell.deleted_at.is_(None)))
    )
    assert_cells_releasable(db, live_cells, what="удалить стеллаж")
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    for cell in live_cells:
        cell.deleted_at = now
    for shelf in db.scalars(select(Shelf).where(Shelf.rack_id == rack.id, Shelf.deleted_at.is_(None))):
        shelf.deleted_at = now
    rack.deleted_at = now
    audit.record(db, entity_type="rack", entity_id=rack.id, action="delete", actor=actor)
    db.commit()


def delete_shelf(db: Session, shelf: Shelf, actor: str) -> None:
    live_cells = list(
        db.scalars(select(Cell).where(Cell.shelf_id == shelf.id, Cell.deleted_at.is_(None)))
    )
    assert_cells_releasable(db, live_cells, what="удалить полку")
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    for cell in live_cells:
        cell.deleted_at = now
    shelf.deleted_at = now
    _recount_rack(db, shelf.rack)
    audit.record(db, entity_type="shelf", entity_id=shelf.id, action="delete", actor=actor)
    db.commit()


def delete_zone(db: Session, zone: Zone, actor: str) -> None:
    live_cells = list(
        db.scalars(
            select(Cell)
            .join(Rack, Rack.id == Cell.rack_id)
            .where(Rack.zone_id == zone.id, Cell.deleted_at.is_(None))
        )
    )
    assert_cells_releasable(db, live_cells, what=f"удалить зону {zone.code}")
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    for cell in live_cells:
        cell.deleted_at = now
    racks = db.scalars(select(Rack).where(Rack.zone_id == zone.id, Rack.deleted_at.is_(None))).all()
    rack_ids = [r.id for r in racks]
    if rack_ids:
        for shelf in db.scalars(
            select(Shelf).where(Shelf.rack_id.in_(rack_ids), Shelf.deleted_at.is_(None))
        ):
            shelf.deleted_at = now
    for rack in racks:
        rack.deleted_at = now
    zone.deleted_at = now
    audit.record(db, entity_type="zone", entity_id=zone.id, action="delete", actor=actor)
    db.commit()


def delete_cell(db: Session, cell: Cell, actor: str) -> None:
    assert_cells_releasable(db, [cell], what="удалить ячейку")
    import datetime as dt

    cell.deleted_at = dt.datetime.now(dt.timezone.utc)
    audit.record(db, entity_type="cell", entity_id=cell.id, action="delete", actor=actor)
    db.commit()


def resolve_location(db: Session, raw: str) -> Cell:
    """Принимает id / 'CELL-000123' / адрес 'A-1-10' — сам определяет, что перед ним.
    Один парсер на весь проект: клиент шлёт отсканированную строку как есть,
    не классифицируя её (см. DEV-PLAN.md, приём mobile-putaway.html). Адрес,
    введённый кириллицей в русской раскладке, распознаётся через normalize_homoglyphs."""
    raw = raw.strip()
    if raw.isdigit():
        cell = db.scalar(select(Cell).where(Cell.id == int(raw), Cell.deleted_at.is_(None)))
        if cell:
            return cell

    upper = raw.upper()
    cell = db.scalar(select(Cell).where(Cell.barcode == upper, Cell.deleted_at.is_(None)))
    if cell:
        return cell

    normalized = normalize_homoglyphs(upper)
    cell = db.scalar(select(Cell).where(Cell.address == normalized, Cell.deleted_at.is_(None)))
    if cell:
        return cell

    raise NotFoundError(f'Ячейка «{raw}» не найдена.')


def check_placement_allowed(db: Session, cell: Cell, product_barcode: str) -> None:
    """Три проверки приёмки (Scope IN п.4): допуск баркода, занятость другим SKU, блокировка."""
    if cell.status == CellStatus.BLOCKED:
        raise CellBlockedError(
            f"Ячейка {cell.address} заблокирована.", reason=cell.blocked_reason
        )

    allowed = [a.barcode for a in cell.allowed_barcodes]
    if allowed and product_barcode not in allowed:
        raise BarcodeNotAllowedError(
            f"В ячейку {cell.address} нельзя класть этот товар — баркод не входит "
            f"в список разрешённых для неё.",
            allowed=allowed,
        )
    # занятость другим SKU проверяется в services.receiving.place_stock / services.stock.move_stock,
    # где виден фактический остаток по ячейке


def block_cell(db: Session, cell: Cell, reason: str) -> None:
    cell.status = CellStatus.BLOCKED
    cell.blocked_reason = reason
    db.commit()


def unblock_cell(db: Session, cell: Cell) -> None:
    """Снимает блокировку. Статус после этого — производная от фактического остатка
    (occupied/free), а не безусловный free (FEATURES-PLAN.md, дефект №2: непустая
    ячейка, разблокированная вслепую, лезла в список свободных)."""
    cell.status = CellStatus.FREE  # временно, recalc_cell_status поправит на occupied при остатке
    cell.blocked_reason = None
    recalc_cell_status(db, cell)
    db.commit()


def find_free_cell_suggestions(db: Session, zone_code: str | None = None, limit: int = 3) -> list[dict]:
    stmt = select(Cell).where(Cell.status == CellStatus.FREE, Cell.deleted_at.is_(None))
    if zone_code:
        stmt = stmt.where(Cell.zone_code == zone_code.upper())
    stmt = stmt.order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no).limit(limit)
    return [{"address": c.address, "barcode": c.barcode} for c in db.scalars(stmt)]


def get_cell_contents(db: Session, cell: Cell) -> list[dict]:
    """Содержимое места: товары с положительным остатком в этой ячейке (C.1).
    Join stock_by_cell → products по cell_id, только qty > 0, сортировка по названию."""
    rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.barcode,
            Product.image_url,
            StockByCell.qty,
        )
        .join(StockByCell, StockByCell.product_id == Product.id)
        .where(StockByCell.cell_id == cell.id, StockByCell.qty > 0)
        .order_by(Product.name)
    ).all()
    return [
        {
            "productId": r.id,
            "productName": r.name,
            "barcode": r.barcode,
            "imageUrl": r.image_url,
            "qty": r.qty,
        }
        for r in rows
    ]


def get_cell_map(db: Session) -> dict:
    """Строится ОТ зон (не от ячеек) — пустая зона видна сразу после создания
    (FEATURES-PLAN.md, дефект №6). Порядок зон — position, id: новая всегда последняя."""
    zones = list_zones(db)
    racks = list(
        db.scalars(
            select(Rack)
            .where(Rack.zone_id.in_([z.id for z in zones]), Rack.deleted_at.is_(None))
            .order_by(Rack.number)
        )
    ) if zones else []
    shelves = list(
        db.scalars(
            select(Shelf)
            .where(Shelf.rack_id.in_([r.id for r in racks]), Shelf.deleted_at.is_(None))
            .order_by(Shelf.number)
        )
    ) if racks else []
    cells = list(
        db.scalars(
            select(Cell)
            .where(Cell.shelf_id.in_([s.id for s in shelves]), Cell.deleted_at.is_(None))
            .order_by(Cell.cell_no)
        )
    ) if shelves else []

    # Остаток по всем местам карты — ОДНИМ агрегирующим запросом (C.1), без N+1:
    # карта тянет все места сразу. Места без остатка → qty: 0.
    cell_ids = [c.id for c in cells]
    qty_by_cell: dict[int, int] = {}
    if cell_ids:
        qty_by_cell = {
            cid: int(total or 0)
            for cid, total in db.execute(
                select(StockByCell.cell_id, func.sum(StockByCell.qty))
                .where(StockByCell.cell_id.in_(cell_ids))
                .group_by(StockByCell.cell_id)
            ).all()
        }

    cells_by_shelf: dict[int, list[Cell]] = {}
    for c in cells:
        cells_by_shelf.setdefault(c.shelf_id, []).append(c)
    shelves_by_rack: dict[int, list[Shelf]] = {}
    for s in shelves:
        shelves_by_rack.setdefault(s.rack_id, []).append(s)
    racks_by_zone: dict[int, list[Rack]] = {}
    for r in racks:
        racks_by_zone.setdefault(r.zone_id, []).append(r)

    return {
        "zones": [
            {
                "id": zone.id,
                "code": zone.code,
                "name": zone.name,
                "racks": [
                    {
                        "id": rack.id,
                        "number": rack.number,
                        "shelves": [
                            {
                                "id": shelf.id,
                                "number": shelf.number,
                                "placesCount": shelf.places_count,
                                "cells": [
                                    {
                                        "id": cell.id,
                                        "address": cell.address,
                                        "barcode": cell.barcode,
                                        "qty": qty_by_cell.get(cell.id, 0),
                                        "status": cell.status.value,
                                        "blockedReason": cell.blocked_reason,
                                        "zoneCode": cell.zone_code,
                                        "rackNo": cell.rack_no,
                                        "shelfNo": cell.shelf_no,
                                        "cellNo": cell.cell_no,
                                    }
                                    for cell in cells_by_shelf.get(shelf.id, [])
                                ],
                            }
                            for shelf in shelves_by_rack.get(rack.id, [])
                        ],
                    }
                    for rack in racks_by_zone.get(zone.id, [])
                ],
            }
            for zone in zones
        ]
    }
