from fastapi import APIRouter, HTTPException, status

from fulfil.auth import create_access_token
from fulfil.config import get_settings
from fulfil.schemas.common import CamelModel

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(CamelModel):
    login: str
    password: str


class LoginResponse(CamelModel):
    access_token: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    settings = get_settings()
    if body.login != settings.admin_login or body.password != settings.admin_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    return LoginResponse(access_token=create_access_token(body.login))
