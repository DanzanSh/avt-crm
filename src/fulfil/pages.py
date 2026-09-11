"""Реестр страниц — единственный источник правды.

В эталоне (Logidex CRM) карта page -> href рукописно продублирована в трёх местах
(js/sidebar.js, js/auth-guard.js, и бэкенд api/app/utils/pages.py) и они расходятся.
Здесь регистр один; фронт получает его целиком через GET /api/v1/settings/pages
и не хранит собственной копии.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PageDef:
    key: str
    href: str
    label: str
    section: str


PAGES: list[PageDef] = [
    PageDef("storage", "/storage-map.html", "Карта склада", "Склад"),
    PageDef("receiving", "/receiving.html", "Приёмка", "Склад"),
    PageDef("stock", "/stock.html", "Остатки", "Склад"),
    PageDef("products", "/products.html", "Товары", "Справочники"),
    PageDef("clients", "/clients.html", "Клиенты", "Справочники"),
    PageDef("orders", "/fbs/orders.html", "Заказы ФБС", "ФБС"),
    PageDef("picking", "/fbs/picking.html", "Сборка заказа", "ФБС"),
    PageDef("supplies", "/fbs/supplies.html", "Поставки", "ФБС"),
]

HREF_TO_PAGE: dict[str, str] = {p.href: p.key for p in PAGES}
PAGE_TO_HREF: dict[str, str] = {p.key: p.href for p in PAGES}
