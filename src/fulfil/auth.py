"""Однопользовательская авторизация PoC.

Deny-by-default по pageKey + allowedPages из эталона здесь не нужен буквально —
в PoC один пользователь с полным доступом (role=admin), но сам механизм
(JWT + AuthGuard.checkPageAccess на фронте) сохранён, чтобы расширение до
нескольких ролей в MVP не требовало менять контракт.
"""

import datetime as dt

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from fulfil.config import get_settings

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


class TokenPayload(BaseModel):
    sub: str
    role: str = "admin"
    allowed_pages: list[str] = []
    exp: int


def create_access_token(username: str) -> str:
    settings = get_settings()
    expire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": username, "role": "admin", "allowedPages": [], "exp": int(expire.timestamp())}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия истекла или недействительна",
        ) from exc


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется авторизация"
        )
    return decode_token(creds.credentials)
