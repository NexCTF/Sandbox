"""Tests for the ``script`` solution type (admin-provided checker function)."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from microsandbox import ExecTimeoutError
from nexctf.exceptions import SolutionTimeoutError
from nexctf.model.custom_field import CustomFieldType
from nexctf.plugins.registry import solution_registry
from nexctf.plugins.testing import assert_registered, assert_verifies
from nexctf.util.pydantic import resolve_dynamic_defaults
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from nexctf_sandbox._sandbox import MAX_PAYLOAD_CHARS, MAX_TIMEOUT
from nexctf_sandbox.solutions import script
from nexctf_sandbox.solutions.script import (
    _DEFAULT_CHECKER,
    _WRAPPER,
    ScriptSolution,
    ScriptSolutionCreate,
    ScriptSolutionRead,
    ScriptSolutionUpdate,
)

from .conftest import _returns

_PAYLOAD = {"answer": "ans", "team_id": "T", "team_fields": {"seed": 1}}


def _session(rows) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = rows
    return session


@pytest.fixture
def patch_team_fields(monkeypatch):
    """Attach the solution to a stand-in session whose fields come from *load*."""
    session = object()

    def _patch(load) -> object:
        monkeypatch.setattr(script, "async_object_session", lambda obj: session)
        monkeypatch.setattr(script, "load_team_fields", load)
        return session

    return _patch


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


async def test_verify_raises_on_timeout(patch_run_python) -> None:
    """Swallowing this into False marks a correct answer wrong whenever the sandbox
    host is merely loaded, and hides the load from the admin event feed."""

    async def _timeout(code, stdin="", *, timeout):
        raise ExecTimeoutError("timed out")

    patch_run_python(script, _timeout)
    with pytest.raises(SolutionTimeoutError):
        await ScriptSolution(checker_code="...", timeout=5).verify("ans")


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
    assert payload == {
        "answer": "my-answer",
        "team_id": str(team_id),
        "team_fields": {},
    }


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
    assert payload == {"answer": "my-answer", "team_id": None, "team_fields": {}}


async def test_checker_receives_team_fields_from_the_solution_session(
    patch_run_python, patch_team_fields
) -> None:
    """Fields are read through the session the solution was loaded with."""
    captured: dict[str, str] = {}
    loaded_with: list = []
    team_id = uuid4()

    async def _capture(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
        captured["stdin"] = stdin
        return 0, ""

    async def _load(s, tid):
        loaded_with.append((s, tid))
        return {"seed": 42, "hardmode": True}

    patch_run_python(script, _capture)
    session = patch_team_fields(_load)
    solution = ScriptSolution(checker_code="...", timeout=5)
    await solution.verify("my-answer", team_id=team_id)

    assert loaded_with == [(session, team_id)]
    payload = json.loads(captured["stdin"])
    assert payload["team_fields"] == {"seed": 42, "hardmode": True}


async def test_verify_false_when_team_fields_cannot_be_read(
    patch_run_python, patch_team_fields
) -> None:
    """A database error fails closed like any checker failure, not a 500."""

    async def _load(s, tid):
        raise RuntimeError("db down")

    patch_run_python(script, _returns(0))
    patch_team_fields(_load)
    await assert_verifies(
        ScriptSolution(checker_code="...", timeout=5), [("ans", False)], team_id=uuid4()
    )


async def test_load_team_fields_coerces_by_field_type() -> None:
    rows = [
        ("seed", CustomFieldType.integer, "42"),
        ("hardmode", CustomFieldType.boolean, "True"),
        ("country", CustomFieldType.string, "FR"),
        ("site", CustomFieldType.url, "https://example.org"),
        ("broken", CustomFieldType.integer, "not-a-number"),
        ("empty", CustomFieldType.string, None),
    ]
    session = _session(rows)

    fields = await script.load_team_fields(session, uuid4())
    (stmt,) = session.execute.await_args.args
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FROM custom_field_values JOIN custom_field_definitions ON" in sql
    assert "custom_field_values.team_id" in sql
    assert fields == {
        "seed": 42,
        "hardmode": True,
        "country": "FR",
        "site": "https://example.org",
        "broken": "not-a-number",
        "empty": None,
    }


def _run_wrapper(checker_code: str, payload: dict) -> int:
    code = _WRAPPER.format(checker_code=checker_code)
    return subprocess.run(
        [sys.executable, "-c", code], input=json.dumps(payload), text=True, check=False
    ).returncode


@pytest.mark.parametrize(
    "checker",
    [
        "def check(a, t, team_fields): return team_fields == {'seed': 1} and t == 'T'",
        "def check(a, t, team_fields=None): return team_fields == {'seed': 1}",
        "def check(a, t, *, team_fields): return team_fields['seed'] == 1",
        "def check(a, t, **kw): return kw == {'team_fields': {'seed': 1}}",
    ],
)
def test_wrapper_passes_team_fields_to_checkers_that_take_it(checker: str) -> None:
    assert _run_wrapper(checker, _PAYLOAD) == 0


@pytest.mark.parametrize(
    "checker",
    [
        "def check(a, t): return (a, t) == ('ans', 'T')",
        "def check(a, t, *, strict=True): return strict",
        # A third parameter that is not team_fields must keep its own default.
        "def check(a, t, strict=False): return strict is False",
        "def check(*args): return args == ('ans', 'T')",
        _DEFAULT_CHECKER.replace("expected_answer", "ans").replace(
            ", team_fields: dict", ""
        ),
    ],
)
def test_wrapper_keeps_two_arg_checkers_working(checker: str) -> None:
    assert _run_wrapper(checker, _PAYLOAD) == 0


def test_default_checker_runs_in_the_wrapper() -> None:
    payload = {"answer": "expected_answer", "team_id": None, "team_fields": {}}
    assert _run_wrapper(_DEFAULT_CHECKER, payload) == 0
    assert _run_wrapper(_DEFAULT_CHECKER, {**payload, "answer": "nope"}) == 1


def test_checker_template_lists_team_fields() -> None:
    template = script.checker_template(
        [("seed", CustomFieldType.integer), ("hard mode", CustomFieldType.boolean)]
    )
    assert "#   team_fields['seed']: int" in template
    assert "#   team_fields['hard mode']: bool" in template
    assert _run_wrapper(template, {"answer": "expected_answer", "team_id": None}) == 0


def test_checker_template_without_fields_is_the_default() -> None:
    assert script.checker_template([]) == _DEFAULT_CHECKER


def test_checker_template_survives_hostile_field_names() -> None:
    """A field name is admin text; it must not escape its comment line."""
    template = script.checker_template(
        [("x\nimport os; os._exit(0)", CustomFieldType.string)]
    )
    assert _run_wrapper(template, {"answer": "nope", "team_id": None}) == 1


async def test_create_schema_default_lists_team_fields(monkeypatch) -> None:
    """The admin form reads the starter checker from the resolved schema."""

    @asynccontextmanager
    async def _db():
        yield _session([("seed", CustomFieldType.integer)])

    monkeypatch.setattr(script, "_db_context", _db)
    schema = await resolve_dynamic_defaults(ScriptSolutionCreate)
    assert "team_fields['seed']" in schema["properties"]["checker_code"]["default"]
    assert schema["properties"]["checker_code"]["x-ui-widget"] == "code"


async def test_create_schema_default_falls_back_when_db_fails(monkeypatch) -> None:
    def _db():
        raise RuntimeError("db down")

    monkeypatch.setattr(script, "_db_context", _db)
    schema = await resolve_dynamic_defaults(ScriptSolutionCreate)
    assert schema["properties"]["checker_code"]["default"] == _DEFAULT_CHECKER


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


def test_create_schema_rejects_unbounded_timeout() -> None:
    for timeout in (0, MAX_TIMEOUT + 1):
        with pytest.raises(ValidationError):
            ScriptSolutionCreate(question_id=uuid4(), timeout=timeout)


def test_create_schema_bounds_checker_code() -> None:
    """CodeStr carries a UI hint, not a length bound — a 50 MB checker would be
    formatted, encoded and shipped into the VM on every submission."""
    with pytest.raises(ValidationError):
        ScriptSolutionCreate(
            question_id=uuid4(), checker_code="x" * (MAX_PAYLOAD_CHARS + 1)
        )
