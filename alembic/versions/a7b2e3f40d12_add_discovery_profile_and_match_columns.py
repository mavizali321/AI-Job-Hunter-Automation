"""add_discovery_profile_and_match_columns

Revision ID: a7b2e3f40d12
Revises: c4fc0511bc73
Create Date: 2026-09-16 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision: str = 'a7b2e3f40d12'
down_revision: Union[str, None] = 'c4fc0511bc73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    # --- Discovery columns on jobs ---
    if not _column_exists("jobs", "discovery_url"):
        op.add_column("jobs", sa.Column("discovery_url", sa.String(2048), nullable=True))
    if not _column_exists("jobs", "discovery_source"):
        op.add_column("jobs", sa.Column("discovery_source", sa.String(100), nullable=True))
    if not _column_exists("jobs", "official_url"):
        op.add_column("jobs", sa.Column("official_url", sa.String(2048), nullable=True))
    if not _column_exists("jobs", "official_url_method"):
        op.add_column("jobs", sa.Column("official_url_method", sa.String(50), nullable=True))

    # --- Match columns on jobs ---
    if not _column_exists("jobs", "rejection_reason"):
        op.add_column("jobs", sa.Column("rejection_reason", sa.Text(), nullable=True))
    if not _column_exists("jobs", "matched_skills"):
        op.add_column("jobs", sa.Column("matched_skills", sa.JSON(), nullable=True))
    if not _column_exists("jobs", "missing_skills"):
        op.add_column("jobs", sa.Column("missing_skills", sa.JSON(), nullable=True))
    if not _column_exists("jobs", "seniority_fit"):
        op.add_column("jobs", sa.Column("seniority_fit", sa.String(100), nullable=True))
    if not _column_exists("jobs", "location_fit_detail"):
        op.add_column("jobs", sa.Column("location_fit_detail", sa.String(200), nullable=True))
    if not _column_exists("jobs", "match_score"):
        op.add_column("jobs", sa.Column("match_score", sa.Float(), nullable=True))
    if not _column_exists("jobs", "match_details"):
        op.add_column("jobs", sa.Column("match_details", sa.JSON(), nullable=True))

    # --- CandidateProfile table ---
    if not _table_exists("candidate_profiles"):
        op.create_table(
            "candidate_profiles",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("resume_checksum", sa.String(64), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("email", sa.String(200), nullable=True),
            sa.Column("phone", sa.String(50), nullable=True),
            sa.Column("linkedin", sa.String(500), nullable=True),
            sa.Column("seniority", sa.String(50), nullable=False, server_default="junior"),
            sa.Column("total_experience_years", sa.Float(), nullable=True),
            sa.Column("skills", sa.JSON(), nullable=False),
            sa.Column("experience", sa.JSON(), nullable=False),
            sa.Column("education", sa.JSON(), nullable=False),
            sa.Column("projects", sa.JSON(), nullable=False),
            sa.Column("target_roles", sa.JSON(), nullable=False),
            sa.Column("location_preferences", sa.JSON(), nullable=False),
            sa.Column("do_not_claim", sa.JSON(), nullable=False),
            sa.Column("raw_resume_text", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("resume_checksum"),
        )


def downgrade() -> None:
    if _table_exists("candidate_profiles"):
        op.drop_table("candidate_profiles")

    for col in [
        "match_details", "match_score", "location_fit_detail", "seniority_fit",
        "missing_skills", "matched_skills", "rejection_reason",
        "official_url_method", "official_url", "discovery_source", "discovery_url",
    ]:
        if _column_exists("jobs", col):
            op.drop_column("jobs", col)
