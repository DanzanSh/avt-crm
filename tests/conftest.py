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


def make_client(db, name: str = "Тестовый клиент"):
    """Плоский хелпер (не фикстура) — заводит клиента для тестов, которым нужно
    больше одного (test_clients.py: два кабинета в одном тесте). Имя уникально
    среди живых (uq_client_name_live), так что при повторном вызове в одном
    тесте нужно передавать разные name."""
    from fulfil.models.client import Client

    client = Client(name=name)
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


@pytest.fixture()
def seller(db):
    """Клиент по умолчанию для тестов, которым нужен ровно один — Product/Order/
    Supply/Receipt теперь требуют client_id (Этап 1). Названа НЕ `client`, чтобы
    не путать с fastapi TestClient."""
    return make_client(db, name="Тестовый клиент")
