from fulfil.schemas.common import CamelModel


class GenerateCellsRequest(CamelModel):
    zone_code: str
    racks: int
    cells_per_rack: int


class CellOut(CamelModel):
    id: int
    address: str
    barcode: str
    status: str
    blocked_reason: str | None = None
    zone_code: str
    rack_no: int
    cell_no: int


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
    size: str = "58x40"


class ZoneOut(CamelModel):
    id: int
    code: str
    name: str | None = None
    position: int


class ZoneCreateRequest(CamelModel):
    code: str
    name: str | None = None


class ZoneUpdateRequest(CamelModel):
    code: str | None = None
    name: str | None = None
    racks: int | None = None
    cells_per_rack: int | None = None


class RackOut(CamelModel):
    id: int
    zone_id: int
    number: int
    cells_count: int


class AddRacksRequest(CamelModel):
    racks: int
    cells_per_rack: int


class RackResizeRequest(CamelModel):
    cells_count: int
