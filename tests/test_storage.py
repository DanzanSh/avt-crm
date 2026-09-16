import pytest

from fulfil.errors import CellsNotReleasableError, NotFoundError, ZoneCodeNotLatinError
from fulfil.models.storage import CellStatus
from fulfil.services.receiving import place_stock
from fulfil.services.storage import (
    MAX_CELLS_PER_GENERATE,
    create_zone,
    delete_cell,
    delete_rack,
    delete_zone,
    format_address,
    format_cell_barcode,
    generate_cells,
    get_cell_map,
    list_zones,
    parse_address,
    rename_zone,
    resolve_location,
    validate_zone_code_latin,
)


def _make_product(db, seller, barcode="2000000000017", name="Майка белая"):
    from fulfil.models.product import Product

    p = Product(client_id=seller.id, barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_parse_address_roundtrip():
    zone, rack, shelf, cell = parse_address("A-1-1-10")
    assert (zone, rack, shelf, cell) == ("A", 1, 1, 10)
    assert format_address(zone, rack, shelf, cell) == "A-1-1-10"


def test_parse_address_normalizes_cyrillic_homoglyphs():
    """Оператор в русской раскладке сканирует «А-1-1-10» кириллицей — секция всё равно
    латинская 'A' (Scope IN п.1)."""
    zone, rack, shelf, cell = parse_address("А-1-1-10")
    assert (zone, rack, shelf, cell) == ("A", 1, 1, 10)


def test_parse_address_rejects_garbage():
    with pytest.raises(NotFoundError):
        parse_address("not-an-address")


def test_validate_zone_code_latin_accepts_latin():
    assert validate_zone_code_latin("a") == "A"
    assert validate_zone_code_latin("B1") == "B1"


def test_validate_zone_code_latin_rejects_cyrillic_with_suggestion():
    with pytest.raises(ZoneCodeNotLatinError) as exc_info:
        validate_zone_code_latin("А")  # кириллическая А
    assert exc_info.value.extra["suggestion"] == "A"


def test_generate_cells_creates_expected_count(db, seller):
    cells = generate_cells(db, "A", racks=2, cells_per_rack=3, shelves_per_rack=2)
    assert len(cells) == 12
    addresses = sorted(c.address for c in cells)
    assert addresses == [
        "A-1-1-1", "A-1-1-2", "A-1-1-3",
        "A-1-2-1", "A-1-2-2", "A-1-2-3",
        "A-2-1-1", "A-2-1-2", "A-2-1-3",
        "A-2-2-1", "A-2-2-2", "A-2-2-3",
    ]


def test_generate_cells_creates_shelves(db, seller):
    from fulfil.models.storage import Shelf

    generate_cells(db, "A", racks=2, cells_per_rack=3, shelves_per_rack=2)
    shelves = db.query(Shelf).filter(Shelf.deleted_at.is_(None)).all()
    assert len(shelves) == 4
    assert all(s.places_count == 3 for s in shelves)


def test_generate_cells_is_additive_not_duplicating(db, seller):
    generate_cells(db, "A", racks=1, cells_per_rack=2)
    second = generate_cells(db, "A", racks=1, cells_per_rack=3)
    # первые 2 места уже существовали — создаётся только недостающее третье
    assert len(second) == 1
    assert second[0].address == "A-1-1-3"


def test_generate_cells_enforces_limit(db, seller):
    """Превышение лимита — ошибка ввода (400), а не "не найдено" (P3)."""
    from fulfil.errors import AppError

    with pytest.raises(AppError) as exc_info:
        generate_cells(db, "A", racks=10, cells_per_rack=MAX_CELLS_PER_GENERATE)
    assert exc_info.value.status_code == 400
    assert exc_info.value.reason_code == "too_many_cells"


def test_generate_cells_rejects_cyrillic_zone(db, seller):
    with pytest.raises(ZoneCodeNotLatinError):
        generate_cells(db, "А", racks=1, cells_per_rack=1)


def test_cell_barcode_format():
    assert format_cell_barcode(123) == "CELL-000123"


def _tiny_png() -> bytes:
    """Минимальный валидный PNG для reportlab.ImageReader — рендер штрихкода при этом
    не запускается (B.3: проверяем, что кодируется адрес, а не сам PNG)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_render_cell_labels_encodes_address(db, monkeypatch):
    """Новые этикетки кодируют человекочитаемый адрес A-1-1-1, а не CELL-000001."""
    import fulfil.labels.cell_labels as cell_labels

    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    png = _tiny_png()
    captured: list[str] = []
    monkeypatch.setattr(
        cell_labels, "_barcode_png", lambda value: captured.append(value) or png
    )

    cell_labels.render_cell_labels_pdf([cell])

    assert captured == [cell.address] == ["A-1-1-1"]


def _pdf_page_count(pdf_bytes: bytes) -> int:
    """Без внешних зависимостей (pypdf и т.п. в проекте нет) считаем страницы по
    маркерам объектов reportlab: /Type /Page встречается один раз на страницу, а
    /Type /Pages (дерево страниц) — с суффиксом 's', его исключаем негативным лукахедом."""
    import re

    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf_bytes))


@pytest.mark.parametrize("size", ["58x40", "120x75"])
def test_render_cell_labels_fits_long_address(db, size):
    """0.1: длинный адрес (двухбуквенная зона, двузначные номера) не должен ронять
    рендер ни на одном из двух форматов этикетки, и PDF должен содержать по одной
    странице на каждую переданную ячейку."""
    import fulfil.labels.cell_labels as cell_labels

    cells = generate_cells(db, "B1", racks=12, cells_per_rack=10, shelves_per_rack=4)
    long_address_cells = [c for c in cells if c.address == "B1-12-4-10"]
    assert long_address_cells, "ожидали найти ячейку с адресом B1-12-4-10 среди сгенерированных"

    pdf_bytes = cell_labels.render_cell_labels_pdf(cells[:5], size=size)

    assert pdf_bytes.startswith(b"%PDF")
    assert _pdf_page_count(pdf_bytes) == 5


def test_fit_font_size_shrinks_for_long_address():
    """Автоподгонка через stringWidth: обычный короткий адрес влезает базовым
    кеглем, а длинный — уменьшенным, но не ниже минимума."""
    import fulfil.labels.cell_labels as cell_labels

    narrow_width = 60.0  # заведомо узко для 58×40 при базовом кегле 18pt
    normal = cell_labels._fit_font_size("A-1-1-1", narrow_width, cell_labels._ADDRESS_BASE_FONT)
    long_addr = cell_labels._fit_font_size("B1-12-4-10", narrow_width, cell_labels._ADDRESS_BASE_FONT)
    assert long_addr <= normal
    assert long_addr >= cell_labels._ADDRESS_MIN_FONT


def test_resolve_location_by_barcode_or_address(db, seller):
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    assert resolve_location(db, cell.barcode).id == cell.id
    assert resolve_location(db, cell.address).id == cell.id
    assert resolve_location(db, cell.address.lower()).id == cell.id  # раскладка/регистр
    assert resolve_location(db, str(cell.id)).id == cell.id


def test_resolve_location_accepts_cyrillic_homoglyph_address(db, seller):
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    assert resolve_location(db, "А-1-1-1").id == cell.id  # 'А' кириллическая


def test_resolve_location_not_found(db, seller):
    with pytest.raises(NotFoundError):
        resolve_location(db, "CELL-999999")


def test_create_zone_new_zone_is_last_by_position(db, seller):
    create_zone(db, "A", None, actor="tester")
    create_zone(db, "B", None, actor="tester")
    zones = list_zones(db)
    assert [z.code for z in zones] == ["A", "B"]


def test_create_zone_duplicate_rejected(db, seller):
    from fulfil.errors import AppError

    create_zone(db, "A", None, actor="tester")
    with pytest.raises(AppError):
        create_zone(db, "A", None, actor="tester")


def test_cell_map_shows_empty_zone(db, seller):
    """Дефект №6: карта строится от зон, не от ячеек — пустая зона видна сразу
    после создания (FEATURES-PLAN.md, этап 1)."""
    create_zone(db, "A", None, actor="tester")
    data = get_cell_map(db)
    assert data["zones"][0]["code"] == "A"
    assert data["zones"][0]["racks"] == []


def test_delete_zone_blocked_when_cell_occupied(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 5)
    zone = list_zones(db)[0]

    with pytest.raises(CellsNotReleasableError) as exc_info:
        delete_zone(db, zone, actor="tester")
    assert exc_info.value.extra["blockingCells"][0]["address"] == "A-1-1-1"


def test_delete_zone_cascades_when_empty(db, seller):
    generate_cells(db, "A", racks=1, cells_per_rack=2)
    zone = list_zones(db)[0]
    delete_zone(db, zone, actor="tester")

    assert list_zones(db) == []
    with pytest.raises(NotFoundError):
        resolve_location(db, "A-1-1-1")


def test_delete_rack_blocked_when_cell_blocked(db, seller):
    from fulfil.services.storage import block_cell

    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    block_cell(db, cell, "залив")
    rack = cell.rack

    with pytest.raises(CellsNotReleasableError):
        delete_rack(db, rack, actor="tester")


def test_delete_cell_ok_when_free(db, seller):
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    delete_cell(db, cell, actor="tester")
    with pytest.raises(NotFoundError):
        resolve_location(db, cell.address)


def test_rename_zone_rewrites_cell_addresses(db, seller):
    generate_cells(db, "A", racks=1, cells_per_rack=2)
    zone = list_zones(db)[0]
    rename_zone(db, zone, "Z", None, actor="tester")

    cell = resolve_location(db, "Z-1-1-1")
    assert cell.address == "Z-1-1-1"
    assert cell.zone_code == "Z"


def test_generate_cells_after_delete_reuses_address(db, seller):
    """Частичный уникальный индекс: после мягкого удаления адрес можно занять заново."""
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    delete_cell(db, cell, actor="tester")
    [new_cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    assert new_cell.address == "A-1-1-1"
    assert new_cell.id != cell.id


def test_get_cell_map_has_shelves_level(db, seller):
    generate_cells(db, "A", racks=1, cells_per_rack=2, shelves_per_rack=2)
    data = get_cell_map(db)
    rack = data["zones"][0]["racks"][0]
    assert len(rack["shelves"]) == 2
    shelf = rack["shelves"][0]
    assert shelf["number"] == 1
    assert len(shelf["cells"]) == 2
    assert shelf["cells"][0]["shelfNo"] == 1


def test_resize_shelf_grows_and_shrinks(db, seller):
    from fulfil.services.storage import get_live_shelf, resize_shelf
    from fulfil.models.storage import Shelf

    generate_cells(db, "A", racks=1, cells_per_rack=2)
    shelf_id = db.query(Shelf).filter(Shelf.deleted_at.is_(None)).one().id

    resize_shelf(db, get_live_shelf(db, shelf_id), 5, actor="tester")
    assert resolve_location(db, "A-1-1-5").address == "A-1-1-5"

    resize_shelf(db, get_live_shelf(db, shelf_id), 3, actor="tester")
    with pytest.raises(NotFoundError):
        resolve_location(db, "A-1-1-5")


def test_delete_shelf_blocked_when_cell_has_stock(db, seller):
    from fulfil.services.storage import delete_shelf, get_live_shelf
    from fulfil.models.storage import Shelf

    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 3)
    shelf_id = db.query(Shelf).filter(Shelf.deleted_at.is_(None)).one().id

    with pytest.raises(CellsNotReleasableError):
        delete_shelf(db, get_live_shelf(db, shelf_id), actor="tester")


# --- Допустимые баркоды места (problems.txt, п.1) ---


def test_add_allowed_barcode_rejects_when_other_product_lies_in_cell(db, seller):
    from fulfil.errors import AllowedBarcodesConflictError
    from fulfil.services.storage import add_allowed_barcode

    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product_a, cell, 5)

    with pytest.raises(AllowedBarcodesConflictError) as exc_info:
        add_allowed_barcode(db, cell, "2222222222222", actor="t")
    assert exc_info.value.extra["contents"] == [{"productName": "Товар A", "barcode": "1111111111111"}]
    assert cell.allowed_barcodes == []


def test_add_allowed_barcode_of_product_in_cell_then_other(db, seller):
    from fulfil.services.storage import add_allowed_barcode

    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product_a, cell, 5)

    add_allowed_barcode(db, cell, " 1111111111111 ", actor="t")
    add_allowed_barcode(db, cell, "2222222222222", actor="t")  # A уже в списке — ok
    add_allowed_barcode(db, cell, "2222222222222", actor="t")  # повтор — без дубля
    assert sorted(a.barcode for a in cell.allowed_barcodes) == ["1111111111111", "2222222222222"]


def test_add_allowed_barcode_unknown_barcode(db, seller):
    from fulfil.errors import AppError
    from fulfil.services.storage import add_allowed_barcode

    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    with pytest.raises(AppError) as exc_info:
        add_allowed_barcode(db, cell, "0000000000000", actor="t")
    assert exc_info.value.reason_code == "unknown_barcode"


def test_remove_allowed_barcode_of_product_in_cell(db, seller):
    from fulfil.errors import AllowedBarcodesConflictError
    from fulfil.services.storage import add_allowed_barcode, remove_allowed_barcode

    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product_a, cell, 5)
    add_allowed_barcode(db, cell, "1111111111111", actor="t")
    add_allowed_barcode(db, cell, "2222222222222", actor="t")

    with pytest.raises(AllowedBarcodesConflictError):
        remove_allowed_barcode(db, cell, "1111111111111", actor="t")

    remove_allowed_barcode(db, cell, "2222222222222", actor="t")
    remove_allowed_barcode(db, cell, "1111111111111", actor="t")  # последний — можно всегда
    assert cell.allowed_barcodes == []
    remove_allowed_barcode(db, cell, "1111111111111", actor="t")  # уже нет — без ошибки
