"""Runner solution — executes player Python3 code against test cases.

Bundles the model, schemas, and microsandbox runner for the ``runner``
solution type in a single module.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi_toolsets.schemas import PydanticBase
from microsandbox import ExecTimeoutError
from pydantic import Field
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexctf.model.solution import Solution
from nexctf.schema.solution import AdminSolutionRead
from nexctf_sandbox._sandbox import run_python

logger = logging.getLogger(__name__)


class TestCase(PydanticBase):
    input: str = Field(
        default="",
        title="Input (stdin)",
        description="Text fed to the program on stdin.",
    )
    expected_output: str = Field(
        title="Expected output",
        description="Expected stdout, compared after stripping whitespace.",
    )


class RunnerSolutionCreate(PydanticBase):
    question_id: UUID
    test_cases: list[TestCase] = Field(
        default=[],
        title="Test cases",
        description="All test cases must pass for the answer to be accepted.",
    )
    timeout: int = Field(
        default=5,
        title="Timeout (s)",
        description="Maximum seconds each test case may run.",
    )


class RunnerSolutionUpdate(PydanticBase):
    id: UUID
    test_cases: list[TestCase] | None = Field(default=None, title="Test cases")
    timeout: int | None = Field(default=None, title="Timeout (s)")


class RunnerSolutionRead(AdminSolutionRead):
    test_cases: list[TestCase]
    timeout: int


async def run_code(code: str, stdin: str, timeout: int) -> str | None:
    """Execute player *code* in an isolated microVM and return stdout, or None on error/timeout."""
    try:
        exit_code, stdout = await run_python(code, stdin, timeout=timeout)
        return stdout if exit_code == 0 else None
    except ExecTimeoutError:
        logger.warning("runner timed out")
        return None
    except Exception:
        logger.exception("runner failed")
        return None


class RunnerSolution(Solution):
    """Runs submitted Python3 code against a set of test cases (all must pass)."""

    __tablename__ = "solutions_runner"
    __mapper_args__ = {"polymorphic_identity": "runner"}

    id: Mapped[UUID] = mapped_column(ForeignKey("solutions.id"), primary_key=True)
    test_cases: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    timeout: Mapped[int] = mapped_column(default=5)

    async def verify(self, submission: str, *, team_id=None) -> bool:
        if not self.test_cases:
            return False
        for tc in self.test_cases:
            stdin = tc.get("input", "")
            expected = tc.get("expected_output", "")
            actual = await run_code(submission, stdin, self.timeout)
            if actual is None or actual.strip() != expected.strip():
                return False
        return True
