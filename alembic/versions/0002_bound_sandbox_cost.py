"""Bound the cost of one verify(): timeout range and test-case count.

The Pydantic schemas enforce these too, but fixtures, imports and direct model
writes do not go through them, and an unbounded timeout stalls scoring
platform-wide during submission re-verification.

Existing timeouts are clamped into range. Over-long test-case lists are left
alone on purpose, so this migration fails loudly rather than silently deleting
an admin's test cases.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

_TIMEOUT = "timeout BETWEEN 1 AND 30"
_TEST_CASES = "jsonb_array_length(test_cases) <= 20"


def upgrade() -> None:
    op.execute("UPDATE solutions_script SET timeout = LEAST(GREATEST(timeout, 1), 30)")
    op.execute("UPDATE solutions_runner SET timeout = LEAST(GREATEST(timeout, 1), 30)")
    op.create_check_constraint(
        "ck_solutions_script_timeout", "solutions_script", _TIMEOUT
    )
    op.create_check_constraint(
        "ck_solutions_runner_timeout", "solutions_runner", _TIMEOUT
    )
    op.create_check_constraint(
        "ck_solutions_runner_test_cases", "solutions_runner", _TEST_CASES
    )


def downgrade() -> None:
    op.drop_constraint("ck_solutions_runner_test_cases", "solutions_runner")
    op.drop_constraint("ck_solutions_runner_timeout", "solutions_runner")
    op.drop_constraint("ck_solutions_script_timeout", "solutions_script")
