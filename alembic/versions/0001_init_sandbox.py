"""Create solutions_script and solutions_runner tables.

Revision ID: 0001
Revises:
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "solutions_script",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("checker_code", sa.Text(), nullable=False),
        sa.Column("timeout", sa.Integer(), nullable=False, server_default="5"),
        sa.ForeignKeyConstraint(["id"], ["solutions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "solutions_runner",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("test_cases", JSONB(), nullable=False, server_default="[]"),
        sa.Column("timeout", sa.Integer(), nullable=False, server_default="5"),
        sa.ForeignKeyConstraint(["id"], ["solutions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("solutions_runner")
    op.drop_table("solutions_script")
