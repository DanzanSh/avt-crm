"""Авторизация: JWT + учётные записи в БД (problems.txt, п.5; authorization-model.md, этап 1).

Токен несёт только sub (логин), uid и exp. Роль и сам факт «учётка жива и активна»
читаются из БД на каждый запрос: заблокированный или архивированный пользователь
получает 401 сразу, не дожидаясь истечения токена, а смена роли действует без
повторного входа.

get_current_user по-прежнему отдаёт словарь ({sub, uid, role, fullName}) — роутеры
берут из него actor через user.get("sub"), контракт для них не меняется.
"""

import datetime as dt

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from fulfil.config import get_settings
from fulfil.db import get_db
from fulfil.errors import AppError
from fulfil.models.user import User, UserRole
from fulfil.services.users import get_live_user

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)

_SESSION_INVALID = "Сессия истекла или недействительна"


def create_access_token(user: User) -> str:
    settings = get_settings()
    expire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": user.login, "uid": user.id, "exp": int(expire.timestamp())}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_SESSION_INVALID) from exc


def user_claims(user: User) -> dict:
    return {"sub": user.login, "uid": user.id, "role": user.role.value, "fullName": user.full_name}


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> dict:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется авторизация")
    payload = decode_token(creds.credentials)
    uid = payload.get("uid")
    # Токены до появления учёток (без uid) недействительны — нужен повторный вход.
    user = get_live_user(db, uid) if isinstance(uid, int) else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_SESSION_INVALID)
    return user_claims(user)


def is_host(user: dict) -> bool:
    return user.get("role") == UserRole.HOST.value


def require_host(user: dict = Depends(get_current_user)) -> dict:
    """Действия только владельца: восстановление и удаление клиентов (problems.txt, п.5)."""
    if not is_host(user):
        raise AppError(
            "Это действие доступно только владельцу системы.",
            status_code=403, reason_code="forbidden",
        )
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Управление учётными записями — владелец и администраторы."""
    if user.get("role") not in (UserRole.HOST.value, UserRole.ADMIN.value):
        raise AppError(
            "Управлять пользователями могут только владелец и администраторы.",
            status_code=403, reason_code="forbidden",
        )
    return user
