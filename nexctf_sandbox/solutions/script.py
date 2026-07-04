"""Script checker solution — runs an admin-provided Python checker in a microVM.

Bundles the model, schemas, and microsandbox runner for the ``script`` solution
type in a single module. The admin writes a complete
``check(answer, team_id) -> bool`` function; input is passed via stdin as JSON
and exit code 0 means correct.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from fastapi_toolsets.schemas import PydanticBase
from microsandbox import ExecTimeoutError
from pydantic import Field
from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexctf.model.solution import Solution
from nexctf.schema.solution import AdminSolutionRead
from nexctf.util.pydantic import CodeStr
from nexctf_sandbox._sandbox import run_python

logger = logging.getLogger(__name__)

_DEFAULT_CHECKER = """\
def check(answer: str, team_id: str | None) -> bool:
    return answer.strip() == "expected_answer"
"""

_WRAPPER = """\
import sys as _sys
import json as _json

{checker_code}

_data = _json.load(_sys.stdin)
_result = check(_data["answer"], _data.get("team_id"))
_sys.exit(0 if bool(_result) else 1)
"""


class ScriptSolutionCreate(PydanticBase):
    question_id: UUID
    checker_code: CodeStr = Field(
        default=_DEFAULT_CHECKER,
        title="Checker function",
        description="Python function check(answer, team_id) → bool. Return True to accept the answer.",
    )
    timeout: int = Field(
        default=5,
        title="Timeout (s)",
        description="Maximum seconds the checker may run before being killed.",
    )


class ScriptSolutionUpdate(PydanticBase):
    id: UUID
    checker_code: CodeStr | None = Field(
        default=None,
        title="Checker function",
        description="Python function check(answer, team_id) → bool. Return True to accept the answer.",
    )
    timeout: int | None = Field(
        default=None,
        title="Timeout (s)",
        description="Maximum seconds the checker may run before being killed.",
    )


class ScriptSolutionRead(AdminSolutionRead):
    checker_code: CodeStr
    timeout: int


async def run_checker(
    checker_code: str,
    answer: str,
    team_id: UUID | None,
    timeout: int,
) -> bool:
    code = _WRAPPER.format(checker_code=checker_code)
    stdin = json.dumps({"answer": answer, "team_id": str(team_id) if team_id else None})
    try:
        exit_code, _ = await run_python(code, stdin, timeout=timeout)
        return exit_code == 0
    except ExecTimeoutError:
        logger.warning("script.checker timed out team_id=%s", team_id)
        return False
    except Exception:
        logger.exception("script.checker failed team_id=%s", team_id)
        return False


class ScriptSolution(Solution):
    """Custom checker: admin provides a Python check(answer, team_id) -> bool function."""

    __tablename__ = "solutions_script"
    __mapper_args__ = {"polymorphic_identity": "script"}

    id: Mapped[UUID] = mapped_column(ForeignKey("solutions.id"), primary_key=True)
    checker_code: Mapped[str] = mapped_column(Text)
    timeout: Mapped[int] = mapped_column(default=5)

    async def verify(self, submission: str, *, team_id: UUID | None = None) -> bool:
        return await run_checker(self.checker_code, submission, team_id, self.timeout)
