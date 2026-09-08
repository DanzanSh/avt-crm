from fulfil.schemas.common import CamelModel


class GenerateCellsRequest(CamelModel):
    zone_code: str
    racks: int
    shelves_per_rack: int = 1
    cells_per_rack: int  # мест на полку


class CellOut(CamelModel):
    id: int
    address: str
    barcode: str
    status: str
    blocked_reason: str | None = None
    zone_code: str
    rack_no: int
    shelf_no: int
    cell_no: int


class ShelfOut(CamelModel):
    id: int
    rack_id: int
    number: int
    places_count: int


class BlockCellRequest(CamelModel):
    reason: str


class AllowedBarcodeRequest(CamelModel):
    barcode: str


class ResolveRequest(CamelModel):
    code: str


class PrintLabelsRequest(CamelModel):
    filter: str = "all"  # all | zone | rack
    zone_code: str | None = None
    rack_no: int | None = None
    shelf_no: int | None = None
    size: str = "58x40"


class ZoneOut(CamelModel):
    id: int
    code: str
    name: str | None = None
    position: int


class ZoneCreateRequest(CamelModel):
    code: str
    name: str | None = None
    # Если переданы — сразу генерируем структуру секции (объединённое создание).
    racks: int | None = None
    shelves_per_rack: int | None = None
    cells_per_rack: int | None = None  # мест на полку


class ZoneUpdateRequest(CamelModel):
    code: str | None = None
    name: str | None = None
    racks: int | None = None
    shelves_per_rack: int | None = None
    cells_per_rack: int | None = None  # мест на полку


class RackOut(CamelModel):
    id: int
    zone_id: int
    number: int
    cells_count: int


class AddRacksRequest(CamelModel):
    racks: int
    shelves_per_rack: int = 1
    cells_per_rack: int


class RackResizeRequest(CamelModel):
    shelves_count: int
    places_per_shelf: int


class ShelfResizeRequest(CamelModel):
    places_count: int
