import pytest

from fulfil.errors import AmbiguousCodeError, NotFoundError
from fulfil.models.product import Product
from fulfil.services.scan import fix_ru_layout, resolve_scan
from fulfil.services.storage import generate_cells


def _make_product(db, barcode="2000000000017", name="Майка белая") -> Product:
    p = Product(barcode=barcode, name=name)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_fix_ru_layout_matches_scan_layout_js_table():
    # 'йцукен' в русской раскладке физически напечатано теми же клавишами, что 'qwerty'
    assert fix_ru_layout("йцукен") == "qwerty"
    assert fix_ru_layout("qwe") == "qwe"  # латиница не трогается


def test_resolve_scan_finds_product_by_exact_barcode(db):
    product = _make_product(db)
    kind, entity = resolve_scan(db, product.barcode)
    assert kind == "product"
    assert entity.id == product.id


def test_resolve_scan_finds_cell_by_address(db):
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    kind, entity = resolve_scan(db, cell.address)
    assert kind == "cell"
    assert entity.id == cell.id


def test_resolve_scan_finds_cell_by_barcode(db):
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    kind, entity = resolve_scan(db, cell.barcode)
    assert kind == "cell"
    assert entity.id == cell.id


def test_resolve_scan_not_found(db):
    with pytest.raises(NotFoundError):
        resolve_scan(db, "totally-unknown-code")


def test_resolve_scan_ambiguous_when_raw_and_fixed_both_exist_as_different_entities(db):
    """Guard неоднозначности (DEV-PLAN.md, placement-items.html): если сырой код и код
    после исправления раскладки существуют и указывают на разные сущности — отказ,
    а не угадывание (FEATURES-PLAN.md, дефект №4)."""
    raw = "агп"  # RU_TO_EN: а->f, г->u, п->g
    fixed = fix_ru_layout(raw)
    assert fixed == "fug"

    product_raw = _make_product(db, barcode=raw, name="Товар как есть")
    product_fixed = _make_product(db, barcode=fixed, name="Товар после раскладки")
    assert product_raw.id != product_fixed.id

    with pytest.raises(AmbiguousCodeError):
        resolve_scan(db, raw)


def test_resolve_scan_not_ambiguous_when_only_one_candidate_exists(db):
    raw = "агп"
    product = _make_product(db, barcode=raw, name="Товар")
    kind, entity = resolve_scan(db, raw)
    assert kind == "product"
    assert entity.id == product.id
