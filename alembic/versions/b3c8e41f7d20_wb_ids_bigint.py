"""wb ids bigint

Revision ID: b3c8e41f7d20
Revises: a770e551d74e
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3c8e41f7d20'
down_revision: Union[str, None] = 'a770e551d74e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# chrtID от WB перерос INTEGER (2222347681 > 2^31-1): синхронизация карточек
# падала 500 с NumericValueOutOfRange уже на SELECT по wb_chrt_id.
_COLUMNS = (
    ('products', 'wb_nm_id'),
    ('products', 'wb_imt_id'),
    ('products', 'wb_chrt_id'),
    ('orders', 'wb_nm_id'),
    ('orders', 'wb_chrt_id'),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.alter_column(table, column, type_=sa.BigInteger(), existing_type=sa.Integer(), existing_nullable=True)


def downgrade() -> None:
    # Упадёт, если в базе уже есть значения больше INTEGER — это ожидаемо.
    for table, column in reversed(_COLUMNS):
        op.alter_column(table, column, type_=sa.Integer(), existing_type=sa.BigInteger(), existing_nullable=True)
