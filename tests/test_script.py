"""Tests for the ``script`` solution type (admin-provided checker function)."""

from __future__ import annotations

import json
from uuid import uuid4

from microsandbox import ExecTimeoutError

from nexctf.plugins.registry import solution_registry
from nexctf.plugins.testing import assert_registered, assert_verifies
from nexctf_sandbox.solutions import script
from nexctf_sandbox.solutions.script import (
    _DEFAULT_CHECKER,
    ScriptSolution,
    ScriptSolutionCreate,
    ScriptSolutionRead,
    ScriptSolutionUpdate,
)

from .conftest import _returns


async def test_verify_true_when_checker_exits_zero(patch_run_python) -> None:
    patch_run_python(script, _returns(0))
    await assert_verifies(
        ScriptSolution(checker_code="...", timeout=5), [("ans", True)], team_id=uuid4()
    )


async def test_verify_false_when_checker_exits_nonzero(patch_run_python) -> None:
    patch_run_python(script, _returns(1))
    await assert_verifies(
        ScriptSolution(checker_code="...", timeout=5), [("ans", False)]
    )


async def test_verify_false_on_timeout(patch_run_python) -> None:
    async def _timeout(code, stdin="", *, timeout):
        raise ExecTimeoutError("timed out")

    patch_run_python(script, _timeout)
    await assert_verifies(
        ScriptSolution(checker_code="...", timeout=5), [("ans", False)]
    )


async def test_verify_false_on_unexpected_error(patch_run_python) -> None:
    """Any checker failure is swallowed and rejected, never raised to the caller."""

    async def _boom(code, stdin="", *, timeout):
        raise RuntimeError("boom")

    patch_run_python(script, _boom)
    await assert_verifies(
        ScriptSolution(checker_code="...", timeout=5), [("ans", False)]
    )


async def test_checker_receives_answer_and_team_id(patch_run_python) -> None:
    """The wrapper embeds the checker code and feeds answer/team_id as JSON stdin."""
    captured: dict[str, str] = {}

    async def _capture(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
        captured["code"] = code
        captured["stdin"] = stdin
        return 0, ""

    patch_run_python(script, _capture)
    team_id = uuid4()
    solution = ScriptSolution(checker_code="def check(a, t): return True", timeout=5)
    await solution.verify("my-answer", team_id=team_id)

    assert "def check(a, t): return True" in captured["code"]
    payload = json.loads(captured["stdin"])
    assert payload == {"answer": "my-answer", "team_id": str(team_id)}


async def test_checker_receives_null_team_id_when_none(patch_run_python) -> None:
    """team_id=None must serialize to JSON null, not the string 'None'."""
    captured: dict[str, str] = {}

    async def _capture(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
        captured["stdin"] = stdin
        return 0, ""

    patch_run_python(script, _capture)
    solution = ScriptSolution(checker_code="def check(a, t): return True", timeout=5)
    await solution.verify("my-answer", team_id=None)

    payload = json.loads(captured["stdin"])
    assert payload == {"answer": "my-answer", "team_id": None}


def test_script_is_registered() -> None:
    assert_registered(
        solution_registry,
        "script",
        model=ScriptSolution,
        create_schema=ScriptSolutionCreate,
        update_schema=ScriptSolutionUpdate,
        read_schema=ScriptSolutionRead,
    )


def test_create_schema_defaults() -> None:
    create = ScriptSolutionCreate(question_id=uuid4())
    assert create.timeout == 5
    assert create.checker_code == _DEFAULT_CHECKER
