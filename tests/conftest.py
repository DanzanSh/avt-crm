import os

# Обязательные настройки — выставляем до любого импорта fulfil, иначе Settings()
# упадёт на сборе тестов. Реальное окружение / CI всё равно перебивает setdefault.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ADMIN_LOGIN", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fulfil.db import Base


@pytest.fixture()
def db():
    """SQLite in-memory для юнитов на инварианты — быстро, без docker.
    FOR UPDATE (row lock в receiving.place_stock) на SQLite не поддерживается
    и просто игнорируется — гонки в юнит-тестах не проверяем, только логику."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
