"""fbs: поставки — статус доставки/приёмки, имя поставки (Этап 4 плана №3, п.6 problems.txt)

Разведка (problems.txt, п.6) нашла: статус поставки был бинарным (open/closed),
хотя у WB их три разных момента — «на сборке» (done=false), «в доставке»
(done=true, scanDt пуст) и «принята» складом (scanDt заполнен). Без последних
двух заказчик не видел, добралась ли поставка до склада WB и принял ли её
приёмщик — обновление раньше приходило только по ручному клику "Закрыть", а
факт приёмки не отслеживался вообще.

Что делает ревизия:
  * supplystatus: новые значения in_delivery, accepted (ALTER TYPE в autocommit-
    блоке — Postgres не даёт добавить значение нативного enum внутри той же
    транзакции, где оно могло бы использоваться).
  * supplies: name (то, что раньше уходило только в WB, теперь персистится),
    wb_done/created_at_wb/closed_at_wb/scan_dt — сырые поля WB, из которых
    services.supplies выводит статус.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0a0ecffb8e11'
down_revision: Union[str, None] = '0eeaedbcdb49'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ВНИМАНИЕ: PostgreSQL enum-метки здесь — это ИМЕНА питоновских членов
    # (SupplyStatus.IN_DELIVERY), а не их .value ('in_delivery') — ровно так
    # же, как уже лежащие в этом типе OPEN/CLOSED/PARTIAL/FAILED/STALE (см.
    # initial_schema: sa.Enum('OPEN', 'CLOSED', ...)). SQLAlchemy сериализует
    # native Enum по умолчанию через .name, а не .value, если тип не объявлен
    # с values_callable — эта миграция ниже НЕ должна ломать конвенцию.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE supplystatus ADD VALUE IF NOT EXISTS 'IN_DELIVERY'")
        op.execute("ALTER TYPE supplystatus ADD VALUE IF NOT EXISTS 'ACCEPTED'")

    op.add_column("supplies", sa.Column("name", sa.String(length=255), nullable=True))
    op.add_column(
        "supplies", sa.Column("wb_done", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    op.add_column("supplies", sa.Column("created_at_wb", sa.DateTime(timezone=True), nullable=True))
    op.add_column("supplies", sa.Column("closed_at_wb", sa.DateTime(timezone=True), nullable=True))
    op.add_column("supplies", sa.Column("scan_dt", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Откат enum-значений Postgres не поддерживает (ALTER TYPE ... DROP VALUE не
    # существует) — если к этому моменту уже есть поставки со status IN
    # ('in_delivery', 'accepted'), их нужно вручную перевести в closed/failed
    # перед откатом, иначе следующие ALTER на supplies.status упадут.
    op.drop_column("supplies", "scan_dt")
    op.drop_column("supplies", "closed_at_wb")
    op.drop_column("supplies", "created_at_wb")
    op.drop_column("supplies", "wb_done")
    op.drop_column("supplies", "name")
