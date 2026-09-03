"""Серверная классификация отсканированного кода (FEATURES-PLAN.md, этап 4.3).

Фронт больше не решает по стадии экрана, что он отсканировал (дефект №3: скан
неизвестного баркода на приёмке молча брал products[0] из результатов поиска).
Один эндпоинт пробует код как есть и после исправления русской раскладки; если
оба варианта существуют и указывают на РАЗНЫЕ сущности — guard неоднозначности
(AmbiguousCodeError) вместо угадывания (см. DEV-PLAN.md, приём placement-items.html).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.errors import AmbiguousCodeError, NotFoundError
from fulfil.models.product import Product
from fulfil.services.storage import resolve_location

# Та же таблица, что web/js/scan-layout.js — исправление русской раскладки на сервере
# для guard'а неоднозначности (не для замены клиентского исправления, а как второй кандидат).
_RU_TO_EN = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u", "ш": "i", "щ": "o", "з": "p",
    "х": "[", "ъ": "]", "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k",
    "д": "l", "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n", "ь": "m",
    "б": ",", "ю": ".", "ё": "`",
}
_RU_TO_EN.update({k.upper(): v.upper() for k, v in list(_RU_TO_EN.items())})


def fix_ru_layout(s: str) -> str:
    return "".join(_RU_TO_EN.get(ch, ch) for ch in s)


def _find_product(db: Session, code: str) -> Product | None:
    return db.scalar(select(Product).where(Product.barcode == code, Product.archived_at.is_(None)))


def _find_cell(db: Session, code: str):
    try:
        return resolve_location(db, code)
    except NotFoundError:
        return None


def resolve_scan(db: Session, raw_code: str) -> tuple[str, object]:
    """Возвращает (kind, entity), kind ∈ {'product', 'cell'}."""
    raw = raw_code.strip()
    if not raw:
        raise NotFoundError("Пустой код.")

    fixed = fix_ru_layout(raw)
    candidates = [raw] if fixed == raw else [raw, fixed]

    hits: list[tuple[str, str, object]] = []  # (candidate, kind, entity)
    for cand in candidates:
        product = _find_product(db, cand)
        if product is not None:
            hits.append((cand, "product", product))
            continue  # баркод найден — не пытаемся трактовать тот же кандидат как адрес
        cell = _find_cell(db, cand)
        if cell is not None:
            hits.append((cand, "cell", cell))

    if not hits:
        raise NotFoundError(f'Код «{raw_code}» не распознан — не найден ни товар, ни ячейка.')

    if len(hits) > 1:
        first_kind, first_entity = hits[0][1], hits[0][2]
        for _, kind, entity in hits[1:]:
            if (kind, getattr(entity, "id", None)) != (first_kind, getattr(first_entity, "id", None)):
                raise AmbiguousCodeError(
                    f'Код «{raw_code}» неоднозначен: как есть и после исправления раскладки '
                    f'указывает на разные сущности.'
                )

    return hits[0][1], hits[0][2]
