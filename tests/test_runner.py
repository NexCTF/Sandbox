"""Tests for the ``runner`` solution type (code runner against test cases)."""

from __future__ import annotations

from uuid import uuid4

from microsandbox import ExecTimeoutError

from nexctf.plugins.registry import solution_registry
from nexctf.plugins.testing import assert_registered, assert_verifies
from nexctf_sandbox.solutions import runner
from nexctf_sandbox.solutions.runner import (
    RunnerSolution,
    RunnerSolutionCreate,
    RunnerSolutionRead,
    RunnerSolutionUpdate,
)

from .conftest import _returns


async def _echo_stdin(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Stand-in program that exits 0 and echoes its stdin to stdout."""
    return 0, stdin


async def test_verify_true_when_all_cases_match(patch_run_python) -> None:
    """All test-case outputs match the program output → accepted."""
    patch_run_python(runner, _echo_stdin)
    solution = RunnerSolution(
        test_cases=[
            {"input": "hello", "expected_output": "hello"},
            {"input": "world", "expected_output": "world"},
        ],
        timeout=5,
    )
    await assert_verifies(solution, [("any code", True)])


async def test_verify_false_when_one_case_mismatches(patch_run_python) -> None:
    patch_run_python(runner, _echo_stdin)
    solution = RunnerSolution(
        test_cases=[
            {"input": "hello", "expected_output": "hello"},
            {"input": "world", "expected_output": "WRONG"},
        ],
        timeout=5,
    )
    await assert_verifies(solution, [("any code", False)])


async def test_verify_false_when_no_test_cases(patch_run_python) -> None:
    """A solution with no test cases can never be satisfied (and never runs code)."""

    async def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("run_python should not be called without test cases")

    patch_run_python(runner, _boom)
    await assert_verifies(RunnerSolution(test_cases=[], timeout=5), [("code", False)])


async def test_verify_strips_whitespace_before_comparing(patch_run_python) -> None:
    """Output is compared after stripping, so surrounding whitespace is ignored."""
    patch_run_python(runner, _returns(0, "  42 \n"))
    solution = RunnerSolution(
        test_cases=[{"input": "", "expected_output": "42"}], timeout=5
    )
    await assert_verifies(solution, [("code", True)])


async def test_verify_false_on_nonzero_exit(patch_run_python) -> None:
    """A non-zero exit code is treated as a failed run (None) → rejected."""
    patch_run_python(runner, _returns(1, "42"))
    solution = RunnerSolution(
        test_cases=[{"input": "", "expected_output": "42"}], timeout=5
    )
    await assert_verifies(solution, [("code", False)])


async def test_verify_false_on_timeout(patch_run_python) -> None:
    """A microVM timeout is caught and rejected rather than propagated."""

    async def _timeout(code, stdin="", *, timeout):
        raise ExecTimeoutError("timed out")

    patch_run_python(runner, _timeout)
    solution = RunnerSolution(
        test_cases=[{"input": "", "expected_output": "42"}], timeout=5
    )
    await assert_verifies(solution, [("code", False)])


def test_runner_is_registered() -> None:
    assert_registered(
        solution_registry,
        "runner",
        model=RunnerSolution,
        create_schema=RunnerSolutionCreate,
        update_schema=RunnerSolutionUpdate,
        read_schema=RunnerSolutionRead,
    )


def test_create_schema_defaults() -> None:
    create = RunnerSolutionCreate(question_id=uuid4())
    assert create.timeout == 5
    assert create.test_cases == []
