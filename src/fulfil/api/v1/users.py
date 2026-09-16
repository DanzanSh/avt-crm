"""Учётные записи (problems.txt, п.5) — только владелец и администраторы.
Кто кого может менять, проверяет services.users, не роутер."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fulfil.auth import require_admin
from fulfil.db import get_db
from fulfil.models.user import User
from fulfil.schemas.user import SetPasswordRequest, UserCreateRequest, UserOut, UserUpdateRequest
from fulfil.services import users as users_service

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])


def _actor_user(db: Session, user: dict) -> User:
    return users_service.get_user_or_404(db, user["uid"])


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return users_service.list_users(db)


@router.post("", response_model=UserOut)
def create_user(body: UserCreateRequest, db: Session = Depends(get_db), user: dict = Depends(require_admin)) -> User:
    return users_service.create_user(
        db, _actor_user(db, user), login=body.login, full_name=body.full_name,
        password=body.password, role=body.role,
    )


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int, body: UserUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(require_admin)
) -> User:
    target = users_service.get_user_or_404(db, user_id)
    return users_service.update_user(
        db, _actor_user(db, user), target, full_name=body.full_name, role=body.role, is_active=body.is_active,
    )


@router.post("/{user_id}/password")
def set_password(
    user_id: int, body: SetPasswordRequest, db: Session = Depends(get_db), user: dict = Depends(require_admin)
) -> dict:
    target = users_service.get_user_or_404(db, user_id)
    users_service.set_password(db, _actor_user(db, user), target, body.password)
    return {"ok": True}


@router.delete("/{user_id}")
def archive_user(user_id: int, db: Session = Depends(get_db), user: dict = Depends(require_admin)) -> dict:
    target = users_service.get_user_or_404(db, user_id)
    users_service.archive_user(db, _actor_user(db, user), target)
    return {"ok": True}
