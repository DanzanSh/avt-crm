"""Разбор боевого ответа WB /content/v2/get/cards/list в WBHttpClient.

Регресс: `characteristics` приходит СПИСКОМ объектов {id,name,value}, а не словарём —
старый маппинг падал с AttributeError: 'list' object has no attribute 'get'.

Этап 1: WBHttpClient(token, client_id=...) — токен приходит от вызывающей стороны,
не из settings. get_product_cards разворачивает sizes[] каждой карточки в отдельные
записи (chrtId, techSize, skus[0] как barcode); размер без skus пропускается."""

import httpx
import pytest

from fulfil.errors import WbApiError
from fulfil.integrations.wb.http import WBHttpClient


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


_WB_PAYLOAD = {
    "cards": [
        {
            "nmID": 100500,
            "imtID": 900500,
            "vendorCode": "SKU-1",
            "title": "Куртка",
            "brand": "ACME",
            "characteristics": [
                {"id": 1, "name": "Состав", "value": ["хлопок"]},
                {"id": 2, "name": "Цвет", "value": ["чёрный", "синий"]},
            ],
            "sizes": [
                {"chrtID": 111, "techSize": "48", "wbSize": "M", "skus": ["2000000012345"]},
                {"chrtID": 112, "techSize": "50", "wbSize": "L", "skus": ["2000000012346"]},
            ],
            "photos": [{"big": "https://cdn.wb.ru/big.jpg", "square": "https://cdn.wb.ru/sq.jpg"}],
        },
        # вырожденная карточка: photos=None, sizes отсутствуют, characteristics пуст —
        # без sizes карточка не даёт ни одного товара (нечем идентифицировать размер).
        {"nmID": 100501, "title": "Носки", "characteristics": [], "photos": None},
        # размер без skus (баркод ещё не привязан в личном кабинете WB) — пропускается.
        {"nmID": 100502, "title": "Шапка", "sizes": [{"chrtID": 113, "techSize": "one size", "skus": []}]},
    ],
    "cursor": {"updatedAt": "2026-02-01T00:00:00Z", "nmID": 100501, "total": 3},
}


def test_get_product_cards_expands_sizes_into_separate_entries(monkeypatch):
    client = WBHttpClient("test-token")
    monkeypatch.setattr(client, "_request", lambda *a, **kw: _Resp(_WB_PAYLOAD))

    page = client.get_product_cards()

    # Карточка с двумя размерами -> два товара, вырожденные карточки — ноль.
    assert len(page["cards"]) == 2

    first = page["cards"][0]
    assert first["nmId"] == 100500
    assert first["chrtId"] == 111
    assert first["barcode"] == "2000000012345"
    assert first["size"] == "48"
    assert first["color"] == "чёрный,синий"
    assert first["imageUrl"] == "https://cdn.wb.ru/big.jpg"

    second = page["cards"][1]
    assert second["chrtId"] == 112
    assert second["barcode"] == "2000000012346"
    assert second["size"] == "50"

    # cursor.total — число КАРТОЧЕК у WB (3), не число получившихся товаров (2).
    assert page["cursor"] == {"updatedAt": "2026-02-01T00:00:00Z", "nmID": 100501, "total": 3}


class _FakeHttpxClient:
    """Заглушка httpx.Client: всегда отдаёт заранее заданный Response."""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kw):
        self._response.request = httpx.Request(method, url)
        return self._response


def test_request_converts_wb_4xx_to_wb_api_error(monkeypatch):
    """Регресс: 401 от WB долетал до FastAPI голым 500 со стектрейсом httpx.
    Теперь — доменный WbApiError (→ конверт 502) с кодом ответа и подсказкой."""
    resp = httpx.Response(401, text='{"errors":["invalid token"]}')
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _FakeHttpxClient(resp))
    monkeypatch.setattr(WBHttpClient, "_log", staticmethod(lambda *a, **kw: None))

    client = WBHttpClient("test-token", client_name="ООО Ромашка")
    with pytest.raises(WbApiError) as ei:
        client.get_product_cards()

    err = ei.value
    assert err.status_code == 502
    assert err.reason_code == "wb_api_error"
    assert err.extra["upstreamStatus"] == 401
    assert "401" in err.detail and "invalid token" in err.detail
    assert "Ромашка" in err.what_to_do  # подсказка называет клиента по имени


def test_request_converts_network_error_to_wb_api_error(monkeypatch):
    def _boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(WBHttpClient, "_log", staticmethod(lambda *a, **kw: None))
    monkeypatch.setattr("fulfil.integrations.wb.http.time.sleep", lambda *_: None)

    client = WBHttpClient("test-token")
    with pytest.raises(WbApiError) as ei:
        client.get_product_cards()
    assert ei.value.extra["upstreamStatus"] is None


def test_request_without_token_fails_fast(monkeypatch):
    client = WBHttpClient("")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("не должно дойти до сети"))
    with pytest.raises(WbApiError) as ei:
        client.get_product_cards()
    assert ei.value.extra["upstreamStatus"] is None
    assert "не задан ключ API" in ei.value.detail


def test_get_product_cards_sends_content_base_and_incremental_cursor(monkeypatch):
    client = WBHttpClient("test-token")
    seen: dict = {}

    def _fake_request(method, path, *, base=None, json=None, **kw):
        seen.update(method=method, path=path, base=base, json=json)
        return _Resp({"cards": [], "cursor": {"updatedAt": "x", "nmID": 1, "total": 0}})

    monkeypatch.setattr(client, "_request", _fake_request)
    client.get_product_cards({"updatedAt": "2026-01-01T00:00:00Z", "nmID": 42, "total": 100})

    assert seen["path"] == "/content/v2/get/cards/list"
    assert seen["base"] == client._content_base
    cur = seen["json"]["settings"]["cursor"]
    assert cur["updatedAt"] == "2026-01-01T00:00:00Z" and cur["nmID"] == 42
    assert seen["json"]["settings"]["sort"] == {"ascending": True}


def test_ping_checks_both_hosts(monkeypatch):
    client = WBHttpClient("test-token")
    seen_bases = []

    def _fake_request(method, path, *, base=None, **kw):
        seen_bases.append(base)
        return _Resp({})

    monkeypatch.setattr(client, "_request", _fake_request)
    result = client.ping()

    assert result["marketplace"]["ok"] is True
    assert result["content"]["ok"] is True
    assert set(seen_bases) == {client._base, client._content_base}
