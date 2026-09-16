import io

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from fulfil.errors import AppError, BarcodeNotAllowedError, CellBlockedError, CellOccupiedError
from fulfil.models.product import Product
from fulfil.models.receiving import ReceiptStatus
from fulfil.models.stock import StockMove
from fulfil.models.storage import CellAllowedBarcode
from fulfil.services.receiving import (
    accept_manual,
    add_receipt_line,
    build_template_for_client,
    create_receipt,
    finish_receipt,
    import_plan_xlsx,
    list_receipt_lines,
    place_stock,
    receipt_progress,
    set_plan_lines,
    start_receipt,
)
from fulfil.services.storage import block_cell, generate_cells, get_cell_contents, get_cell_map
from conftest import make_client


def _make_product(db, seller, barcode="2000000000017", name="Майка белая", size=None) -> Product:
    p = Product(client_id=seller.id, barcode=barcode, name=name, size=size)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _started_receipt(db, seller):
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")
    return start_receipt(db, receipt)


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Баркод", "Артикул", "Наименование", "Размер", "Цвет", "Ожидается"])
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- place_stock: инварианты остатка не изменились этим этапом ---


def test_place_stock_happy_path(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    row = place_stock(db, product, cell, 100)
    assert row.qty == 100
    assert cell.status.value == "occupied"


def test_place_stock_split_across_two_cells_client_scenario(db, seller):
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=10)
    cell_a110 = next(c for c in cells if c.address == "A-1-1-10")
    cells_b = generate_cells(db, "B", racks=5, cells_per_rack=5)
    cell_b55 = next(c for c in cells_b if c.address == "B-5-1-5")

    place_stock(db, product, cell_a110, 100)
    row_a = place_stock(db, product, cell_a110, 75)
    row_b = place_stock(db, product, cell_b55, 175)

    assert row_a.qty == 175
    assert row_b.qty == 175


def test_place_stock_blocks_other_sku_in_occupied_cell(db, seller):
    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    product_b = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)

    place_stock(db, product_a, cell, 10)
    with pytest.raises(CellOccupiedError):
        place_stock(db, product_b, cell, 5)


def test_place_stock_blocks_disallowed_barcode(db, seller):
    product_a = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    product_b = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    db.add(CellAllowedBarcode(cell_id=cell.id, barcode=product_a.barcode))
    db.commit()

    with pytest.raises(BarcodeNotAllowedError):
        place_stock(db, product_b, cell, 5)

    place_stock(db, product_a, cell, 5)


