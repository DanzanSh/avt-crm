from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from fulfil.auth import create_access_token, get_current_user
from fulfil.db import get_db
from fulfil.schemas.common import CamelModel
from fulfil.services import users as users_service

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(CamelModel):
    login: str
    password: str


class LoginResponse(CamelModel):
    access_token: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """Вход по учётной записи из БД (problems.txt, п.5). Неизвестный логин, неверный
    пароль и заблокированный пользователь — один и тот же 401."""
    user = users_service.authenticate(db, body.login, body.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    return LoginResponse(access_token=create_access_token(user))


@router.get("/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    """Кто вошёл — фронт по роли решает, показывать ли «Пользователей» и действия хоста."""
    return {"id": user["uid"], "login": user["sub"], "fullName": user["fullName"], "role": user["role"]}
