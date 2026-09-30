"""Rate limiting на /auth/login (план «безопасность», п.2)."""

from conftest import make_user

from fulfil.config import get_settings
from fulfil.ratelimit import SlidingWindowLimiter


def test_sliding_window_limiter_basic():
    lim = SlidingWindowLimiter(limit=3, window_sec=60)
    assert lim.hit("a") and lim.hit("a") and lim.hit("a")
    assert not lim.hit("a")
    # другой ключ не задет
    assert lim.hit("b")


def test_sliding_window_limiter_reset():
    lim = SlidingWindowLimiter(limit=1, window_sec=60)
    assert lim.hit("a")
    assert not lim.hit("a")
    lim.reset("a")
    assert lim.hit("a")


def test_login_blocked_after_limit_exceeded(api, db):
    make_user(db, "ivan")
    limit = get_settings().login_rate_limit

    for _ in range(limit):
        resp = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "wrong"})
        assert resp.status_code == 401

    blocked = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "wrong"})
    assert blocked.status_code == 429
    assert blocked.json()["reasonCode"] == "rate_limited"

    # даже с верным паролем — лимит уже исчерпан
    still_blocked = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "secret123"})
    assert still_blocked.status_code == 429


def test_successful_login_resets_limiter(api, db):
    make_user(db, "ivan")
    limit = get_settings().login_rate_limit

    for _ in range(limit - 1):
        resp = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "wrong"})
        assert resp.status_code == 401

    ok = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "secret123"})
    assert ok.status_code == 200

    # счётчик логина сброшен — снова доступно `limit` неудачных попыток
    for _ in range(limit):
        resp = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "wrong"})
        assert resp.status_code == 401


def test_different_logins_do_not_block_each_other(api, db):
    make_user(db, "ivan")
    make_user(db, "petr")
    limit = get_settings().login_rate_limit

    for _ in range(limit):
        resp = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "wrong"})
        assert resp.status_code == 401

    blocked = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "wrong"})
    assert blocked.status_code == 429

    # petr не заблокирован по конкретному логину, хотя один и тот же IP —
    # ip-лимит (limit*4) шире, чем один пользовательский лимит
    ok = api.post("/api/v1/auth/login", json={"login": "petr", "password": "secret123"})
    assert ok.status_code == 200
