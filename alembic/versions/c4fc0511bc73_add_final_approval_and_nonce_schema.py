"""add_final_approval_and_nonce_schema

Revision ID: c4fc0511bc73
Revises:
Create Date: 2026-09-16 03:30:45.243789
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision: str = 'c4fc0511bc73'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    return table in inspector.get_table_names()


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if table not in inspector.get_table_names():
        return False
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    if not _table_exists('submission_nonces'):
        op.create_table('submission_nonces',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('application_id', sa.Integer(), nullable=False),
            sa.Column('nonce', sa.String(length=64), nullable=False),
            sa.Column('manifest_hash', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('consumed', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['application_id'], ['applications.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('application_id'),
            sa.UniqueConstraint('nonce'),
        )
        op.create_index(op.f('ix_submission_nonces_nonce'), 'submission_nonces', ['nonce'], unique=True)

    if _table_exists('applications'):
        if not _column_exists('applications', 'manifest_hash'):
            op.add_column('applications', sa.Column('manifest_hash', sa.String(length=64), nullable=True))
        if not _column_exists('applications', 'confirmation_reference'):
            op.add_column('applications', sa.Column('confirmation_reference', sa.String(length=500), nullable=True))
        if not _column_exists('applications', 'screenshot_path'):
            op.add_column('applications', sa.Column('screenshot_path', sa.String(length=500), nullable=True))
        if not _column_exists('applications', 'attempt_status'):
            op.add_column('applications', sa.Column('attempt_status', sa.String(length=50), nullable=True))

    if _table_exists('approvals'):
        if not _column_exists('approvals', 'approval_type'):
            op.add_column('approvals', sa.Column('approval_type', sa.String(length=10), nullable=True))
        if not _column_exists('approvals', 'approval_ref_code'):
            op.add_column('approvals', sa.Column('approval_ref_code', sa.String(length=20), nullable=True))
            op.create_index(op.f('ix_approvals_approval_ref_code'), 'approvals', ['approval_ref_code'], unique=False)
        if not _column_exists('approvals', 'whatsapp_delivery_status'):
            op.add_column('approvals', sa.Column('whatsapp_delivery_status', sa.String(length=100), nullable=True))

    if _table_exists('jobs'):
        if not _column_exists('jobs', 'verification_evidence'):
            op.add_column('jobs', sa.Column('verification_evidence', sa.JSON(), nullable=True))
        if not _column_exists('jobs', 'verification_blockers'):
            op.add_column('jobs', sa.Column('verification_blockers', sa.Text(), nullable=True))


def downgrade() -> None:
    if _column_exists('jobs', 'verification_blockers'):
        op.drop_column('jobs', 'verification_blockers')
    if _column_exists('jobs', 'verification_evidence'):
        op.drop_column('jobs', 'verification_evidence')
    if _column_exists('approvals', 'approval_ref_code'):
        op.drop_index(op.f('ix_approvals_approval_ref_code'), table_name='approvals')
    if _column_exists('approvals', 'whatsapp_delivery_status'):
        op.drop_column('approvals', 'whatsapp_delivery_status')
    if _column_exists('approvals', 'approval_ref_code'):
        op.drop_column('approvals', 'approval_ref_code')
    if _column_exists('approvals', 'approval_type'):
        op.drop_column('approvals', 'approval_type')
    if _column_exists('applications', 'attempt_status'):
        op.drop_column('applications', 'attempt_status')
    if _column_exists('applications', 'screenshot_path'):
        op.drop_column('applications', 'screenshot_path')
    if _column_exists('applications', 'confirmation_reference'):
        op.drop_column('applications', 'confirmation_reference')
    if _column_exists('applications', 'manifest_hash'):
        op.drop_column('applications', 'manifest_hash')
    if _table_exists('submission_nonces'):
        op.drop_index(op.f('ix_submission_nonces_nonce'), table_name='submission_nonces')
        op.drop_table('submission_nonces')
