import datetime as dt

from fulfil.models.user import UserRole
from fulfil.schemas.common import CamelModel


class UserOut(CamelModel):
    """password_hash сознательно отсутствует — наружу хеш не отдаётся нигде."""

    id: int
    login: str
    full_name: str
    role: UserRole
    is_active: bool
    last_login_at: dt.datetime | None = None
    created_at: dt.datetime | None = None
    created_by: str | None = None


class UserCreateRequest(CamelModel):
    login: str
    full_name: str = ""
    password: str
    role: UserRole = UserRole.EMPLOYEE


class UserUpdateRequest(CamelModel):
    full_name: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class SetPasswordRequest(CamelModel):
    password: str