def test_place_stock_blocks_when_cell_blocked(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    block_cell(db, cell, "Сломан стеллаж")

    with pytest.raises(CellBlockedError):
        place_stock(db, product, cell, 1)


# --- Карточка приёмки и план ---


def test_create_receipt_generates_sequential_number(db, seller):
    r1 = create_receipt(db, seller, expected_date=None, comment="Первая", actor="op")
    r2 = create_receipt(db, seller, expected_date=None, comment=None, actor="op")
    assert r1.status == ReceiptStatus.DRAFT
    assert r1.number != r2.number
    assert r1.comment == "Первая"


def test_set_plan_lines_replaces_and_rejects_unknown_barcode(db, seller):
    p1 = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")

    set_plan_lines(db, receipt, [(p1.barcode, 10)])
    assert len(receipt.plan_lines) == 1

    with pytest.raises(AppError) as exc:
        set_plan_lines(db, receipt, [(p1.barcode, 5), ("9999999999999", 3)])
    assert "errors" in exc.value.extra
    # план не изменился — ошибочный вызов ничего не применил
    db.refresh(receipt)
    assert len(receipt.plan_lines) == 1
    assert receipt.plan_lines[0].expected_qty == 10


def test_set_plan_lines_requires_draft(db, seller):
    p1 = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    receipt = _started_receipt(db, seller)
    with pytest.raises(AppError) as exc:
        set_plan_lines(db, receipt, [(p1.barcode, 10)])
    assert exc.value.reason_code == "wrong_status"


def test_build_template_for_client_lists_all_live_products(db, seller):
    _make_product(db, seller, barcode="1111111111111", name="Товар A", size="42")
    _make_product(db, seller, barcode="2222222222222", name="Товар B", size="44")
    other = make_client(db, "Другой клиент")
    _make_product(db, other, barcode="3333333333333", name="Чужой товар")

    xlsx_bytes = build_template_for_client(db, seller)
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    barcodes = {r[0] for r in rows}
    assert barcodes == {"1111111111111", "2222222222222"}


def test_import_plan_xlsx_unknown_barcode_returns_report_and_changes_nothing(db, seller):
    p1 = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")
    set_plan_lines(db, receipt, [(p1.barcode, 3)])  # уже есть план — импорт не должен его тронуть

    bad_bytes = _xlsx_bytes([["9999999999999", "", "Неизвестный", "", "", 5]])

    result = import_plan_xlsx(db, receipt, bad_bytes)
    assert result["ok"] is False
    assert any("9999999999999" in e for e in result["errors"])

    db.refresh(receipt)
    assert len(receipt.plan_lines) == 1
    assert receipt.plan_lines[0].product_id == p1.id


def test_import_plan_xlsx_skips_empty_and_zero_qty_rows(db, seller):
    p1 = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    p2 = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")

    file_bytes = _xlsx_bytes(
        [
            ["1111111111111", "", "Товар A", "", "", 7],
            ["2222222222222", "", "Товар B", "", "", None],  # пусто — пропускается
            ["2222222222222", "", "Товар B", "", "", 0],  # ноль — тоже пропускается
        ]
    )
    result = import_plan_xlsx(db, receipt, file_bytes)
    assert result == {"ok": True, "imported": 1, "errors": []}
    db.refresh(receipt)
    assert len(receipt.plan_lines) == 1
    assert receipt.plan_lines[0].product_id == p1.id
    assert receipt.plan_lines[0].expected_qty == 7


def test_import_plan_xlsx_requires_draft(db, seller):
    receipt = _started_receipt(db, seller)
    file_bytes = _xlsx_bytes([["1111111111111", "", "Товар A", "", "", 1]])
    with pytest.raises(AppError):
        import_plan_xlsx(db, receipt, file_bytes)


# --- Проведение приёмки ---


def test_start_receipt_transitions_and_sets_started_at(db, seller):
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")
    assert receipt.started_at is None
    receipt = start_receipt(db, receipt)
    assert receipt.status == ReceiptStatus.IN_PROGRESS
    assert receipt.started_at is not None

    with pytest.raises(AppError):
        start_receipt(db, receipt)  # уже начата


def test_add_receipt_line_requires_in_progress(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")  # ещё DRAFT

    with pytest.raises(AppError) as exc:
        add_receipt_line(db, receipt, product, cell, 5, actor="op")
    assert exc.value.reason_code == "wrong_status"


def test_add_receipt_line_rejects_other_clients_product(db, seller):
    other = make_client(db, "Другой клиент")
    product = _make_product(db, other, barcode="9999999999999", name="Чужой товар")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = _started_receipt(db, seller)

    with pytest.raises(AppError) as exc:
        add_receipt_line(db, receipt, product, cell, 5, actor="op")
    assert exc.value.status_code == 404


def test_add_receipt_line_records_actor(db, seller):
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = _started_receipt(db, seller)

    line = add_receipt_line(db, receipt, product, cell, 7, actor="operator-1")
    assert line.actor == "operator-1"


def test_accept_manual_happy_path_multiple_lines(db, seller):
    p1 = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    p2 = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    cells = generate_cells(db, "A", racks=1, cells_per_rack=2)
    receipt = _started_receipt(db, seller)

    lines = accept_manual(
        db, receipt,
        [
            {"productId": p1.id, "qty": 10, "cellCode": cells[0].address},
            {"productId": p2.id, "qty": 5, "cellCode": cells[1].address},
        ],
        actor="op",
    )
    assert len(lines) == 2
    assert {l.qty for l in lines} == {10, 5}


def test_accept_manual_rolls_back_whole_batch_when_third_line_fails(db, seller):
    """Сценарий из плана (п.5.3): третья строка бьётся о занятую другим SKU ячейку —
    откатывается вся пачка, ни одна из первых двух строк не остаётся в остатке."""
    p1 = _make_product(db, seller, barcode="1111111111111", name="Товар A")
    p2 = _make_product(db, seller, barcode="2222222222222", name="Товар B")
    p3 = _make_product(db, seller, barcode="3333333333333", name="Товар C")
    cells = generate_cells(db, "A", racks=1, cells_per_rack=3)
    receipt = _started_receipt(db, seller)

    # Ячейка cells[2] занята чужим (для пачки) товаром p3 заранее другим SKU.
    other = _make_product(db, seller, barcode="4444444444444", name="Уже лежит здесь")
    place_stock(db, other, cells[2], 1)

    with pytest.raises(AppError) as exc:
        accept_manual(
            db, receipt,
            [
                {"productId": p1.id, "qty": 10, "cellCode": cells[0].address},
                {"productId": p2.id, "qty": 5, "cellCode": cells[1].address},
                {"productId": p3.id, "qty": 3, "cellCode": cells[2].address},
            ],
            actor="op",
        )
    assert exc.value.extra.get("lineIndex") == 2

    lines_in_db = list_receipt_lines(db, receipt_id=receipt.id)
    assert lines_in_db == []  # ничего из первых двух строк не осталось


def test_receipt_progress_computes_discrepancies(db, seller):
    p1 = _make_product(db, seller, barcode="1111111111111", name="Недостача")
    p2 = _make_product(db, seller, barcode="2222222222222", name="Излишек")
    p3 = _make_product(db, seller, barcode="3333333333333", name="Незаявленный")
    cells = generate_cells(db, "A", racks=1, cells_per_rack=3)
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")
    set_plan_lines(db, receipt, [(p1.barcode, 100), (p2.barcode, 10)])
    receipt = start_receipt(db, receipt)

    add_receipt_line(db, receipt, p1, cells[0], 99, actor="op")  # заявлено 100 -> принято 99
    add_receipt_line(db, receipt, p2, cells[1], 15, actor="op")  # заявлено 10 -> принято 15
    add_receipt_line(db, receipt, p3, cells[2], 4, actor="op")  # не заявлено вовсе

    progress = receipt_progress(db, receipt)
    by_barcode = {l["barcode"]: l for l in progress["lines"]}

    assert by_barcode[p1.barcode]["diff"] == -1
    assert by_barcode[p1.barcode]["planned"] is True
    assert by_barcode[p2.barcode]["diff"] == 5
    assert by_barcode[p3.barcode]["diff"] == 4
    assert by_barcode[p3.barcode]["planned"] is False
    assert progress["totals"]["expectedQty"] == 110
    assert progress["totals"]["acceptedQty"] == 99 + 15 + 4


def test_finish_receipt_requires_in_progress(db, seller):
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="op")
    with pytest.raises(AppError):
        finish_receipt(db, receipt)

    receipt = start_receipt(db, receipt)
    receipt = finish_receipt(db, receipt)
    assert receipt.status == ReceiptStatus.DONE
    assert receipt.finished_at is not None


def test_list_receipt_lines_newest_first_with_joined_fields(db, seller):
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=2)
    receipt = _started_receipt(db, seller)
    add_receipt_line(db, receipt, product, cells[0], 3, actor="op")
    add_receipt_line(db, receipt, product, cells[1], 5, actor="op")

    rows = list_receipt_lines(db)
    assert [r["qty"] for r in rows] == [5, 3]
    assert rows[0]["productName"] == "Майка белая"
    assert rows[0]["cellAddress"] == cells[1].address
    assert rows[0]["actor"] == "op"
    assert rows[0]["receiptNumber"] == receipt.number
    assert rows[0]["clientId"] == seller.id


