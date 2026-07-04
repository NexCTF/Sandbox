"""Shared fixtures for the nexctf_sandbox tests.

Importing :mod:`nexctf_sandbox` registers the ``runner`` and ``script`` solution
types. The microVM call (``run_python``) is patched per test so the suite runs
without KVM or a network.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import nexctf_sandbox  # noqa: F401 — import for side effect: registers solution types


def _returns(exit_code: int, stdout: str = "") -> Callable:
    """Build an async run_python stub that always returns the given exit code and stdout."""

    async def _fake(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
        return exit_code, stdout

    return _fake


@pytest.fixture
def patch_run_python(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[object, Callable], None]:
    """Return a setter that swaps ``run_python`` in a solution module for a stub.

    Usage::

        patch_run_python(runner, fake_run_python)

    where ``fake_run_python(code, stdin="", *, timeout) -> tuple[int, str]``.
    """

    def _patch(module: object, fake: Callable) -> None:
        monkeypatch.setattr(module, "run_python", fake)

    return _patch
