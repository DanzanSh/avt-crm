import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.errors import NotFoundError
from fulfil.labels.cell_labels import render_cell_labels_pdf
from fulfil.models.storage import Cell, CellAllowedBarcode
from fulfil.schemas.storage import (
    AddRacksRequest,
    AllowedBarcodeRequest,
    BlockCellRequest,
    CellOut,
    GenerateCellsRequest,
    PrintLabelsRequest,
    RackOut,
    RackResizeRequest,
    ResolveRequest,
    ShelfResizeRequest,
    ZoneCreateRequest,
    ZoneOut,
    ZoneUpdateRequest,
)
from fulfil.services import storage as storage_service

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(get_current_user)])


def _actor(user: dict) -> str:
    return user.get("sub", "system")


@router.post("/cells/generate")
def generate_cells(body: GenerateCellsRequest, db: Session = Depends(get_db)) -> dict:
    created = storage_service.generate_cells(
        db, body.zone_code, body.racks, body.cells_per_rack,
        shelves_per_rack=body.shelves_per_rack,
    )
    return {"created": len(created)}


@router.get("/cell-map")
def cell_map(db: Session = Depends(get_db)) -> dict:
    return storage_service.get_cell_map(db)


@router.get("/cells", response_model=list[CellOut])
def list_cells(zone_code: str | None = None, db: Session = Depends(get_db)) -> list[Cell]:
    stmt = (
        select(Cell)
        .where(Cell.deleted_at.is_(None))
        .order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no)
    )
    if zone_code:
        stmt = stmt.where(Cell.zone_code == zone_code.upper())
    return list(db.scalars(stmt))


def _get_cell(db: Session, cell_id: int) -> Cell:
    cell = db.scalar(select(Cell).where(Cell.id == cell_id, Cell.deleted_at.is_(None)))
    if cell is None:
        raise NotFoundError(f"Ячейка #{cell_id} не найдена.")
    return cell


@router.get("/cells/{cell_id}")
def get_cell_detail(cell_id: int, db: Session = Depends(get_db)) -> dict:
    cell = _get_cell(db, cell_id)
    return {
        **CellOut.model_validate(cell).model_dump(by_alias=True),
        "allowedBarcodes": [a.barcode for a in cell.allowed_barcodes],
    }


@router.post("/cells/{cell_id}/block")
def block_cell(cell_id: int, body: BlockCellRequest, db: Session = Depends(get_db)) -> dict:
    cell = _get_cell(db, cell_id)
    storage_service.block_cell(db, cell, body.reason)
    return {"ok": True}


@router.post("/cells/{cell_id}/unblock")
def unblock_cell(cell_id: int, db: Session = Depends(get_db)) -> dict:
    cell = _get_cell(db, cell_id)
    storage_service.unblock_cell(db, cell)
    return {"ok": True}


