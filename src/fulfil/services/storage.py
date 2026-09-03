"""Адресное хранение: зоны/стеллажи/ячейки, разбор адреса, resolve_location,
CRUD с проверкой занятости (FEATURES-PLAN.md, этап 1).

Формат адреса — 'A-1-10' = зона-стеллаж-ячейка (Scope IN бизнес-плана, 3 уровня,
в отличие от 4-уровневого адреса эталона). Числовые сегменты хранятся и
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
from fulfil.models.storage import Cell, CellAllowedBarcode, CellStatus, Rack, Zone
from fulfil.models.stock import StockByCell
from fulfil.services.stock_ledger import recalc_cell_status

ADDRESS_RE = re.compile(r"^([A-Z0-9]{1,2})-(\d{1,4})-(\d{1,4})$")
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


def parse_address(address: str) -> tuple[str, int, int]:
    """Разбирает адрес, попутно нормализуя кириллические гомоглифы в зоне —
    оператор в русской раскладке всё ещё попадает в нужную латинскую зону."""
    normalized = normalize_homoglyphs(address.strip().upper())
    m = ADDRESS_RE.match(normalized)
    if not m:
        raise NotFoundError(f'Не удалось разобрать адрес «{address}». Ожидается формат "A-1-10".')
    zone_code, rack_no, cell_no = m.groups()
    return zone_code, int(rack_no), int(cell_no)


def format_address(zone_code: str, rack_no: int, cell_no: int) -> str:
    return f"{zone_code}-{rack_no}-{cell_no}"


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
                cell.address = format_address(code, cell.rack_no, cell.cell_no)
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


def generate_cells(db: Session, zone_code: str, racks: int, cells_per_rack: int) -> list[Cell]:
    total = racks * cells_per_rack
    if total > MAX_CELLS_PER_GENERATE:
        raise NotFoundError(
            f"За один вызов можно создать не больше {MAX_CELLS_PER_GENERATE} ячеек "
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

        for cell_no in range(1, cells_per_rack + 1):
            address = format_address(zone.code, rack_no, cell_no)
            existing = db.scalar(select(Cell).where(Cell.address == address, Cell.deleted_at.is_(None)))
            if existing is not None:
                continue  # генерация добавляет новые ячейки, не трогая существующие
            cell = Cell(
                rack_id=rack.id,
                zone_code=zone.code,
                rack_no=rack_no,
                cell_no=cell_no,
                address=address,
                barcode="",  # проставим после flush, когда появится id
                status=CellStatus.FREE,
            )
            db.add(cell)
            db.flush()
            cell.barcode = format_cell_barcode(cell.id)
            created.append(cell)
        rack.cells_count = db.scalar(
            select(func.count()).select_from(Cell).where(Cell.rack_id == rack.id, Cell.deleted_at.is_(None))
        )

    db.commit()
    return created


def add_racks_to_zone(db: Session, zone: Zone, racks: int, cells_per_rack: int, actor: str) -> list[Cell]:
    """Добавление N новых стеллажей к существующей зоне (POST /zones/{id}/racks)."""
    existing_max = db.scalar(
        select(func.max(Rack.number)).where(Rack.zone_id == zone.id, Rack.deleted_at.is_(None))
    ) or 0
    total = racks * cells_per_rack
    if total > MAX_CELLS_PER_GENERATE:
        raise NotFoundError(
            f"За один вызов можно создать не больше {MAX_CELLS_PER_GENERATE} ячеек "
            f"(запрошено {total})."
        )

    created: list[Cell] = []
    for offset in range(1, racks + 1):
        rack_no = existing_max + offset
        rack = Rack(zone_id=zone.id, number=rack_no, cells_count=cells_per_rack)
        db.add(rack)
        db.flush()
        for cell_no in range(1, cells_per_rack + 1):
            address = format_address(zone.code, rack_no, cell_no)
            cell = Cell(
                rack_id=rack.id, zone_code=zone.code, rack_no=rack_no, cell_no=cell_no,
                address=address, barcode="", status=CellStatus.FREE,
            )
            db.add(cell)
            db.flush()
            cell.barcode = format_cell_barcode(cell.id)
            created.append(cell)

    audit.record(
        db, entity_type="zone", entity_id=zone.id, action="update", actor=actor,
        changes={"racksAdded": {"from": existing_max, "to": existing_max + racks}},
    )
    db.commit()
    return created


def resize_rack(db: Session, rack: Rack, target_cells_count: int, actor: str, dry_run: bool = False) -> dict:
    """Изменяет число ячеек стеллажа до target_cells_count.

    Растим — добавляем cell_no от текущего максимума до target. Уменьшаем — удаляем
    ячейки с cell_no > target (после проверки assert_cells_releasable).
    dry_run=True — только считает, что произойдёт, ничего не меняя."""
    live_cells = list(
        db.scalars(
            select(Cell).where(Cell.rack_id == rack.id, Cell.deleted_at.is_(None)).order_by(Cell.cell_no)
        )
    )
    max_cell_no = max((c.cell_no for c in live_cells), default=0)
    to_create_count = max(0, target_cells_count - max_cell_no)
    to_delete_cells = [c for c in live_cells if c.cell_no > target_cells_count]

    if dry_run:
        blocking = []
        for c in to_delete_cells:
            qty = _cell_qty(db, c.id)
            if c.status == CellStatus.BLOCKED or qty > 0:
                blocking.append({"address": c.address, "status": c.status.value, "qty": qty})
        return {
            "toCreate": to_create_count,
            "toDelete": [c.address for c in to_delete_cells],
            "blocking": blocking,
        }

    if to_delete_cells:
        assert_cells_releasable(db, to_delete_cells, what="уменьшить стеллаж")
        import datetime as dt

        now = dt.datetime.now(dt.timezone.utc)
        for c in to_delete_cells:
            c.deleted_at = now

    zone_code = rack.zone.code
    for cell_no in range(max_cell_no + 1, target_cells_count + 1):
        address = format_address(zone_code, rack.number, cell_no)
        cell = Cell(
            rack_id=rack.id, zone_code=zone_code, rack_no=rack.number, cell_no=cell_no,
            address=address, barcode="", status=CellStatus.FREE,
        )
        db.add(cell)
        db.flush()
        cell.barcode = format_cell_barcode(cell.id)

    rack.cells_count = db.scalar(
        select(func.count()).select_from(Cell).where(Cell.rack_id == rack.id, Cell.deleted_at.is_(None))
    )
    audit.record(
        db, entity_type="rack", entity_id=rack.id, action="update", actor=actor,
        changes={"cellsCount": {"from": len(live_cells), "to": rack.cells_count}},
    )
    db.commit()
    db.refresh(rack)
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
    rack.deleted_at = now
    audit.record(db, entity_type="rack", entity_id=rack.id, action="delete", actor=actor)
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
    stmt = stmt.order_by(Cell.zone_code, Cell.rack_no, Cell.cell_no).limit(limit)
    return [{"address": c.address, "barcode": c.barcode} for c in db.scalars(stmt)]


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
    cells = list(
        db.scalars(
            select(Cell)
            .where(Cell.rack_id.in_([r.id for r in racks]), Cell.deleted_at.is_(None))
            .order_by(Cell.cell_no)
        )
    ) if racks else []

    cells_by_rack: dict[int, list[Cell]] = {}
    for c in cells:
        cells_by_rack.setdefault(c.rack_id, []).append(c)
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
                        "cells": [
                            {
                                "id": cell.id,
                                "address": cell.address,
                                "barcode": cell.barcode,
                                "status": cell.status.value,
                                "blockedReason": cell.blocked_reason,
                                "zoneCode": cell.zone_code,
                                "rackNo": cell.rack_no,
                                "cellNo": cell.cell_no,
                            }
                            for cell in cells_by_rack.get(rack.id, [])
                        ],
                    }
                    for rack in racks_by_zone.get(zone.id, [])
                ],
            }
            for zone in zones
        ]
    }
