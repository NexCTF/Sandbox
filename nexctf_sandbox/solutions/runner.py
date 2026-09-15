"""Runner solution: executes player Python3 code against test cases."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi_toolsets.schemas import PydanticBase
from microsandbox import ExecTimeoutError
from nexctf.exceptions import SolutionTimeoutError
from nexctf.model.solution import Solution
from nexctf.schema.solution import AdminSolutionRead
from pydantic import Field
from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexctf_sandbox._sandbox import (
    MAX_PAYLOAD_CHARS,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    python_runner,
)

logger = logging.getLogger(__name__)

MAX_TEST_CASES = 20  # all of them share one microVM, run one after the other


class TestCase(PydanticBase):
    input: str = Field(
        default="",
        max_length=MAX_PAYLOAD_CHARS,
        title="Input (stdin)",
        description="Text fed to the program on stdin.",
    )
    expected_output: str = Field(
        max_length=MAX_PAYLOAD_CHARS,
        title="Expected output",
        description="Expected stdout, compared after stripping whitespace.",
    )


class RunnerSolutionCreate(PydanticBase):
    question_id: UUID
    test_cases: list[TestCase] = Field(
        default=[],
        max_length=MAX_TEST_CASES,
        title="Test cases",
        description="All test cases must pass for the answer to be accepted.",
    )
    timeout: int = Field(
        default=5,
        ge=MIN_TIMEOUT,
        le=MAX_TIMEOUT,
        title="Timeout (s)",
        description="Maximum seconds each test case may run.",
    )


class RunnerSolutionUpdate(PydanticBase):
    id: UUID
    test_cases: list[TestCase] | None = Field(
        default=None, max_length=MAX_TEST_CASES, title="Test cases"
    )
    timeout: int | None = Field(
        default=None, ge=MIN_TIMEOUT, le=MAX_TIMEOUT, title="Timeout (s)"
    )


class RunnerSolutionRead(AdminSolutionRead):
    test_cases: list[TestCase]
    timeout: int


async def run_code(run, code: str, stdin: str, timeout: int) -> str | None:
    """Execute player *code* on *run*'s microVM and return stdout, or None on error.

    Raises ``ExecTimeoutError`` on timeout: a timeout says nothing about the answer,
    so it must not be flattened into "wrong".
    """
    try:
        exit_code, stdout = await run(code, stdin, timeout=timeout)
        return stdout if exit_code == 0 else None
    except ExecTimeoutError:
        raise
    except Exception:
        logger.exception("runner failed")
        return None


class RunnerSolution(Solution):
    """Runs submitted Python3 code against a set of test cases (all must pass)."""

    __tablename__ = "solutions_runner"
    __mapper_args__ = {"polymorphic_identity": "runner"}  # noqa: RUF012 — SQLAlchemy idiom
    __table_args__ = (
        CheckConstraint(
            f"timeout BETWEEN {MIN_TIMEOUT} AND {MAX_TIMEOUT}",
            name="ck_solutions_runner_timeout",
        ),
        CheckConstraint(
            f"jsonb_array_length(test_cases) <= {MAX_TEST_CASES}",
            name="ck_solutions_runner_test_cases",
        ),
        # Backstop for the per-field max_length above: one ceiling on the whole blob,
        # because a CHECK cannot walk the array (no subqueries).
        CheckConstraint(
            f"length(test_cases::text) <= {MAX_TEST_CASES * 2 * MAX_PAYLOAD_CHARS}",
            name="ck_solutions_runner_test_cases_size",
        ),
    )

    id: Mapped[UUID] = mapped_column(ForeignKey("solutions.id"), primary_key=True)
    test_cases: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    timeout: Mapped[int] = mapped_column(default=5)

    async def verify(self, submission: str, *, team_id=None) -> bool:
        if not self.test_cases:
            return False
        try:
            # One VM for the whole submission: booting one per case cost ~1.1s each.
            # The cases see each other's guest state; they cannot see another player.
            async with python_runner() as run:
                for tc in self.test_cases:
                    stdin = tc.get("input", "")
                    expected = tc.get("expected_output", "")
                    actual = await run_code(run, submission, stdin, self.timeout)
                    if actual is None or actual.strip() != expected.strip():
                        return False
        except ExecTimeoutError as exc:
            logger.warning("runner timed out solution_id=%s", self.id)
            raise SolutionTimeoutError(self.id) from exc
        return True
