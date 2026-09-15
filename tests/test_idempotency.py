"""P0-2: begin_idempotent() должен отличать "операция ещё выполняется" от
"операция завершена с пустым {} ответом" — иначе повтор/гонка выполняет
мутирующую операцию второй раз (двойной скан на приёмке удваивал приход)."""

import pytest

from fulfil.errors import AppError
from fulfil.services import idempotency


def test_no_key_always_proceeds(db):
    assert idempotency.begin_idempotent(db, "ep", None) is None
    assert idempotency.begin_idempotent(db, "ep", "") is None


def test_first_call_reserves_and_returns_none(db):
    assert idempotency.begin_idempotent(db, "ep", "key-1") is None


def test_in_progress_key_raises_conflict_not_falsy_empty_dict(db):
    """Ключ зарезервирован (complete_idempotent ещё не вызван) — раньше это
    возвращало {} и вызывающая сторона (`if cached:`) выполняла операцию
    заново; теперь — явный 409, отличимый от настоящего пустого ответа."""
    idempotency.begin_idempotent(db, "ep", "key-2")

    with pytest.raises(AppError) as exc_info:
        idempotency.begin_idempotent(db, "ep", "key-2")
    assert exc_info.value.status_code == 409
    assert exc_info.value.reason_code == "idempotency_in_progress"


def test_completed_key_returns_saved_response_including_empty_dict(db):
    idempotency.begin_idempotent(db, "ep", "key-3")
    idempotency.complete_idempotent(db, "ep", "key-3", {})

    result = idempotency.begin_idempotent(db, "ep", "key-3")
    assert result == {}
    assert result is not None


def test_completed_key_returns_saved_nonempty_response(db):
    idempotency.begin_idempotent(db, "ep", "key-4")
    idempotency.complete_idempotent(db, "ep", "key-4", {"ok": True, "lineId": 42})

    assert idempotency.begin_idempotent(db, "ep", "key-4") == {"ok": True, "lineId": 42}


def test_different_endpoints_do_not_share_a_key(db):
    idempotency.begin_idempotent(db, "ep-a", "shared-key")
    # Тот же ключ на другом endpoint — независимая резервация, не конфликт.
    assert idempotency.begin_idempotent(db, "ep-b", "shared-key") is None
