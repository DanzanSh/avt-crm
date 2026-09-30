from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from fulfil.auth import create_access_token, get_current_user
from fulfil.config import get_settings
from fulfil.db import get_db
from fulfil.errors import AppError
from fulfil.ratelimit import SlidingWindowLimiter
from fulfil.schemas.common import CamelModel
from fulfil.services import users as users_service

router = APIRouter(prefix="/auth", tags=["auth"])

_settings = get_settings()
# Ключ «IP+логин» — основной лимит; более мягкий лимит просто по IP (x4) не даёт
# перебирать логины через разные учётки с одного адреса.
_login_limiter = SlidingWindowLimiter(_settings.login_rate_limit, _settings.login_rate_window_sec)
_ip_limiter = SlidingWindowLimiter(_settings.login_rate_limit * 4, _settings.login_rate_window_sec)


def _rate_limited_error(window_sec: int) -> AppError:
    minutes = max(1, round(window_sec / 60))
    return AppError(
        f"Слишком много попыток входа. Повторите через {minutes} мин.",
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        reason_code="rate_limited",
    )


class LoginRequest(CamelModel):
    login: str
    password: str


class LoginResponse(CamelModel):
    access_token: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    """Вход по учётной записи из БД (problems.txt, п.5). Неизвестный логин, неверный
    пароль и заблокированный пользователь — один и тот же 401.

    Rate limit до обращения к БД и scrypt (см. fulfil.ratelimit): IP не доверяем
    заголовку X-Forwarded-For — перед приложением стоит прокси платформы, но
    TrustedHostMiddleware/список доверенных прокси не настроен, и заголовок
    подделывается тривиально."""
    ip = request.client.host if request.client else "unknown"
    login_key = f"{ip}:{body.login.strip().lower()}"
    if not _ip_limiter.hit(ip):
        raise _rate_limited_error(_settings.login_rate_window_sec)
    if not _login_limiter.hit(login_key):
        raise _rate_limited_error(_settings.login_rate_window_sec)

    user = users_service.authenticate(db, body.login, body.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    _login_limiter.reset(login_key)
    return LoginResponse(access_token=create_access_token(user))


@router.get("/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    """Кто вошёл — фронт по роли решает, показывать ли «Пользователей» и действия хоста."""
    return {"id": user["uid"], "login": user["sub"], "fullName": user["fullName"], "role": user["role"]}
