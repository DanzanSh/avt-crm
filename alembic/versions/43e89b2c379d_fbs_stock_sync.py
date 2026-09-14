"""fbs: правильная передача остатка на WB (Этап 2 плана №3, п.2.3)

WB `PUT /api/v3/stocks/{warehouseId}` ЗАДАЁТ остаток, а не увеличивает его —
`transfer_to_fbs` раньше отправлял `qty` как есть, поэтому вторая передача «+3»
поверх уже переданных «5» затирала остаток на WB тройкой вместо восьмёрки.

Что делает ревизия:
  1. `products.wb_fbs_amount` — кэш текущего остатка на складе WB FBS. Источник
     правды для «доступно к передаче» вместо ломкого Σ уже отправленных транзакций
     (та сумма не падает при сборке заказа, поэтому давала ложный fbsOversold).
  2. `fbs_transfers.wb_amount_before/after` — аудит: что было на WB до передачи
     и что стало после (текущее + qty), для разбора расхождений.

Оба поля заполняются нулём/NULL для уже существующих строк — это осознанно:
исторические передачи не знают, каким был остаток на WB на тот момент.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "43e89b2c379d"
down_revision: Union[str, None] = "5df130e474c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("wb_fbs_amount", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("fbs_transfers", sa.Column("wb_amount_before", sa.Integer(), nullable=True))
    op.add_column("fbs_transfers", sa.Column("wb_amount_after", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("fbs_transfers", "wb_amount_after")
    op.drop_column("fbs_transfers", "wb_amount_before")
    op.drop_column("products", "wb_fbs_amount")
