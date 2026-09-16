"""Учётные записи и вход (problems.txt, п.5; authorization-model.md, этап 1)."""

import datetime as dt

import pytest
from conftest import auth_headers, make_user
from jose import jwt

from fulfil.config import get_settings
from fulfil.errors import AppError
from fulfil.models.user import User, UserRole
from fulfil.passwords import hash_password, verify_password
from fulfil.services import users as users_service


def test_password_roundtrip():
    stored = hash_password("secret123")
    assert stored.startswith("scrypt$") and "secret123" not in stored
    assert verify_password("secret123", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("secret123", "garbage")


def test_authenticate_rejects_wrong_unknown_inactive_archived(db):
    make_user(db, "ivan")
    make_user(db, "blocked", is_active=False)
    make_user(db, "gone", archived_at=dt.datetime.now(dt.timezone.utc))

    assert users_service.authenticate(db, "ivan", "secret123").login == "ivan"
    assert users_service.authenticate(db, "ivan", "nope") is None
    assert users_service.authenticate(db, "nobody", "secret123") is None
    assert users_service.authenticate(db, "blocked", "secret123") is None
    assert users_service.authenticate(db, "gone", "secret123") is None


def test_ensure_host_seeds_only_empty_table(db):
    host = users_service.ensure_host(db)
    assert host.role == UserRole.HOST and host.login == get_settings().admin_login
    assert users_service.ensure_host(db) is None
    assert db.query(User).count() == 1


def test_login_same_401_for_unknown_and_wrong_password(api, db):
    make_user(db, "ivan")
    ok = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "secret123"})
    assert ok.status_code == 200
    token = ok.json()["accessToken"]

    wrong = api.post("/api/v1/auth/login", json={"login": "ivan", "password": "x"})
    unknown = api.post("/api/v1/auth/login", json={"login": "nobody", "password": "x"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()

    me = api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json() == {"id": me.json()["id"], "login": "ivan", "fullName": "ivan", "role": "employee"}


def test_blocked_user_token_stops_working_immediately(api, db):
    user = make_user(db, "ivan")
    headers = auth_headers(user)
    assert api.get("/api/v1/auth/me", headers=headers).status_code == 200

    user.is_active = False
    db.commit()
    assert api.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_old_token_without_uid_rejected(api):
    settings = get_settings()
    legacy = jwt.encode(
        {"sub": "admin", "role": "admin", "exp": int(dt.datetime.now().timestamp()) + 600},
        settings.jwt_secret, algorithm="HS256",
    )
    assert api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 401


def test_password_hash_not_exposed(api, db):
    host = make_user(db, "owner", role="host")
    body = api.get("/api/v1/users", headers=auth_headers(host)).text
    assert "passwordHash" not in body and "scrypt$" not in body


# --- Кто кого может менять ---


def test_admin_cannot_create_host_or_admin(db):
    admin = make_user(db, "admin", role="admin")
    for role in (UserRole.HOST, UserRole.ADMIN):
        with pytest.raises(AppError) as exc_info:
            users_service.create_user(db, admin, login=f"x-{role.value}", full_name="", password="secret123", role=role)
        assert exc_info.value.status_code == 403
    employee = users_service.create_user(
        db, admin, login="worker", full_name="Рабочий", password="secret123", role=UserRole.EMPLOYEE,
    )
    assert employee.created_by == "admin"


def test_nobody_else_manages_host(db):
    host = make_user(db, "owner", role="host")
    admin = make_user(db, "admin", role="admin")
    with pytest.raises(AppError):
        users_service.set_password(db, admin, host, "newpass123")
    with pytest.raises(AppError):
        users_service.update_user(db, host, host, is_active=False)
    with pytest.raises(AppError):
        users_service.archive_user(db, host, host)
    users_service.set_password(db, host, host, "newpass123")  # сам себе — можно
    assert users_service.authenticate(db, "owner", "newpass123") is not None


def test_login_of_archived_user_can_be_reused(db):
    host = make_user(db, "owner", role="host")
    old = users_service.create_user(db, host, login="ivan", full_name="", password="secret123", role=UserRole.EMPLOYEE)
    with pytest.raises(AppError):
        users_service.create_user(db, host, login="ivan", full_name="", password="secret123", role=UserRole.EMPLOYEE)
    users_service.archive_user(db, host, old)
    users_service.create_user(db, host, login="ivan", full_name="", password="secret123", role=UserRole.EMPLOYEE)


def test_employee_has_no_access_to_users(api, db):
    employee = make_user(db, "worker")
    resp = api.get("/api/v1/users", headers=auth_headers(employee))
    assert resp.status_code == 403
    assert resp.json()["reasonCode"] == "forbidden"
