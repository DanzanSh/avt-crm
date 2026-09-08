"""Боевой клиент Wildberries Seller API.

ВНИМАНИЕ: точные пути и формы ответов ниже — best-effort по публичной документации
WB Seller API на момент написания. Часть контрактов (особенно передача кодов
маркировки при подтверждении сборки и выдача QR коробов/поставки) — риск №1
бизнес-плана; они ДОЛЖНЫ быть проверены и, если нужно, поправлены в День 1
против реального токена и зафиксированы в docs/wb-api-contract.md.
Токен на момент написания этого клиента недоступен — код не был обкатан вживую.
"""

import time

import httpx

from fulfil.config import get_settings
from fulfil.db import SessionLocal
from fulfil.integrations.wb.base import WbCardsPage, WbCursor, WbOrder, WbSticker
from fulfil.models.wb_log import WbApiLog

_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 0.5


def _card_color(card: dict) -> str:
    """WB /content/v2/get/cards/list отдаёт `characteristics` списком объектов
    `{id, name, value}` (value — список или скаляр). Раньше код обращался к нему
    как к словарю `{"Цвет": [...]}` и падал с AttributeError на боевом ответе."""
    chars = card.get("characteristics")
    values: list[str] = []
    if isinstance(chars, list):
        for ch in chars:
            if isinstance(ch, dict) and ch.get("name") == "Цвет":
                v = ch.get("value")
                if isinstance(v, list):
                    values.extend(str(x) for x in v if x not in (None, ""))
                elif v not in (None, ""):
                    values.append(str(v))
    elif isinstance(chars, dict):  # на случай иной/старой формы ответа
        v = chars.get("Цвет") or []
        values.extend(str(x) for x in v) if isinstance(v, list) else values.append(str(v))
    return ",".join(values)


def _first_dict(seq) -> dict:
    """Первый элемент списка, если это словарь; иначе пустой словарь.
    Защищает от `sizes`/`photos` == None или элементов неожиданного типа."""
    if isinstance(seq, list) and seq and isinstance(seq[0], dict):
        return seq[0]
    return {}


def _first_sku(card: dict) -> str:
    skus = _first_dict(card.get("sizes")).get("skus")
    return skus[0] if isinstance(skus, list) and skus else ""


def _first_size(card: dict) -> str:
    return _first_dict(card.get("sizes")).get("techSize", "") or ""


def _first_photo(card: dict) -> str:
    return _first_dict(card.get("photos")).get("big", "") or ""


class WBHttpClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base = settings.wb_api_base.rstrip("/")
        self._content_base = settings.wb_content_api_base.rstrip("/")
        self._token = settings.wb_api_token
        self._warehouse_id = settings.wb_warehouse_id

    def _headers(self) -> dict:
        return {"Authorization": self._token, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, base: str | None = None, **kwargs) -> httpx.Response:
        url = f"{(base or self._base)}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            started = time.monotonic()
            status_code: int | None = None
            error: str | None = None
            try:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.request(method, url, headers=self._headers(), **kwargs)
                status_code = resp.status_code
                if resp.status_code == 429 and attempt < _MAX_RETRIES - 1:
                    time.sleep(_BACKOFF_BASE_SEC * (2**attempt))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                error = str(exc)
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_BACKOFF_BASE_SEC * (2**attempt))
                    continue
            finally:
                duration_ms = int((time.monotonic() - started) * 1000)
                self._log(method, path, status_code, duration_ms, error)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _log(method: str, path: str, status: int | None, duration_ms: int, error: str | None) -> None:
        db = SessionLocal()
        try:
            db.add(
                WbApiLog(
                    method=method,
                    path=path,
                    status=status,
                    duration_ms=duration_ms,
                    error=error,
                )
            )
            db.commit()
        finally:
            db.close()

    # --- Каталог ---
    def get_product_cards(self, cursor: WbCursor | None = None) -> WbCardsPage:
        cur: dict = {"limit": 100}
        if cursor:
            cur["updatedAt"] = cursor["updatedAt"]
            cur["nmID"] = cursor["nmID"]
        body = {
            "settings": {
                "sort": {"ascending": True},
                "filter": {"withPhoto": -1},
                "cursor": cur,
            }
        }
        resp = self._request(
            "POST", "/content/v2/get/cards/list", base=self._content_base, json=body
        )
        data = resp.json()
        cards = [
            {
                "nmId": c.get("nmID"),
                "imtId": c.get("imtID"),
                "vendorCode": c.get("vendorCode", ""),
                "barcode": _first_sku(c),
                "name": c.get("title", ""),
                "brand": c.get("brand", ""),
                "size": _first_size(c),
                "color": _card_color(c),
                "imageUrl": _first_photo(c),
            }
            for c in data.get("cards", [])
        ]
        c = data.get("cursor") or {}
        next_cursor: WbCursor = {
            "updatedAt": c.get("updatedAt"),
            "nmID": c.get("nmID"),
            "total": c.get("total", 0),
        }
        return {"cards": cards, "cursor": next_cursor}

    # --- Остатки FBS ---
    def update_fbs_stock(self, warehouse_id: str, barcode: str, qty: int) -> dict:
        body = {"stocks": [{"sku": barcode, "amount": qty}]}
        resp = self._request("PUT", f"/api/v3/stocks/{warehouse_id}", json=body)
        return {"ok": resp.status_code < 300}

    # --- Заказы ---
    def get_new_orders(self) -> list[WbOrder]:
        resp = self._request("GET", "/api/v3/orders/new")
        data = resp.json()
        return [
            {
                "orderId": str(o.get("id")),
                "supplyId": o.get("supplyId"),
                "createdAt": o.get("createdAt", ""),
                "deadlineAt": None,
                "items": [{"barcode": o.get("skus", [""])[0], "qty": 1}],
            }
            for o in data.get("orders", [])
        ]

    def get_order_sticker(self, order_id: str) -> WbSticker:
        resp = self._request(
            "POST", "/api/v3/orders/stickers", json={"orders": [int(order_id)]}, params={"type": "png"}
        )
        data = resp.json()
        stickers = data.get("stickers", [])
        first = stickers[0] if stickers else {"file": ""}
        return {"type": "png", "data": first.get("file", "")}

    def send_marking_codes(self, order_id: str, codes: list[str]) -> dict:
        resp = self._request(
            "POST", f"/api/v3/orders/{order_id}/meta/sgtin", json={"sgtins": codes}
        )
        return {"ok": resp.status_code < 300}

    # --- Поставки ---
    def create_supply(self) -> str:
        resp = self._request("POST", "/api/v3/supplies", json={"name": "Поставка PoC"})
        return str(resp.json().get("id"))

    def add_order_to_supply(self, supply_id: str, order_id: str) -> dict:
        resp = self._request("PATCH", f"/api/v3/supplies/{supply_id}/orders/{order_id}")
        return {"ok": resp.status_code < 300}

    def close_supply(self, supply_id: str) -> dict:
        resp = self._request("PATCH", f"/api/v3/supplies/{supply_id}/deliver")
        return {"ok": resp.status_code < 300, "status": "closed"}

    def get_supply_qr(self, supply_id: str) -> WbSticker:
        resp = self._request(
            "GET", f"/api/v3/supplies/{supply_id}/barcode", params={"type": "png"}
        )
        return {"type": "png", "data": resp.json().get("file", "")}

    def get_supply_boxes_qr(self, supply_id: str, amount: int) -> list[WbSticker]:
        resp = self._request(
            "POST", f"/api/v3/supplies/{supply_id}/trbx", json={"trbxCount": amount}
        )
        trbx_ids = resp.json().get("trbxIds", [])
        return [{"type": "png", "data": t} for t in trbx_ids]
