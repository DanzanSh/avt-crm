"""Учётные записи (problems.txt, п.5; authorization-model.md, этап 1).

Три уровня: хост (владелец, один на систему) → администратор (заводит сотрудников)
→ сотрудник. Права по разделам (этап 2 документа) сюда не входят: пока сотрудник
может всё, кроме того, что закрыто ролью хоста — восстановление и удаление клиентов.

Все правила «кто кого может менять» — здесь, а не в интерфейсе.
"""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfil.config import get_settings
from fulfil.errors import AppError, NotFoundError
from fulfil.models import audit
from fulfil.models.user import User, UserRole
from fulfil.passwords import hash_password, verify_password

MIN_PASSWORD_LEN = 6


def _forbidden(detail: str) -> AppError:
    return AppError(detail, status_code=403, reason_code="forbidden")


def get_live_user(db: Session, user_id: int) -> User | None:
    return db.scalar(select(User).where(User.id == user_id, User.archived_at.is_(None)))


def get_user_or_404(db: Session, user_id: int) -> User:
    user = get_live_user(db, user_id)
    if user is None:
        raise NotFoundError(f"Пользователь #{user_id} не найден.")
    return user


def authenticate(db: Session, login: str, password: str) -> User | None:
    """None и для неизвестного логина, и для неверного пароля, и для заблокированного —
    вызывающий отдаёт один и тот же 401, чтобы перебором нельзя было узнать логины."""
    user = db.scalar(select(User).where(User.login == login.strip(), User.archived_at.is_(None)))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return None
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return user


def ensure_host(db: Session) -> User | None:
    """Страховочный сев: пустая таблица users + заданные ADMIN_LOGIN/ADMIN_PASSWORD →
    заводим хоста. Без этого чистая БД после `docker compose up` не пускала бы никого."""
    if db.scalar(select(User.id).limit(1)) is not None:
        return None
    settings = get_settings()
    host = User(
        login=settings.admin_login, full_name="Владелец", role=UserRole.HOST,
        password_hash=hash_password(settings.admin_password), created_by="bootstrap",
    )
    db.add(host)
    db.commit()
    return host


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.archived_at.is_(None)).order_by(User.login)))


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AppError(
            f"Пароль слишком короткий — нужно не меньше {MIN_PASSWORD_LEN} символов.",
            status_code=400, reason_code="weak_password",
        )


def _check_can_manage(actor: User, target: User) -> None:
    """Хоста не меняет никто, кроме него самого (и то только имя/пароль);
    администраторов — только хост; сотрудников — хост и администраторы."""
    if target.role == UserRole.HOST and actor.id != target.id:
        raise _forbidden("Учётную запись владельца может менять только он сам.")
    if target.role == UserRole.ADMIN and actor.role != UserRole.HOST and actor.id != target.id:
        raise _forbidden("Администраторов может менять только владелец.")


def _check_can_assign(actor: User, role: UserRole) -> None:
    if role == UserRole.HOST:
        raise _forbidden("Владелец в системе один — назначить ещё одного нельзя.")
    if role == UserRole.ADMIN and actor.role != UserRole.HOST:
        raise _forbidden("Назначать администраторов может только владелец.")


def create_user(
    db: Session, actor: User, *, login: str, full_name: str, password: str, role: UserRole,
) -> User:
    login = login.strip()
    if not login:
        raise AppError("Логин не может быть пустым.", status_code=400, reason_code="invalid_login")
    _check_can_assign(actor, role)
    _check_password(password)
    if db.scalar(select(User.id).where(User.login == login, User.archived_at.is_(None))) is not None:
        raise AppError(f"Логин «{login}» уже занят.", status_code=409, reason_code="login_exists")
    user = User(
        login=login, full_name=full_name.strip(), role=role,
        password_hash=hash_password(password), created_by=actor.login,
    )
    db.add(user)
    db.flush()
    audit.record(
        db, entity_type="user", entity_id=user.id, action="create", actor=actor.login,
        changes={"login": login, "role": role.value},
    )
    db.commit()
    db.refresh(user)
    return user


def update_user(
    db: Session, actor: User, user: User, *, full_name: str | None = None,
    role: UserRole | None = None, is_active: bool | None = None,
) -> User:
    _check_can_manage(actor, user)
    changes: dict = {}
    if full_name is not None and full_name.strip() != user.full_name:
        changes["fullName"] = [user.full_name, full_name.strip()]
        user.full_name = full_name.strip()
    if role is not None and role != user.role:
        if user.role == UserRole.HOST:
            raise _forbidden("Роль владельца изменить нельзя.")
        _check_can_assign(actor, role)
        changes["role"] = [user.role.value, role.value]
        user.role = role
    if is_active is not None and is_active != user.is_active:
        if user.role == UserRole.HOST or user.id == actor.id:
            raise _forbidden("Нельзя заблокировать владельца или самого себя.")
        changes["isActive"] = [user.is_active, is_active]
        user.is_active = is_active
    if changes:
        audit.record(db, entity_type="user", entity_id=user.id, action="update", actor=actor.login, changes=changes)
        db.commit()
        db.refresh(user)
    return user


def set_password(db: Session, actor: User, user: User, password: str) -> None:
    _check_can_manage(actor, user)
    _check_password(password)
    user.password_hash = hash_password(password)
    # сам пароль и хеш в аудит не пишем — только факт смены
    audit.record(db, entity_type="user", entity_id=user.id, action="password_reset", actor=actor.login)
    db.commit()


def archive_user(db: Session, actor: User, user: User) -> None:
    _check_can_manage(actor, user)
    if user.role == UserRole.HOST or user.id == actor.id:
        raise _forbidden("Нельзя удалить владельца или самого себя.")
    user.archived_at = dt.datetime.now(dt.timezone.utc)
    audit.record(db, entity_type="user", entity_id=user.id, action="delete", actor=actor.login)
    db.commit()