@router.delete("/cells/{cell_id}")
def delete_cell(cell_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    cell = _get_cell(db, cell_id)
    storage_service.delete_cell(db, cell, actor=_actor(user))
    return {"ok": True}


@router.post("/cells/{cell_id}/allowed-barcodes")
def add_allowed_barcode(
    cell_id: int, body: AllowedBarcodeRequest, db: Session = Depends(get_db)
) -> dict:
    _get_cell(db, cell_id)
    existing = db.scalar(
        select(CellAllowedBarcode).where(
            CellAllowedBarcode.cell_id == cell_id, CellAllowedBarcode.barcode == body.barcode
        )
    )
    if existing is None:
        db.add(CellAllowedBarcode(cell_id=cell_id, barcode=body.barcode))
        db.commit()
    return {"ok": True}


@router.delete("/cells/{cell_id}/allowed-barcodes/{barcode}")
def remove_allowed_barcode(cell_id: int, barcode: str, db: Session = Depends(get_db)) -> dict:
    row = db.scalar(
        select(CellAllowedBarcode).where(
            CellAllowedBarcode.cell_id == cell_id, CellAllowedBarcode.barcode == barcode
        )
    )
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.post("/resolve", response_model=CellOut)
def resolve(body: ResolveRequest, db: Session = Depends(get_db)) -> Cell:
    return storage_service.resolve_location(db, body.code)


@router.post("/labels")
def print_labels(body: PrintLabelsRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    stmt = select(Cell).where(Cell.deleted_at.is_(None))
    if body.filter == "zone" and body.zone_code:
        stmt = stmt.where(Cell.zone_code == body.zone_code.upper())
    elif body.filter == "rack" and body.zone_code and body.rack_no:
        stmt = stmt.where(Cell.zone_code == body.zone_code.upper(), Cell.rack_no == body.rack_no)
        if body.shelf_no:
            stmt = stmt.where(Cell.shelf_no == body.shelf_no)
    stmt = stmt.order_by(Cell.zone_code, Cell.rack_no, Cell.shelf_no, Cell.cell_no)
    cells = list(db.scalars(stmt))

    pdf_bytes = render_cell_labels_pdf(cells, size=body.size)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=cell-labels.pdf"},
    )


# ---------------------------------------------------------------------------
# Зоны/стеллажи: CRUD (FEATURES-PLAN.md, этап 1)
# ---------------------------------------------------------------------------


@router.get("/zones", response_model=list[ZoneOut])
def list_zones(db: Session = Depends(get_db)) -> list:
    return storage_service.list_zones(db)


@router.post("/zones")
def create_zone(body: ZoneCreateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    """Объединённое создание: секция + (если переданы racks) её структура
    стеллаж → полка → место одним действием."""
    zone = storage_service.create_zone(db, body.code, body.name, actor=_actor(user))
    result = ZoneOut.model_validate(zone).model_dump(by_alias=True)
    if body.racks:
        created = storage_service.generate_cells(
            db, zone.code, body.racks, body.cells_per_rack or 1,
            shelves_per_rack=body.shelves_per_rack or 1,
        )
        result["created"] = len(created)
    return result


@router.patch("/zones/{zone_id}")
def update_zone(
    zone_id: int,
    body: ZoneUpdateRequest,
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    """Переименование (code/name) и/или ресайз до формы racks×cellsPerRack.
    ?dry_run=true считает, что произойдёт, ничего не меняя — обязателен перед
    применением на UI (FEATURES-PLAN.md, этап 1.5: удаление вслепую по количеству —
    самый лёгкий способ снести полсклада одним кликом)."""
    zone = storage_service.get_live_zone(db, zone_id)
    actor = _actor(user)

    resize_result = None
    if body.racks is not None and body.cells_per_rack is not None:
        resize_result = _resize_zone(
            db, zone, body.racks, body.shelves_per_rack or 1, body.cells_per_rack, actor, dry_run
        )
        if dry_run:
            return resize_result

    if body.code is not None or body.name is not None:
        storage_service.rename_zone(db, zone, body.code, body.name, actor)

    result = ZoneOut.model_validate(zone).model_dump(by_alias=True)
    if resize_result is not None:
        result["resize"] = resize_result
    return result


def _resize_zone(
    db: Session, zone, racks: int, shelves_per_rack: int, cells_per_rack: int, actor: str, dry_run: bool
) -> dict:
    """Ресайз секции до формы racks × shelves_per_rack × cells_per_rack (мест на полку).
    План — в терминах МЕСТ."""
    from fulfil.models.storage import Rack

    existing_racks = list(
        db.scalars(
            select(Rack).where(Rack.zone_id == zone.id, Rack.deleted_at.is_(None)).order_by(Rack.number)
        )
    )
    to_create_total = 0
    to_delete: list[str] = []
    blocking: list[dict] = []

    for rack in existing_racks:
        target = shelves_per_rack if rack.number <= racks else 0
        plan = storage_service.resize_rack(
            db, rack, target, actor, places_per_shelf=cells_per_rack, dry_run=True
        )
        to_create_total += plan["toCreate"]
        to_delete.extend(plan["toDelete"])
        blocking.extend(plan["blocking"])

    existing_numbers = {r.number for r in existing_racks}
    missing_racks = [n for n in range(1, racks + 1) if n not in existing_numbers]
    to_create_total += len(missing_racks) * shelves_per_rack * cells_per_rack

    if dry_run:
        return {"toCreate": to_create_total, "toDelete": to_delete, "blocking": blocking}

    if blocking:
        from fulfil.errors import CellsNotReleasableError

        raise CellsNotReleasableError(
            f"Нельзя изменить размер секции {zone.code}: {len(blocking)} мест заняты или заблокированы.",
            blocking_cells=blocking,
        )

    for rack in existing_racks:
        target = shelves_per_rack if rack.number <= racks else 0
        storage_service.resize_rack(db, rack, target, actor, places_per_shelf=cells_per_rack, dry_run=False)
        if target == 0:
            storage_service.delete_rack(db, rack, actor)

    if missing_racks:
        # добавляем недостающие стеллажи одним блоком, начиная с наименьшего номера
        # (реалистичный случай — все недостающие идут подряд, т.к. существующие уже покрыты выше)
        count = len(missing_racks)
        storage_service.add_racks_to_zone(
            db, zone, count, cells_per_rack, actor, shelves_per_rack=shelves_per_rack
        )

    return {"toCreate": to_create_total, "toDelete": to_delete, "blocking": []}


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    zone = storage_service.get_live_zone(db, zone_id)
    storage_service.delete_zone(db, zone, actor=_actor(user))
    return {"ok": True}


@router.post("/zones/{zone_id}/racks")
def add_racks(
    zone_id: int, body: AddRacksRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
) -> dict:
    zone = storage_service.get_live_zone(db, zone_id)
    created = storage_service.add_racks_to_zone(
        db, zone, body.racks, body.cells_per_rack, actor=_actor(user),
        shelves_per_rack=body.shelves_per_rack,
    )
    return {"created": len(created)}


@router.patch("/racks/{rack_id}")
def resize_rack(
    rack_id: int,
    body: RackResizeRequest,
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    rack = storage_service.get_live_rack(db, rack_id)
    return storage_service.resize_rack(
        db, rack, body.shelves_count, actor=_actor(user),
        places_per_shelf=body.places_per_shelf, dry_run=dry_run,
    )


@router.delete("/racks/{rack_id}")
def delete_rack(rack_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    rack = storage_service.get_live_rack(db, rack_id)
    storage_service.delete_rack(db, rack, actor=_actor(user))
    return {"ok": True}


@router.patch("/shelves/{shelf_id}")
def resize_shelf(
    shelf_id: int,
    body: ShelfResizeRequest,
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    shelf = storage_service.get_live_shelf(db, shelf_id)
    return storage_service.resize_shelf(db, shelf, body.places_count, actor=_actor(user), dry_run=dry_run)


@router.delete("/shelves/{shelf_id}")
def delete_shelf(shelf_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)) -> dict:
    shelf = storage_service.get_live_shelf(db, shelf_id)
    storage_service.delete_shelf(db, shelf, actor=_actor(user))
    return {"ok": True}
