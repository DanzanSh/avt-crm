"""X-Idempotency-Key на мутирующих складских операциях (DEV-PLAN.md, конвенции API;
FEATURES-PLAN.md, дефект №5 и этап 4.4).

Двухфазная схема "резервируем — потом заполняем": ключ вставляется ДО выполнения
операции (уникальный индекс (endpoint, key) ловит гонку), а не после — иначе два
конкурентных запроса с одним ключом успели бы оба выполнить операцию до того,
как кто-то из них записал бы идемпотентность.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.models.idempotency import IdempotencyKey


def begin_idempotent(db: Session, endpoint: str, key: str | None) -> dict | None:
    """Без ключа — идемпотентность не участвует, возвращает None (выполняем как обычно).
    С ключом: если операция уже завершена — возвращает сохранённый ответ (даже пустой {}
    первой фазы, если операция ещё не завершилась или упала — тогда лучше вручную
    проверить состояние, чем выполнить операцию повторно). Иначе резервирует ключ."""
    if not key:
        return None

    existing = db.scalar(
        select(IdempotencyKey).where(IdempotencyKey.endpoint == endpoint, IdempotencyKey.key == key)
    )
    if existing is not None:
        return existing.response

    row = IdempotencyKey(endpoint=endpoint, key=key, status_code=0, response={})
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(IdempotencyKey).where(IdempotencyKey.endpoint == endpoint, IdempotencyKey.key == key)
        )
        return existing.response if existing is not None else None
    return None


def complete_idempotent(db: Session, endpoint: str, key: str | None, response: dict) -> None:
    if not key:
        return
    row = db.scalar(
        select(IdempotencyKey).where(IdempotencyKey.endpoint == endpoint, IdempotencyKey.key == key)
    )
    if row is not None:
        row.response = response
        row.status_code = 200
        db.commit()
