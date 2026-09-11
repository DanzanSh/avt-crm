"""Инкрементальная синхронизация карточек WB: сохранение курсора между запусками,
пагинация по total < limit, матчинг по (client_id, wb_chrt_id) — Этап 1: товар =
размер карточки, matching по chrtId заменил старый matching по nmId (у карточки
несколько размеров = несколько chrtId)."""

from sqlalchemy import select

from fulfil.models.integration_state import IntegrationState
from fulfil.models.product import Product
from fulfil.services.products import sync_products_from_wb

_LIMIT = 100


class _PagedWBClient:
    """Отдаёт 2 страницы: первая с total == limit (есть продолжение), вторая с
    total < limit (конец). Запоминает последний пришедший курсор."""

    def __init__(self):
        self.calls: list = []
        page1 = [
            {"nmId": i, "chrtId": i, "barcode": f"200000000{i:04d}", "name": f"Товар {i}"}
            for i in range(1, 4)
        ]
        page2 = [{"nmId": 4, "chrtId": 4, "barcode": "2000000000004", "name": "Товар 4"}]
        self._pages = [
            {"cards": page1, "cursor": {"updatedAt": "2026-01-02T00:00:00Z", "nmID": 3, "total": _LIMIT}},
            {"cards": page2, "cursor": {"updatedAt": "2026-01-03T00:00:00Z", "nmID": 4, "total": 1}},
        ]

    def get_product_cards(self, cursor=None):
        self.calls.append(cursor)
        idx = 0 if cursor is None else 1
        return self._pages[min(idx, 1)]


def test_first_sync_persists_cursor(db, seller):
    wb = _PagedWBClient()
    result = sync_products_from_wb(db, seller, wb)

    assert result["imported"] == 4
    assert result["cursor"] == {"updatedAt": "2026-01-03T00:00:00Z", "nmID": 4}

    state = db.get(IntegrationState, f"wb.product_cards:{seller.id}")
    assert state is not None
    assert state.cursor == {"updatedAt": "2026-01-03T00:00:00Z", "nmID": 4}
    # первый вызов без курсора, второй — с курсором первой страницы
    assert wb.calls[0] is None
    assert wb.calls[1] == {"updatedAt": "2026-01-02T00:00:00Z", "nmID": 3}


def test_second_sync_starts_from_saved_cursor(db, seller):
    sync_products_from_wb(db, seller, _PagedWBClient())

    wb2 = _PagedWBClient()
    sync_products_from_wb(db, seller, wb2)
    assert wb2.calls[0] == {"updatedAt": "2026-01-03T00:00:00Z", "nmID": 4}


def test_full_resync_ignores_saved_cursor(db, seller):
    sync_products_from_wb(db, seller, _PagedWBClient())

    wb2 = _PagedWBClient()
    sync_products_from_wb(db, seller, wb2, full=True)
    assert wb2.calls[0] is None


def test_match_by_wb_chrt_id_when_barcode_changed(db, seller):
    wb = _PagedWBClient()
    sync_products_from_wb(db, seller, wb)
    count_before = db.scalar(select(Product.id).where(Product.wb_chrt_id == 1))
    assert count_before is not None

    class _ChangedBarcode:
        def get_product_cards(self, cursor=None):
            return {
                "cards": [{"nmId": 1, "chrtId": 1, "barcode": "9999999999999", "name": "Товар 1 new"}],
                "cursor": {"updatedAt": "x", "nmID": 1, "total": 0},
            }

    sync_products_from_wb(db, seller, _ChangedBarcode(), full=True)
    rows = db.scalars(select(Product).where(Product.wb_chrt_id == 1)).all()
    assert len(rows) == 1  # не создан дубль — матч по (client_id, wb_chrt_id), а не по баркоду
    assert rows[0].name == "Товар 1 new"  # карточка обновлена, не продублирована
