"""add_confirmed_and_profile_only_skills

Revision ID: b3d5f7a90123
Revises: a7b2e3f40d12
Create Date: 2026-09-17 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision: str = 'b3d5f7a90123'
down_revision: Union[str, None] = 'a7b2e3f40d12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if table not in inspector.get_table_names():
        return False
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("candidate_profiles"):
        if not _column_exists("candidate_profiles", "confirmed_skills"):
            op.add_column("candidate_profiles", sa.Column("confirmed_skills", sa.JSON(), nullable=True))
        if not _column_exists("candidate_profiles", "profile_only_skills"):
            op.add_column("candidate_profiles", sa.Column("profile_only_skills", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _table_exists("candidate_profiles"):
        if _column_exists("candidate_profiles", "profile_only_skills"):
            op.drop_column("candidate_profiles", "profile_only_skills")
        if _column_exists("candidate_profiles", "confirmed_skills"):
            op.drop_column("candidate_profiles", "confirmed_skills")
