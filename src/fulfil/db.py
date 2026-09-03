from collections.abc import Generator
from typing import TypeVar

from sqlalchemy import Select, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from fulfil.config import get_settings


class Base(DeclarativeBase):
    pass


_M = TypeVar("_M")


def live(stmt: Select, model: type[_M], column: str = "deleted_at") -> Select:
    """Фильтр "не удалено" — один хелпер на все soft-delete сущности (zones/racks/cells:
    deleted_at, products: archived_at), чтобы не забыть его в новом запросе."""
    return stmt.where(getattr(model, column).is_(None))


_settings = get_settings()
engine = create_engine(_settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
