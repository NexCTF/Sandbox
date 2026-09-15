"""Tests for the ``runner`` solution type (code runner against test cases)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from microsandbox import ExecTimeoutError
from nexctf.exceptions import SolutionTimeoutError
from nexctf.plugins.registry import solution_registry
from nexctf.plugins.testing import assert_registered, assert_verifies
from pydantic import ValidationError

from nexctf_sandbox._sandbox import MAX_PAYLOAD_CHARS, MAX_TIMEOUT
from nexctf_sandbox.solutions import runner
from nexctf_sandbox.solutions.runner import (
    MAX_TEST_CASES,
    RunnerSolution,
    RunnerSolutionCreate,
    RunnerSolutionRead,
    RunnerSolutionUpdate,
)

from .conftest import _returns


async def _echo_stdin(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Stand-in program that exits 0 and echoes its stdin to stdout."""
    return 0, stdin


async def test_verify_true_when_all_cases_match(patch_python_runner) -> None:
    """All test-case outputs match the program output → accepted."""
    patch_python_runner(runner, _echo_stdin)
    solution = RunnerSolution(
        test_cases=[
            {"input": "hello", "expected_output": "hello"},
            {"input": "world", "expected_output": "world"},
        ],
        timeout=5,
    )
    await assert_verifies(solution, [("any code", True)])


async def test_verify_false_when_one_case_mismatches(patch_python_runner) -> None:
    patch_python_runner(runner, _echo_stdin)
    solution = RunnerSolution(
        test_cases=[
            {"input": "hello", "expected_output": "hello"},
            {"input": "world", "expected_output": "WRONG"},
        ],
        timeout=5,
    )
    await assert_verifies(solution, [("any code", False)])


async def test_verify_false_when_no_test_cases(monkeypatch) -> None:
    """A solution with no test cases can never be satisfied, so it must not pay a boot."""
    boots = 0

    @asynccontextmanager
    async def _runner():
        nonlocal boots
        boots += 1
        yield _echo_stdin

    monkeypatch.setattr(runner, "python_runner", _runner)
    await assert_verifies(RunnerSolution(test_cases=[], timeout=5), [("code", False)])

    assert boots == 0


async def test_verify_strips_whitespace_before_comparing(patch_python_runner) -> None:
    """Output is compared after stripping, so surrounding whitespace is ignored."""
    patch_python_runner(runner, _returns(0, "  42 \n"))
    solution = RunnerSolution(
        test_cases=[{"input": "", "expected_output": "42"}], timeout=5
    )
    await assert_verifies(solution, [("code", True)])


async def test_verify_false_on_nonzero_exit(patch_python_runner) -> None:
    """A non-zero exit code is treated as a failed run (None) → rejected."""
    patch_python_runner(runner, _returns(1, "42"))
    solution = RunnerSolution(
        test_cases=[{"input": "", "expected_output": "42"}], timeout=5
    )
    await assert_verifies(solution, [("code", False)])


async def test_verify_raises_on_timeout(patch_python_runner) -> None:
    """A loaded host must not turn a correct answer into a wrong one silently: the
    platform has a first-class SolutionTimeoutError and emits solution.timeout admin
    events for it, so the timeout is converted, not swallowed into False."""

    async def _timeout(code, stdin="", *, timeout):
        raise ExecTimeoutError("timed out")

    patch_python_runner(runner, _timeout)
    solution = RunnerSolution(
        test_cases=[{"input": "", "expected_output": "42"}], timeout=5
    )
    with pytest.raises(SolutionTimeoutError):
        await solution.verify("code")


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


def test_create_schema_rejects_unbounded_cost() -> None:
    """One verify() costs N x (boot + timeout) with no total budget, so both factors
    are capped at the schema."""
    for timeout in (0, MAX_TIMEOUT + 1):  # 0 times out at 0ns: every answer wrong
        with pytest.raises(ValidationError):
            RunnerSolutionCreate(question_id=uuid4(), timeout=timeout)

    with pytest.raises(ValidationError):
        RunnerSolutionCreate(
            question_id=uuid4(),
            test_cases=[{"expected_output": "x"}] * (MAX_TEST_CASES + 1),
        )


def test_create_schema_bounds_test_case_payloads() -> None:
    """input and expected_output ship into the VM on every submission, so neither is
    unbounded; expected_output past the output cap could never match anyway."""
    oversized = "x" * (MAX_PAYLOAD_CHARS + 1)
    for case in (
        {"expected_output": oversized},
        {"input": oversized, "expected_output": "x"},
    ):
        with pytest.raises(ValidationError):
            RunnerSolutionCreate(question_id=uuid4(), test_cases=[case])


async def test_all_cases_share_one_microvm(monkeypatch) -> None:
    """Boot is ~1.1s and dominated a short case: 3 cases measured 3.07s booting one
    VM each vs 1.05s reusing one, so verify() opens the sandbox once per submission."""
    boots = 0

    @asynccontextmanager
    async def _runner():
        nonlocal boots
        boots += 1
        yield _echo_stdin

    monkeypatch.setattr(runner, "python_runner", _runner)
    solution = RunnerSolution(
        test_cases=[{"input": c, "expected_output": c} for c in "abc"], timeout=5
    )
    await assert_verifies(solution, [("any code", True)])

    assert boots == 1
