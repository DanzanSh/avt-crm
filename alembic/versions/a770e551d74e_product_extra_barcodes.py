"""product extra barcodes

Revision ID: a770e551d74e
Revises: 709c32cd1f0d
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a770e551d74e'
down_revision: Union[str, None] = '709c32cd1f0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Доп. баркоды того же размера карточки WB (skus[1:]) — P2-11: заказ может
    # прийти с любым из нескольких skus одного chrtId, а Product.barcode хранит
    # только один. См. models/product.py Product.extra_barcodes.
    op.add_column('products', sa.Column('extra_barcodes', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    op.drop_column('products', 'extra_barcodes')
