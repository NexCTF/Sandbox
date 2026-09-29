"""Script checker solution: runs an admin-provided Python checker in a microVM."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi_toolsets.schemas import PydanticBase
from microsandbox import ExecTimeoutError
from nexctf.exceptions import SolutionTimeoutError
from nexctf.model.custom_field import (
    CustomFieldDefinition,
    CustomFieldTarget,
    CustomFieldType,
    CustomFieldValue,
)
from nexctf.model.solution import Solution
from nexctf.schema.solution import AdminSolutionRead
from nexctf.util.pydantic import CodeStr, DynamicDefault
from pydantic import Field
from sqlalchemy import CheckConstraint, ForeignKey, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session
from sqlalchemy.orm import Mapped, mapped_column

from nexctf_sandbox._sandbox import (
    MAX_PAYLOAD_CHARS,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    run_python,
)

logger = logging.getLogger(__name__)

_CHECK_SIGNATURE = (
    "def check(answer: str, team_id: str | None, team_fields: dict) -> bool:\n"
)
_CHECK_BODY = '    return answer.strip() == "expected_answer"\n'
_DEFAULT_CHECKER = _CHECK_SIGNATURE + _CHECK_BODY

# team_fields goes by name, and only to a checker that can take it: a checker
# written before it existed, even check(answer, team_id, strict=False), is unchanged.
_WRAPPER = """\
import sys as _sys
import json as _json
import inspect as _inspect

{checker_code}

_data = _json.load(_sys.stdin)
_args = (_data["answer"], _data.get("team_id"))
_kwargs = {{"team_fields": _data.get("team_fields") or {{}}}}
try:
    _inspect.signature(check).bind(*_args, **_kwargs)
except (TypeError, ValueError):
    _kwargs = {{}}
