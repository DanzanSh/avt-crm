"""storage shelves + receipt_lines.actor + integration_state

Revision ID: 7f3a1c920b58
Revises: 010451787642
Create Date: 2026-09-08 00:00:00.000000

Добавляет 4-й уровень адресного хранения — полки (`shelves`) между стеллажом и
местом. Адрес места переезжает с `A-стеллаж-место` на `A-стеллаж-полка-место`;
существующие данные конвертируются `A-r-c -> A-r-1-c` (всё едет на «полку 1»).

Заодно (одна ревизия по плану доработок):
  * `receipt_lines.actor` — кто принял (для постоянной истории приёмок);
  * таблица `integration_state` — курсор инкрементальной выгрузки карточек WB.

ВНИМАНИЕ (downgrade): обратная конвертация адреса `A-r-1-c -> A-r-c` выполняется
ТОЛЬКО для мест с `shelf_no = 1`. Если в БД появились места на полках > 1, откат
данных лоссовый (эти адреса не восстанавливаются в 3-сегментный вид) — так же,
как необратима латинизация зон в ревизии 010451787642.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7f3a1c920b58"
down_revision: Union[str, None] = "010451787642"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE = sa.text("deleted_at IS NULL")
_ADDR_3 = r"^[^-]+-[0-9]+-[0-9]+$"
_ADDR_4 = r"^[^-]+-[0-9]+-[0-9]+-[0-9]+$"


def upgrade() -> None:
    # 1. shelves ------------------------------------------------------------
    op.create_table(
        "shelves",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rack_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("places_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rack_id"], ["racks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_shelves_deleted_at"), "shelves", ["deleted_at"], unique=False)
    op.create_index(
        "uq_shelf_rack_number_live", "shelves", ["rack_id", "number"], unique=True,
        postgresql_where=_LIVE, sqlite_where=_LIVE,
    )

    # 2. cells.shelf_id / shelf_no (сначала nullable / с дефолтом) ----------
    op.add_column(
        "cells",
        sa.Column(
            "shelf_id", sa.Integer(),
            sa.ForeignKey("shelves.id", ondelete="CASCADE"), nullable=True,
        ),
    )
    op.add_column(
        "cells", sa.Column("shelf_no", sa.Integer(), nullable=False, server_default="1")
    )

    # 3. бэкфилл (данные сохраняем) ---------------------------------------
    # По одной полке №1 на каждый стеллаж (в т.ч. удалённый — у его мест тоже
    # должен быть непустой shelf_id). deleted_at полки наследует стеллаж.
    op.execute(
        "INSERT INTO shelves (rack_id, number, places_count, deleted_at) "
        "SELECT id, 1, COALESCE(cells_count, 0), deleted_at FROM racks"
    )
    op.execute(
        "UPDATE cells AS c SET shelf_id = s.id, shelf_no = 1 "
        "FROM shelves AS s WHERE s.rack_id = c.rack_id AND s.number = 1"
    )
    op.execute(
        "UPDATE cells SET address = "
        "split_part(address,'-',1)||'-'||split_part(address,'-',2)||'-1-'||split_part(address,'-',3) "
        f"WHERE address ~ '{_ADDR_3}'"
    )

    # 4. закрепляем инварианты ------------------------------------------
    op.alter_column("cells", "shelf_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("cells", "shelf_no", existing_type=sa.Integer(), server_default=None)

    # 5. индексы уровня места ------------------------------------------
    op.create_index(op.f("ix_cells_shelf_id"), "cells", ["shelf_id"], unique=False)
    op.create_index(op.f("ix_cells_shelf_no"), "cells", ["shelf_no"], unique=False)

    # 6. receipt_lines.actor ------------------------------------------
    op.add_column(
        "receipt_lines",
        sa.Column("actor", sa.String(length=64), nullable=False, server_default="system"),
    )

    # 7. integration_state ------------------------------------------
    op.create_table(
        "integration_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("integration_state")
    op.drop_column("receipt_lines", "actor")

    op.drop_index(op.f("ix_cells_shelf_no"), table_name="cells")
    op.drop_index(op.f("ix_cells_shelf_id"), table_name="cells")

    # Обратная конвертация адреса — только для полки 1 (см. docstring).
    op.execute(
        "UPDATE cells SET address = "
        "split_part(address,'-',1)||'-'||split_part(address,'-',2)||'-'||split_part(address,'-',4) "
        f"WHERE shelf_no = 1 AND address ~ '{_ADDR_4}'"
    )

    op.drop_column("cells", "shelf_no")
    op.drop_column("cells", "shelf_id")

    op.drop_index("uq_shelf_rack_number_live", table_name="shelves")
    op.drop_index(op.f("ix_shelves_deleted_at"), table_name="shelves")
    op.drop_table("shelves")
