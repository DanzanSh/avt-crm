from fastapi import APIRouter

from fulfil.pages import PAGES

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/pages")
def get_pages() -> list[dict]:
    """Реестр страниц — единственный источник правды (fulfil.pages.PAGES).
    Фронт строит сайдбар и ACL из этого ответа, не хранит собственной копии карты."""
    return [
        {"key": p.key, "href": p.href, "label": p.label, "section": p.section} for p in PAGES
    ]


@router.get("/public")
def get_public_settings() -> dict:
    return {"companyName": "Fulfil PoC", "brandShort": "Fulfil"}
