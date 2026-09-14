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
from fulfil.errors import WbApiError
from fulfil.integrations.wb.base import WbCardsPage, WbCursor, WbOffice, WbOrder, WbSticker, WbWarehouse
from fulfil.models.wb_log import WbApiLog

_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 0.5


def _wb_hints(client_name: str | None) -> dict:
    who = f'клиента «{client_name}»' if client_name else "клиента"
    return {
        401: f"Ключ API {who} истёк или не подходит — обновите его в разделе «Клиенты».",
        403: f"У ключа API {who} не хватает прав (нужны категории «Контент» и «Маркетплейс»).",
        404: "WB не нашёл ресурс по этому пути — вероятно, изменился контракт API.",
        429: "WB временно ограничил частоту запросов — повторите синхронизацию через минуту.",
    }


def _as_wb_error(method: str, path: str, exc: Exception, client_name: str | None = None) -> WbApiError:
    """Переводит любую сетевую ошибку клиента WB в доменный WbApiError, чтобы
    ручка отдала внятный конверт, а не 500 со стектрейсом httpx."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        body = (exc.response.text or "").strip().replace("\n", " ")
        if len(body) > 300:
            body = body[:300] + "…"
        detail = f"Wildberries API вернул {code} на {method} {path}."
        if body:
            detail += f" Ответ: {body}"
        return WbApiError(detail, upstream_status=code, what_to_do=_wb_hints(client_name).get(code))
    return WbApiError(
        f"Не удалось получить ответ от Wildberries API ({method} {path}): {exc}.",
        what_to_do="WB недоступен или таймаут — повторите синхронизацию позже.",
    )


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


def _first_photo(card: dict) -> str:
    return _first_dict(card.get("photos")).get("big", "") or ""


def _card_sizes(card: dict) -> list[dict]:
    """Разворачивает sizes[] карточки в отдельные записи товаров (Этап 1, п.1.1:
    товар = размер карточки, а не карточка целиком). Размер без skus (ещё не привязан
    штрихкод в личном кабинете WB) пропускается — на него нельзя ни принять, ни продать."""
    sizes = card.get("sizes")
    if not isinstance(sizes, list):
        return []
    out = []
    for size in sizes:
        if not isinstance(size, dict):
            continue
        skus = size.get("skus")
        if not isinstance(skus, list) or not skus:
            continue
        out.append(
            {
                "chrtId": size.get("chrtID"),
                "barcode": skus[0],
                "size": size.get("techSize", "") or "",
            }
        )
    return out


class WBHttpClient:
    """Токен и client_id теперь приходят от вызывающей стороны (один экземпляр на
    вызов — см. integrations/wb/__init__.get_wb_client), а не из глобальных settings:
    у каждого клиента фулфилмента свой кабинет WB со своим ключом (Этап 1)."""

    def __init__(self, token: str, *, client_id: int | None = None, client_name: str | None = None) -> None:
        settings = get_settings()
        self._base = settings.wb_api_base.rstrip("/")
        self._content_base = settings.wb_content_api_base.rstrip("/")
        self._token = token
        self._client_id = client_id
        self._client_name = client_name

    def _headers(self) -> dict:
        return {"Authorization": self._token, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, base: str | None = None, **kwargs) -> httpx.Response:
        if not self._token:
            raise WbApiError(
                "У клиента не задан ключ API — запрос к WB невозможен.",
                what_to_do="Введите ключ API в разделе «Клиенты».",
            )
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
                # 4xx (кроме 429) — контракт/токен, ретрай не поможет: падаем сразу.
                client_error = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and 400 <= exc.response.status_code < 500
                    and exc.response.status_code != 429
                )
                if not client_error and attempt < _MAX_RETRIES - 1:
                    time.sleep(_BACKOFF_BASE_SEC * (2**attempt))
                    continue
                break
            finally:
                duration_ms = int((time.monotonic() - started) * 1000)
                self._log(method, path, status_code, duration_ms, error, client_id=self._client_id)
        assert last_exc is not None
        raise _as_wb_error(method, path, last_exc, client_name=self._client_name) from last_exc

    @staticmethod
    def _log(
        method: str, path: str, status: int | None, duration_ms: int, error: str | None,
        *, client_id: int | None = None,
    ) -> None:
        db = SessionLocal()
        try:
            db.add(
                WbApiLog(
                    client_id=client_id,
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

    # --- Диагностика (Этап 1, «Проверить подключение») ---
    def ping(self) -> dict:
        """Best-effort: у каждого API-хоста WB есть /ping, не требующий прав сверх
        авторизации. Ошибка на одном хосте не мешает проверить второй."""
        result: dict = {}
        for name, base in (("marketplace", self._base), ("content", self._content_base)):
            try:
                self._request("GET", "/ping", base=base)
                result[name] = {"ok": True, "error": None}
            except WbApiError as exc:
                result[name] = {"ok": False, "error": exc.detail}
        return result

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
        cards = []
        for c in data.get("cards", []):
            common = {
                "nmId": c.get("nmID"),
                "imtId": c.get("imtID"),
                "vendorCode": c.get("vendorCode", ""),
                "name": c.get("title", ""),
                "brand": c.get("brand", ""),
                "color": _card_color(c),
                "imageUrl": _first_photo(c),
            }
            for size in _card_sizes(c):
                cards.append({**common, **size})
        c = data.get("cursor") or {}
        next_cursor: WbCursor = {
            "updatedAt": c.get("updatedAt"),
            "nmID": c.get("nmID"),
            "total": c.get("total", 0),
        }
        return {"cards": cards, "cursor": next_cursor}

    # --- Склад WB клиента (Этап 2, п.2.2) ---
    # best-effort: пути и формы ответов не обкатаны против боевого токена, см.
    # предупреждение в шапке файла и docs/wb-api-contract.md.
    def list_offices(self) -> list[WbOffice]:
        resp = self._request("GET", "/api/v3/offices")
        return [
            {"id": o.get("id"), "name": o.get("name", ""), "address": o.get("address", "")}
            for o in resp.json() or []
        ]

    def list_warehouses(self) -> list[WbWarehouse]:
        resp = self._request("GET", "/api/v3/warehouses")
        return [{"id": str(w.get("id")), "name": w.get("name", "")} for w in resp.json() or []]

    def create_warehouse(self, name: str, office_id: int) -> WbWarehouse:
        resp = self._request(
            "POST", "/api/v3/warehouses", json={"name": name, "officeId": office_id}
        )
        data = resp.json()
        return {"id": str(data.get("id")), "name": name}

    # --- Остатки FBS ---
    def get_fbs_stocks(self, warehouse_id: str, barcodes: list[str]) -> dict[str, int]:
        if not barcodes:
            return {}
        resp = self._request(
            "POST", f"/api/v3/stocks/{warehouse_id}", json={"skus": barcodes}
        )
        data = resp.json()
        return {str(s.get("sku")): int(s.get("amount", 0)) for s in data.get("stocks", [])}

    def set_fbs_stocks(self, warehouse_id: str, amounts: dict[str, int]) -> dict:
        body = {"stocks": [{"sku": sku, "amount": amount} for sku, amount in amounts.items()]}
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
