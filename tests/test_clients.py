"""Этап 1 — Клиенты: несколько кабинетов WB.

Покрывает: шифрование ключа туда-обратно, что API/схема не отдаёт ключ, exp из JWT,
синк мока по двум клиентам (раздельные client_id и курсоры), разрешённый одинаковый
баркод у двух клиентов и 409 внутри одного, карточку с несколькими размерами,
архивацию клиента с остатком, transfer_to_fbs без склада, неоднозначность скана
баркода между клиентами (уже в tests/test_scan.py — здесь не дублируем)."""

import base64
import datetime as dt
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from conftest import make_client
from fulfil.errors import AppError
from fulfil.integrations.wb import get_wb_client
from fulfil.integrations.wb.mock import WBMockClient
from fulfil.models.client import Client
from fulfil.models.integration_state import IntegrationState
from fulfil.models.product import Product
from fulfil.schemas.client import ClientOut
from fulfil.secrets import decrypt_api_key, encrypt_api_key, jwt_expires_at, mask_api_key
from fulfil.services import clients as clients_service
from fulfil.services.products import create_product, sync_products_from_wb
from fulfil.services.receiving import place_stock
from fulfil.services.stock import transfer_to_fbs
from fulfil.services.storage import generate_cells


# --- Шифрование -------------------------------------------------------------


def test_encrypt_decrypt_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr("fulfil.secrets.get_settings", lambda: type("S", (), {"client_secrets_key": key})())
    enc = encrypt_api_key("secret-wb-token")
    assert enc != "secret-wb-token"
    assert decrypt_api_key(enc) == "secret-wb-token"


def test_encrypt_without_secrets_key_raises_app_error(monkeypatch):
    monkeypatch.setattr(
        "fulfil.secrets.get_settings", lambda: type("S", (), {"client_secrets_key": ""})()
    )
    with pytest.raises(AppError) as exc_info:
        encrypt_api_key("secret")
    assert exc_info.value.reason_code == "no_secrets_key"
    assert "Fernet.generate_key" in exc_info.value.what_to_do


def test_mask_api_key_shows_last_four_only():
    assert mask_api_key("abcdefgh1234") == "…1234"
    assert mask_api_key("ab") == "…••"


def _fake_jwt(payload: dict) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64(payload)}.signature-not-checked"


def test_jwt_expires_at_reads_exp_without_verifying_signature():
    exp_ts = int(dt.datetime(2027, 3, 12, tzinfo=dt.timezone.utc).timestamp())
    token = _fake_jwt({"exp": exp_ts, "sub": "wb"})
    parsed = jwt_expires_at(token)
    assert parsed == dt.datetime(2027, 3, 12, tzinfo=dt.timezone.utc)


def test_jwt_expires_at_returns_none_for_non_jwt():
    assert jwt_expires_at("not-a-jwt-token") is None
    assert jwt_expires_at("") is None


# --- API/схема не отдаёт ключ ------------------------------------------------


def test_client_out_schema_never_carries_raw_key(db, monkeypatch):
    monkeypatch.setattr(
        "fulfil.secrets.get_settings",
        lambda: type("S", (), {"client_secrets_key": Fernet.generate_key().decode()})(),
    )
    client = clients_service.create_client(db, name="ООО Ромашка", api_key="raw-secret-token", actor="tester")
    assert client.wb_api_key_enc != "raw-secret-token"

    status = clients_service.key_status(client)
    assert status["hasApiKey"] is True
    dumped = ClientOut.model_validate({**status, "id": client.id, "name": client.name}).model_dump()
    assert "wb_api_key_enc" not in dumped
    assert "api_key" not in dumped
    assert "raw-secret-token" not in str(dumped)


# --- Синхронизация мока по двум клиентам ------------------------------------


def _force_mock_mode(monkeypatch):
    """Тестовое окружение читает настоящий .env проекта (pydantic-settings,
    env_file=".env") — там WB_MODE может быть http. Эти тесты проверяют именно
    мок-диспатчинг get_wb_client(client), поэтому режим фиксируем явно."""
    import fulfil.integrations.wb as wb_module

    monkeypatch.setattr(wb_module, "get_settings", lambda: type("S", (), {"wb_mode": "mock"})())


