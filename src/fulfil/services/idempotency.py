"""X-Idempotency-Key на мутирующих складских операциях (DEV-PLAN.md, конвенции API;
FEATURES-PLAN.md, дефект №5 и этап 4.4).

Двухфазная схема "резервируем — потом заполняем": ключ вставляется ДО выполнения
операции (уникальный индекс (endpoint, key) ловит гонку), а не после — иначе два
конкурентных запроса с одним ключом успели бы оба выполнить операцию до того,
как кто-то из них записал бы идемпотентность.

status_code=0 отмечает "зарезервировано, но ещё не выполнено" — отличать это
состояние от "выполнено с пустым {} ответом" обязательно (P0-2): раньше функция
возвращала одно и то же — сохранённый response, — в обоих случаях, а вызывающая
сторона проверяла результат как `if cached:`. Пустой словарь ложен в Python, поэтому
и настоящий "в процессе" (response ещё {}), и гипотетический легитимный пустой
ответ одинаково проваливали проверку, и операция выполнялась повторно — именно то,
что идемпотентность должна была предотвратить (двойной скан на приёмке удваивал
приход остатка). Теперь при "в процессе" бросается 409, а не возвращается {}.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.errors import AppError
from fulfil.models.idempotency import IdempotencyKey

def _in_progress_error() -> AppError:
    return AppError(
        "Операция с этим ключом идемпотентности уже выполняется — дождитесь ответа "
        "первого запроса и не повторяйте его, пока он не завершится.",
        status_code=409,
        reason_code="idempotency_in_progress",
        what_to_do="Подождите несколько секунд и проверьте актуальное состояние, не отправляя запрос повторно.",
    )


def _resolve(existing: IdempotencyKey | None) -> dict | None:
    if existing is None:
        return None
    if existing.status_code == 0:
        raise _in_progress_error()
    return existing.response


def begin_idempotent(db: Session, endpoint: str, key: str | None) -> dict | None:
    """Без ключа — идемпотентность не участвует, возвращает None (выполняем как обычно).
    С ключом: операция уже завершена — возвращает сохранённый ответ (в том числе пустой
    {}, если таким был настоящий финальный ответ). Операция ещё выполняется тем же или
    параллельным запросом — AppError 409 idempotency_in_progress. Иначе резервирует ключ
    и возвращает None (выполняем как обычно)."""
    if not key:
        return None

    existing = db.scalar(
        select(IdempotencyKey).where(IdempotencyKey.endpoint == endpoint, IdempotencyKey.key == key)
    )
    if existing is not None:
        return _resolve(existing)

    row = IdempotencyKey(endpoint=endpoint, key=key, status_code=0, response={})
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(IdempotencyKey).where(IdempotencyKey.endpoint == endpoint, IdempotencyKey.key == key)
        )
        return _resolve(existing)
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
