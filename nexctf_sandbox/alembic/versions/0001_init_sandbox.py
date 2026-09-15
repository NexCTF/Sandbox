"""Create solutions_script and solutions_runner tables.

The check constraints bound the cost of one verify(): the schemas enforce the
same ranges, but fixtures and direct model writes do not go through Pydantic.

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

_TIMEOUT = "timeout BETWEEN 1 AND 30"
_TEST_CASES = "jsonb_array_length(test_cases) <= 20"
_CHECKER_CODE = "length(checker_code) <= 65536"
# 20 cases x (input + expected_output) x 65536: a ceiling on the blob, since a
# CHECK cannot walk the array to bound each field the way the schema does.
_TEST_CASES_SIZE = "length(test_cases::text) <= 2621440"


def upgrade() -> None:
    op.create_table(
        "solutions_script",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("checker_code", sa.Text(), nullable=False),
        sa.Column("timeout", sa.Integer(), nullable=False, server_default="5"),
        sa.CheckConstraint(_TIMEOUT, name="ck_solutions_script_timeout"),
        sa.CheckConstraint(_CHECKER_CODE, name="ck_solutions_script_checker_code"),
        sa.ForeignKeyConstraint(["id"], ["solutions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "solutions_runner",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("test_cases", JSONB(), nullable=False, server_default="[]"),
        sa.Column("timeout", sa.Integer(), nullable=False, server_default="5"),
        sa.CheckConstraint(_TIMEOUT, name="ck_solutions_runner_timeout"),
        sa.CheckConstraint(_TEST_CASES, name="ck_solutions_runner_test_cases"),
        sa.CheckConstraint(
            _TEST_CASES_SIZE, name="ck_solutions_runner_test_cases_size"
        ),
        sa.ForeignKeyConstraint(["id"], ["solutions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("solutions_runner")
    op.drop_table("solutions_script")