def test_mock_sync_two_clients_gives_separate_client_ids_and_cursors(db, monkeypatch):
    _force_mock_mode(monkeypatch)
    c1 = make_client(db, name="Клиент 1")
    c2 = make_client(db, name="Клиент 2")

    wb1 = get_wb_client(c1)
    wb2 = get_wb_client(c2)
    assert wb1 is not wb2  # мок кэширует по client.id, но разные клиенты — разные экземпляры

    sync_products_from_wb(db, c1, wb1)
    sync_products_from_wb(db, c2, wb2)

    products_c1 = db.scalars(select(Product).where(Product.client_id == c1.id)).all()
    products_c2 = db.scalars(select(Product).where(Product.client_id == c2.id)).all()
    assert products_c1 and products_c2
    # Баркоды зависят от client_id — не совпадают между клиентами (реальная многоарендность мока).
    assert {p.barcode for p in products_c1}.isdisjoint({p.barcode for p in products_c2})

    assert db.get(IntegrationState, f"wb.product_cards:{c1.id}") is not None
    assert db.get(IntegrationState, f"wb.product_cards:{c2.id}") is not None


def test_mock_card_with_multiple_sizes_gives_multiple_products(db, monkeypatch):
    _force_mock_mode(monkeypatch)
    seller = make_client(db, name="С размерами")
    wb = get_wb_client(seller)
    result = sync_products_from_wb(db, seller, wb)

    products = db.scalars(select(Product).where(Product.client_id == seller.id)).all()
    assert result["imported"] == len(products)
    assert len(products) >= 2  # фикстура мока: одна карточка, два размера
    chrt_ids = {p.wb_chrt_id for p in products}
    assert len(chrt_ids) == len(products)  # разные размеры -> разные wb_chrt_id
    assert len({p.wb_imt_id for p in products}) == 1  # но одна и та же карточка (imtId)


# --- Баркод: разрешён между клиентами, запрещён внутри клиента --------------


def test_same_barcode_allowed_for_two_different_clients(db):
    c1 = make_client(db, name="Клиент А")
    c2 = make_client(db, name="Клиент Б")

    p1 = create_product(db, client_id=c1.id, barcode="4600000000001", name="Товар", actor="t")
    p2 = create_product(db, client_id=c2.id, barcode="4600000000001", name="Товар", actor="t")
    assert p1.id != p2.id
    assert p1.barcode == p2.barcode


def test_same_barcode_rejected_within_one_client(db, seller):
    create_product(db, client_id=seller.id, barcode="4600000000001", name="Товар 1", actor="t")
    with pytest.raises(AppError) as exc_info:
        create_product(db, client_id=seller.id, barcode="4600000000001", name="Товар 2", actor="t")
    assert exc_info.value.reason_code == "barcode_exists"


# --- Архивация клиента ------------------------------------------------------


def test_archive_client_with_stock_is_blocked(db, seller):
    product = create_product(db, client_id=seller.id, barcode="4600000000002", name="Товар", actor="t")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 5)

    with pytest.raises(AppError) as exc_info:
        clients_service.archive_client(db, seller, actor="tester")
    assert exc_info.value.reason_code == "client_has_stock"


def test_archive_client_without_stock_or_orders_succeeds(db, seller):
    archived = clients_service.archive_client(db, seller, actor="tester")
    assert archived.archived_at is not None


# --- transfer_to_fbs без склада ----------------------------------------------


def test_transfer_to_fbs_without_warehouse_gives_409(db, seller):
    product = create_product(db, client_id=seller.id, barcode="4600000000003", name="Товар", actor="t")
    [cell] = generate_cells(db, "A", racks=1, cells_per_rack=1)
    place_stock(db, product, cell, 10)
    assert seller.wb_warehouse_id is None

    with pytest.raises(AppError) as exc_info:
        transfer_to_fbs(db, product, 5, idempotency_key="k1", wb_client=WBMockClient())
    assert exc_info.value.reason_code == "no_wb_warehouse"


