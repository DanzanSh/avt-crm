"""fbs: заказы ФБС — фильтр по складу, статусы WB, unknown_sku (Этап 3 плана №3, пп.4.1, 4.2)

Разведка (problems.txt, п.3-6) нашла три дефекта:
  1. У заказов из WB есть warehouseId, но синхронизация его не читала — заказы
     с чужих складов продавца попадали в базу вместе с нашими.
  2. Опрашивался только GET /orders/new — отмена покупателем, «в доставке»,
     «принята» до нас не доходили, потому что статус уже загруженных заказов
     никогда не перечитывался.
  3. _first_sku() терял позицию, если товар не находился по баркоду — заказ
     тихо «собирался» пустым вместо того, чтобы показать проблему оператору.

Что делает ревизия:
  * orders: wb_warehouse_id (фильтр «только наш склад»), wb_nm_id/wb_chrt_id/article
    (для диагностики и повторного поиска карточки), wb_status/supplier_status
    (кэш последнего опроса POST /api/v3/orders/status), problem ('unknown_sku').
  * order_items.product_id становится nullable — позиция с нераспознанным баркодом
    сохраняется (Order.problem='unknown_sku'), а не пропадает молча.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0eeaedbcdb49"
down_revision: Union[str, None] = "43e89b2c379d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("wb_warehouse_id", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_orders_wb_warehouse_id"), "orders", ["wb_warehouse_id"], unique=False)
    op.add_column("orders", sa.Column("wb_nm_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_orders_wb_nm_id"), "orders", ["wb_nm_id"], unique=False)
    op.add_column("orders", sa.Column("wb_chrt_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("article", sa.String(length=128), nullable=True))
    op.add_column("orders", sa.Column("wb_status", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("supplier_status", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("problem", sa.String(length=32), nullable=True))
    op.create_index(op.f("ix_orders_problem"), "orders", ["problem"], unique=False)

    op.alter_column("order_items", "product_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Откат нельзя провести, если к этому моменту уже есть позиции с product_id
    # IS NULL (unknown_sku) — ALTER ... NOT NULL упадёт, и это верный сигнал:
    # разберитесь с этими позициями (привяжите товар или удалите заказ) вручную
    # перед откатом.
    op.alter_column("order_items", "product_id", existing_type=sa.Integer(), nullable=False)

    op.drop_index(op.f("ix_orders_problem"), table_name="orders")
    op.drop_column("orders", "problem")
    op.drop_column("orders", "supplier_status")
    op.drop_column("orders", "wb_status")
    op.drop_column("orders", "article")
    op.drop_column("orders", "wb_chrt_id")
    op.drop_index(op.f("ix_orders_wb_nm_id"), table_name="orders")
    op.drop_column("orders", "wb_nm_id")
    op.drop_index(op.f("ix_orders_wb_warehouse_id"), table_name="orders")
    op.drop_column("orders", "wb_warehouse_id")