def test_receiving_fills_cell_contents_map_qty_and_move_ref(db, seller):
    product = _make_product(db, seller)
    cells = generate_cells(db, "A", racks=1, cells_per_rack=2)
    receipt = _started_receipt(db, seller)

    line = add_receipt_line(db, receipt, product, cells[0], 9, actor="op")

    contents = get_cell_contents(db, cells[0])
    assert len(contents) == 1
    assert contents[0]["productId"] == product.id
    assert contents[0]["productName"] == "Майка белая"
    assert contents[0]["qty"] == 9

    data = get_cell_map(db)
    map_cells = data["zones"][0]["racks"][0]["shelves"][0]["cells"]
    by_address = {c["address"]: c for c in map_cells}
    assert by_address[cells[0].address]["qty"] == 9
    assert by_address[cells[0].address]["clientId"] == seller.id
    assert by_address[cells[1].address]["qty"] == 0
    assert by_address[cells[1].address]["clientId"] is None

    move = db.scalar(select(StockMove).where(StockMove.cell_id == cells[0].id))
    assert move.ref_type == "receipt_line"
    assert move.ref_id == line.id


def test_receipt_history_visible_from_fresh_session(db, seller):
    """Эмуляция F5: история читается новым запросом, не живёт в DOM."""
    product = _make_product(db, seller)
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = _started_receipt(db, seller)
    add_receipt_line(db, receipt, product, cell, 2, actor="op")

    db.expire_all()
    rows = list_receipt_lines(db)
    assert len(rows) == 1
    assert rows[0]["qty"] == 2