# --- Склад WB клиента (Этап 2 плана №3, п.2.2) -------------------------------
# «Оба варианта — выбрать существующий из списка или создать через API».


def test_create_wb_warehouse_creates_in_wb_and_binds_to_client(db, seller):
    wb = WBMockClient(client_id=seller.id)
    assert wb.list_warehouses() == []

    updated = clients_service.create_wb_warehouse(
        db, seller, wb, name="Фулфилмент Ромашка", office_id=1, actor="tester",
    )

    assert updated.wb_warehouse_id is not None
    assert updated.wb_warehouse_name == "Фулфилмент Ромашка"
    # Реально создан в WB (а не только записан в клиенте) — виден в списке складов.
    assert [w["id"] for w in wb.list_warehouses()] == [updated.wb_warehouse_id]


def test_set_wb_warehouse_binds_existing_without_calling_wb(db, seller):
    updated = clients_service.set_wb_warehouse(
        db, seller, warehouse_id="WH-EXISTING-1", warehouse_name="Склад продавца", actor="tester",
    )
    assert updated.wb_warehouse_id == "WH-EXISTING-1"
    assert updated.wb_warehouse_name == "Склад продавца"


def test_list_wb_offices_and_warehouses_delegate_to_wb_client(db, seller):
    wb = WBMockClient(client_id=seller.id)
    offices = clients_service.list_wb_offices(wb)
    assert offices and all("id" in o and "name" in o for o in offices)

    clients_service.create_wb_warehouse(db, seller, wb, name="Мой склад", office_id=offices[0]["id"], actor="t")
    warehouses = clients_service.list_wb_warehouses(wb)
    assert len(warehouses) == 1
    assert warehouses[0]["name"] == "Мой склад"


# --- P2-16: смена склада WB при активных заказах ------------------------------


def _make_active_order(db, client, wb_order_id="ACT-1"):
    from fulfil.models.fbs import Order, OrderStatus

    order = Order(client_id=client.id, wb_order_id=wb_order_id, status=OrderStatus.CONFIRMED)
    db.add(order)
    db.commit()
    return order


def test_update_client_blocks_warehouse_change_with_active_orders(db, seller):
    seller.wb_warehouse_id = "WH-OLD"
    db.commit()
    _make_active_order(db, seller)

    with pytest.raises(AppError) as exc_info:
        clients_service.update_client(db, seller, wb_warehouse_id="WH-NEW", actor="tester")
    assert exc_info.value.reason_code == "client_has_active_orders"
    db.refresh(seller)
    assert seller.wb_warehouse_id == "WH-OLD"  # не поменялось


def test_update_client_allows_warehouse_change_without_active_orders(db, seller):
    seller.wb_warehouse_id = "WH-OLD"
    db.commit()

    updated = clients_service.update_client(db, seller, wb_warehouse_id="WH-NEW", actor="tester")
    assert updated.wb_warehouse_id == "WH-NEW"


def test_set_wb_warehouse_blocks_change_with_active_orders(db, seller):
    seller.wb_warehouse_id = "WH-OLD"
    db.commit()
    _make_active_order(db, seller)

    with pytest.raises(AppError) as exc_info:
        clients_service.set_wb_warehouse(
            db, seller, warehouse_id="WH-NEW", warehouse_name="Новый склад", actor="tester",
        )
    assert exc_info.value.reason_code == "client_has_active_orders"


def test_create_wb_warehouse_blocks_change_with_active_orders(db, seller):
    seller.wb_warehouse_id = "WH-OLD"
    db.commit()
    _make_active_order(db, seller)
    wb = WBMockClient(client_id=seller.id)

    with pytest.raises(AppError) as exc_info:
        clients_service.create_wb_warehouse(db, seller, wb, name="Новый склад", office_id=1, actor="tester")
    assert exc_info.value.reason_code == "client_has_active_orders"
