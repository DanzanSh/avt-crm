"""users and client soft delete

Revision ID: c7d2a9e5f013
Revises: b3c8e41f7d20
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7d2a9e5f013'
down_revision: Union[str, None] = 'b3c8e41f7d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE = sa.text("archived_at IS NULL")


def upgrade() -> None:
    # 1. Учётные записи (problems.txt, п.5; authorization-model.md, этап 1) ---
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('login', sa.String(length=64), nullable=False),
        sa.Column('full_name', sa.String(length=128), nullable=False),
        sa.Column('role', sa.Enum('HOST', 'ADMIN', 'EMPLOYEE', name='userrole'), nullable=False),
        sa.Column('password_hash', sa.String(length=256), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_login'), 'users', ['login'], unique=False)
    op.create_index(op.f('ix_users_archived_at'), 'users', ['archived_at'], unique=False)
    op.create_index('uq_user_login_live', 'users', ['login'], unique=True, postgresql_where=_LIVE, sqlite_where=_LIVE)

    # 2. Хост из прежних ADMIN_LOGIN/ADMIN_PASSWORD — текущий вход продолжает работать.
    from fulfil.config import get_settings
    from fulfil.passwords import hash_password

    settings = get_settings()
    op.get_bind().execute(
        sa.text(
            "INSERT INTO users (login, full_name, role, password_hash, is_active, created_at, created_by) "
            "VALUES (:login, 'Владелец', 'HOST', :hash, true, now(), 'migration')"
        ),
        {"login": settings.admin_login, "hash": hash_password(settings.admin_password)},
    )

    # 3. Мягкое удаление клиента хостом ------------------------------------------
    op.add_column('clients', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('clients', sa.Column('deleted_by', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_clients_deleted_at'), 'clients', ['deleted_at'], unique=False)


def downgrade() -> None:
    # Откат возвращает вход по ADMIN_LOGIN/ADMIN_PASSWORD из .env (код этапа до миграции).
    op.drop_index(op.f('ix_clients_deleted_at'), table_name='clients')
    op.drop_column('clients', 'deleted_by')
    op.drop_column('clients', 'deleted_at')
    op.drop_index('uq_user_login_live', table_name='users')
    op.drop_index(op.f('ix_users_archived_at'), table_name='users')
    op.drop_index(op.f('ix_users_login'), table_name='users')
    op.drop_table('users')
    sa.Enum(name='userrole').drop(op.get_bind(), checkfirst=True)