# --- Удаление приёмки и смена клиента (problems.txt, п.3) ---


def test_delete_draft_receipt(db, seller):
    from fulfil.models.receiving import Receipt, ReceiptPlanLine
    from fulfil.services.receiving import delete_receipt

    _make_product(db, seller, barcode="1111111111111")
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="t")
    set_plan_lines(db, receipt, [("1111111111111", 3)])

    delete_receipt(db, receipt, actor="t")
    assert db.scalars(select(Receipt)).all() == []
    assert db.scalars(select(ReceiptPlanLine)).all() == []


def test_delete_in_progress_receipt_without_lines(db, seller):
    from fulfil.models.receiving import Receipt
    from fulfil.services.receiving import delete_receipt

    _make_product(db, seller, barcode="1111111111111")
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="t")
    set_plan_lines(db, receipt, [("1111111111111", 3)])
    start_receipt(db, receipt)

    delete_receipt(db, receipt, actor="t")
    assert db.scalars(select(Receipt)).all() == []


def test_delete_receipt_with_accepted_lines_forbidden(db, seller):
    from fulfil.services.receiving import delete_receipt

    product = _make_product(db, seller, barcode="1111111111111")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="t")
    set_plan_lines(db, receipt, [("1111111111111", 3)])
    start_receipt(db, receipt)
    accept_manual(db, receipt, [{"productId": product.id, "qty": 2, "cellCode": cell.address}], actor="t")

    with pytest.raises(AppError) as exc_info:
        delete_receipt(db, receipt, actor="t")
    assert exc_info.value.reason_code == "receipt_has_accepted_lines"


def test_change_client_in_draft_clears_plan(db, seller):
    import datetime as dt

    from fulfil.services.receiving import update_receipt

    other = make_client(db, name="Другой клиент")
    _make_product(db, seller, barcode="1111111111111")
    receipt = create_receipt(db, seller, expected_date=dt.date(2026, 9, 20), comment="x", actor="t")
    set_plan_lines(db, receipt, [("1111111111111", 3)])

    update_receipt(db, receipt, changes={"client_id": other.id}, actor="t")
    assert receipt.client_id == other.id
    assert receipt.plan_lines == []
    # частичный PATCH: непереданные поля не затёрты
    assert receipt.expected_date == dt.date(2026, 9, 20)
    assert receipt.comment == "x"


def test_change_client_not_in_draft_forbidden(db, seller):
    from fulfil.services.receiving import update_receipt

    other = make_client(db, name="Другой клиент")
    _make_product(db, seller, barcode="1111111111111")
    receipt = create_receipt(db, seller, expected_date=None, comment=None, actor="t")
    set_plan_lines(db, receipt, [("1111111111111", 3)])
    start_receipt(db, receipt)

    with pytest.raises(AppError) as exc_info:
        update_receipt(db, receipt, changes={"client_id": other.id}, actor="t")
    assert exc_info.value.reason_code == "wrong_status"


def test_update_date_only_keeps_client_and_plan(db, seller):
    import datetime as dt

    from fulfil.services.receiving import update_receipt

    _make_product(db, seller, barcode="1111111111111")
    receipt = create_receipt(db, seller, expected_date=None, comment="c", actor="t")
    set_plan_lines(db, receipt, [("1111111111111", 3)])

    update_receipt(db, receipt, changes={"expected_date": dt.date(2026, 10, 1)}, actor="t")
    assert receipt.client_id == seller.id
    assert len(receipt.plan_lines) == 1
    assert receipt.comment == "c"
