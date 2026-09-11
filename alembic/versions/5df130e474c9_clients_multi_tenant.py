"""clients: несколько кабинетов WB (Этап 1 плана доработок №3, п.2.2)

До этой ревизии приложение было однокабинетным: токен WB_API_TOKEN и склад
WB_WAREHOUSE_ID жили в .env, баркод товара был уникален глобально, курсор синка
карточек был один на всё приложение. Теперь у каждого клиента фулфилмента —
свой кабинет WB (ключ API, склад), и вся предметная область (товары, заказы,
поставки, приёмки) привязана к client_id.

Что делает ревизия:
  1. Создаёт `clients` (ключ API — зашифрован Fernet, см. fulfil.secrets).
  2. Если в БД уже есть данные (products/orders/supplies/receipts) ИЛИ в окружении
     задан WB_API_TOKEN — заводит клиента DEFAULT_CLIENT_NAME ("Основной кабинет"
     по умолчанию) с токеном и складом из текущего .env. Если CLIENT_SECRETS_KEY
     не задан, клиент создаётся БЕЗ ключа (предупреждение в лог) — ключ вводится
     в UI после миграции.
  3. Добавляет `client_id` (nullable -> backfill -> NOT NULL, FK) в products,
     orders, supplies, receipts; nullable client_id — в wb_api_log (диагностика).
  4. Пересоздаёт уникальный индекс баркода как (client_id, barcode) живых
     (было — просто barcode живых: uq_product_barcode_live); снимает уникальность
     с wb_nm_id (карточка теперь может дать несколько товаров — по одному на
     размер); добавляет products.wb_chrt_id.
  5. Переименовывает курсор синка `wb.product_cards` -> `wb.product_cards:<id>`
     клиента, заведённого в п.2 (если он был создан).

ВНИМАНИЕ (downgrade, необратимые части — как и в соседних ревизиях):
  * client_id колонки просто дропаются — привязка строк к клиенту теряется;
  * uq_product_barcode_live возвращается к (barcode) без client_id: если к моменту
    отката два разных клиента получили одинаковый баркод, downgrade упадёт на
    создании индекса — так и должно быть, это сигнал, что откатывать назад
    после реального использования многокабинетности небезопасно;
  * переименование курсора обратно в 'wb.product_cards' лоссово, если к моменту
    отката появилось больше одного клиента с курсором — сохраняется только один
    (остальные удаляются), см. код ниже.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5df130e474c9"
down_revision: Union[str, None] = "7f3a1c920b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE = sa.text("archived_at IS NULL")


def upgrade() -> None:
    bind = op.get_bind()

    # 1. clients ------------------------------------------------------------
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("wb_api_key_enc", sa.Text(), nullable=True),
        sa.Column("wb_warehouse_id", sa.String(length=64), nullable=True),
        sa.Column("wb_warehouse_name", sa.String(length=256), nullable=True),
        sa.Column("wb_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_clients_archived_at"), "clients", ["archived_at"], unique=False)
    op.create_index(
        "uq_client_name_live", "clients", ["name"], unique=True,
        postgresql_where=_LIVE, sqlite_where=_LIVE,
    )

    # 2. клиент "Основной кабинет" из текущего .env -------------------------
    # Читаем настройки через fulfil.config.get_settings() — единственный источник
    # правды для окружения в этом проекте (см. план доработок, п.1.2).
    from fulfil.config import get_settings
    from fulfil.secrets import encrypt_api_key, jwt_expires_at

    settings = get_settings()
    has_data = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM products UNION ALL "
            "  SELECT 1 FROM orders UNION ALL "
            "  SELECT 1 FROM supplies UNION ALL "
            "  SELECT 1 FROM receipts"
            ")"
        )
    ).scalar()

    default_client_id = None
    if has_data or settings.wb_api_token:
        name = settings.default_client_name or "Основной кабинет"
        enc_key = None
        expires_at = None
        if settings.wb_api_token:
            if settings.client_secrets_key:
                enc_key = encrypt_api_key(settings.wb_api_token)
                expires_at = jwt_expires_at(settings.wb_api_token)
            else:
                print(
                    "[migration 5df130e474c9] ВНИМАНИЕ: WB_API_TOKEN задан, но "
                    "CLIENT_SECRETS_KEY не задан — клиент "
                    f"«{name}» создан БЕЗ ключа API. Введите ключ в разделе «Клиенты» "
                    "после миграции."
                )
        default_client_id = bind.execute(
            sa.text(
                "INSERT INTO clients (name, wb_api_key_enc, wb_warehouse_id, wb_token_expires_at, created_at) "
                "VALUES (:name, :key, :wh, :exp, now()) RETURNING id"
            ),
            {
                "name": name,
                "key": enc_key,
                "wh": settings.wb_warehouse_id or None,
                "exp": expires_at,
            },
        ).scalar()

    # 3. client_id: nullable -> backfill -> NOT NULL + FK + индекс ----------
    for table in ("products", "orders", "supplies", "receipts"):
        op.add_column(table, sa.Column("client_id", sa.Integer(), nullable=True))
        if default_client_id is not None:
            op.execute(
                sa.text(f"UPDATE {table} SET client_id = :cid WHERE client_id IS NULL")
                .bindparams(cid=default_client_id)
            )
        op.alter_column(table, "client_id", existing_type=sa.Integer(), nullable=False)
        op.create_foreign_key(f"fk_{table}_client_id", table, "clients", ["client_id"], ["id"])
        op.create_index(op.f(f"ix_{table}_client_id"), table, ["client_id"], unique=False)

    # wb_api_log.client_id — nullable, для диагностики (не у всех логов есть клиент).
    op.add_column("wb_api_log", sa.Column("client_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_wb_api_log_client_id", "wb_api_log", "clients", ["client_id"], ["id"])
    op.create_index(op.f("ix_wb_api_log_client_id"), "wb_api_log", ["client_id"], unique=False)

    # 4. products: баркод уникален В ПРЕДЕЛАХ клиента; wb_nm_id не уникален; wb_chrt_id --
    op.drop_index("uq_product_barcode_live", table_name="products", postgresql_where=_LIVE, sqlite_where=_LIVE)
    op.create_index(
        "uq_product_barcode_live", "products", ["client_id", "barcode"], unique=True,
        postgresql_where=_LIVE, sqlite_where=_LIVE,
    )
    op.drop_index(op.f("ix_products_wb_nm_id"), table_name="products")
    op.create_index(op.f("ix_products_wb_nm_id"), "products", ["wb_nm_id"], unique=False)
    op.add_column("products", sa.Column("wb_chrt_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_products_wb_chrt_id"), "products", ["wb_chrt_id"], unique=False)

    # 5. курсор синка карточек — на клиента --------------------------------
    if default_client_id is not None:
        op.execute(
            sa.text("UPDATE integration_state SET key = :new_key WHERE key = 'wb.product_cards'")
            .bindparams(new_key=f"wb.product_cards:{default_client_id}")
        )


def downgrade() -> None:
    # 5. курсор синка — лоссово при >1 клиента (см. docstring): оставляем один.
    op.execute(
        "DELETE FROM integration_state WHERE key LIKE 'wb.product_cards:%' "
        "AND key <> (SELECT key FROM integration_state WHERE key LIKE 'wb.product_cards:%' ORDER BY key LIMIT 1)"
    )
    op.execute("UPDATE integration_state SET key = 'wb.product_cards' WHERE key LIKE 'wb.product_cards:%'")

    # 4. products: откат к глобальной уникальности баркода/wb_nm_id --------
    op.drop_index(op.f("ix_products_wb_chrt_id"), table_name="products")
    op.drop_column("products", "wb_chrt_id")
    op.drop_index(op.f("ix_products_wb_nm_id"), table_name="products")
    op.create_index(op.f("ix_products_wb_nm_id"), "products", ["wb_nm_id"], unique=True)
    op.drop_index("uq_product_barcode_live", table_name="products", postgresql_where=_LIVE, sqlite_where=_LIVE)
    op.create_index(
        "uq_product_barcode_live", "products", ["barcode"], unique=True,
        postgresql_where=_LIVE, sqlite_where=_LIVE,
    )

    # 3. client_id — дропаем (привязка к клиенту теряется, см. docstring) ---
    op.drop_index(op.f("ix_wb_api_log_client_id"), table_name="wb_api_log")
    op.drop_constraint("fk_wb_api_log_client_id", "wb_api_log", type_="foreignkey")
    op.drop_column("wb_api_log", "client_id")

    for table in ("receipts", "supplies", "orders", "products"):
        op.drop_index(op.f(f"ix_{table}_client_id"), table_name=table)
        op.drop_constraint(f"fk_{table}_client_id", table, type_="foreignkey")
        op.drop_column(table, "client_id")

    # 1-2. clients -----------------------------------------------------------
    op.drop_index("uq_client_name_live", table_name="clients", postgresql_where=_LIVE, sqlite_where=_LIVE)
    op.drop_index(op.f("ix_clients_archived_at"), table_name="clients")
    op.drop_table("clients")