_result = check(*_args, **_kwargs)
_sys.exit(0 if bool(_result) else 1)
"""

_CHECKER_DESCRIPTION = (
    "Python function check(answer, team_id, team_fields) → bool. Return True to accept "
    "the answer. team_fields maps each team custom field name to its value (int, bool "
    "or str); declare it only if the checker needs it."
)

# Label shown in the starter checker, and the parser applied before the checker runs.
_FIELD_TYPES: dict[CustomFieldType, tuple[str, Callable[[str], Any]]] = {
    CustomFieldType.integer: ("int", int),
    CustomFieldType.boolean: ("bool", lambda value: value.lower() == "true"),
}
_STR_FIELD = ("str", str)


def checker_template(fields: list[tuple[str, CustomFieldType]]) -> str:
    """Return the default checker, with a comment listing the team custom fields."""
    if not fields:
        return _DEFAULT_CHECKER
    # repr() keeps a field name from ending the comment line or the string.
    lines = [
        f"    #   team_fields[{name!r}]: {_FIELD_TYPES.get(ftype, _STR_FIELD)[0]}\n"
        for name, ftype in fields
    ]
    header = (
        "    # Team custom fields (a key is missing when the team left it empty):\n"
    )
    return "".join([_CHECK_SIGNATURE, header, *lines, _CHECK_BODY])


def _db_context():
    # Imported late: nexctf.core.db reads the host settings at import time.
    from nexctf.core.db import get_db_context

    return get_db_context()


async def _default_checker() -> str:
    """Build the starter checker when the admin form asks for the schema."""
    try:
        async with _db_context() as session:
            rows = await session.execute(
                select(CustomFieldDefinition.name, CustomFieldDefinition.field_type)
                .where(CustomFieldDefinition.target == CustomFieldTarget.team)
                .order_by(CustomFieldDefinition.name)
            )
            return checker_template(list(rows))
    except Exception:
        # A missing hint must not take the whole solution form down with it.
        logger.warning("script.checker template without team fields", exc_info=True)
        return _DEFAULT_CHECKER


class ScriptSolutionCreate(PydanticBase):
    question_id: UUID
    checker_code: Annotated[CodeStr, DynamicDefault(_default_checker)] = Field(
        default=_DEFAULT_CHECKER,
        max_length=MAX_PAYLOAD_CHARS,
        title="Checker function",
        description=_CHECKER_DESCRIPTION,
    )
    timeout: int = Field(
        default=5,
        ge=MIN_TIMEOUT,
        le=MAX_TIMEOUT,
        title="Timeout (s)",
        description="Maximum seconds the checker may run before being killed.",
    )


class ScriptSolutionUpdate(PydanticBase):
    id: UUID
    checker_code: CodeStr | None = Field(
        default=None,
        max_length=MAX_PAYLOAD_CHARS,
        title="Checker function",
        description=_CHECKER_DESCRIPTION,
    )
    timeout: int | None = Field(
        default=None,
        ge=MIN_TIMEOUT,
        le=MAX_TIMEOUT,
        title="Timeout (s)",
        description="Maximum seconds the checker may run before being killed.",
    )


class ScriptSolutionRead(AdminSolutionRead):
    checker_code: CodeStr
    timeout: int


def _field_value(field_type: CustomFieldType, value: str | None) -> Any:
    if value is None:
        return None
    _, parse = _FIELD_TYPES.get(field_type, _STR_FIELD)
    try:
        return parse(value)
    except ValueError:
        return value  # written before its type changed; hand over the raw string


async def load_team_fields(session: AsyncSession, team_id: UUID) -> dict[str, Any]:
    """Return every custom field value of *team_id*, keyed by field name.

    Private fields are included: the checker is admin code, and per-team secrets
    or seeds are the point of passing fields at all.
    """
    rows = await session.execute(
        select(
            CustomFieldDefinition.name,
            CustomFieldDefinition.field_type,
            CustomFieldValue.value,
        )
        .join(
            CustomFieldDefinition,
            CustomFieldValue.definition_id == CustomFieldDefinition.id,
        )
        .where(CustomFieldValue.team_id == team_id)
    )
    return {name: _field_value(ftype, value) for name, ftype, value in rows}


async def run_checker(
    checker_code: str,
    answer: str,
    team_id: UUID | None,
    timeout: int,
    *,
    session: AsyncSession | None = None,
) -> bool:
    code = _WRAPPER.format(checker_code=checker_code)
    if session is None and team_id is not None:
        # A detached solution has nothing to read from, and opening a connection
        # of our own would starve the pool: the checker sees no fields at all.
        logger.warning(
            "script.checker no session, team_fields empty team_id=%s", team_id
        )
    try:
        team_fields = (
            await load_team_fields(session, team_id)
            if session is not None and team_id is not None
            else {}
        )
        stdin = json.dumps(
            {
                "answer": answer,
                "team_id": str(team_id) if team_id else None,
                "team_fields": team_fields,
            }
        )
        exit_code, _ = await run_python(code, stdin, timeout=timeout)
        return exit_code == 0
    except ExecTimeoutError:
        raise
    except Exception:
        logger.exception("script.checker failed team_id=%s", team_id)
        return False


class ScriptSolution(Solution):
    """Custom checker: admin provides a Python check(answer, team_id, team_fields) -> bool function."""

    __tablename__ = "solutions_script"
    __mapper_args__ = {"polymorphic_identity": "script"}  # noqa: RUF012 — SQLAlchemy idiom
    __table_args__ = (
        CheckConstraint(
            f"timeout BETWEEN {MIN_TIMEOUT} AND {MAX_TIMEOUT}",
            name="ck_solutions_script_timeout",
        ),
        CheckConstraint(
            f"length(checker_code) <= {MAX_PAYLOAD_CHARS}",
            name="ck_solutions_script_checker_code",
        ),
    )

    id: Mapped[UUID] = mapped_column(ForeignKey("solutions.id"), primary_key=True)
    checker_code: Mapped[str] = mapped_column(Text)
    timeout: Mapped[int] = mapped_column(default=5)

    async def verify(self, submission: str, *, team_id: UUID | None = None) -> bool:
        try:
            return await run_checker(
                self.checker_code,
                submission,
                team_id,
                self.timeout,
                session=async_object_session(self),
            )
        except ExecTimeoutError as exc:
            logger.warning("script.checker timed out solution_id=%s", self.id)
            raise SolutionTimeoutError(self.id) from exc
