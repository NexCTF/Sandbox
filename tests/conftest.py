"""Shared fixtures for the nexctf_sandbox tests."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

import pytest

import nexctf_sandbox  # noqa: F401 — import for side effect: registers solution types
from nexctf_sandbox import _sandbox


@pytest.fixture(autouse=True)
def _assume_the_host_can_filter(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pretend this process holds CAP_NET_ADMIN, unless a test says otherwise."""
    if request.node.get_closest_marker("real_capability_probe"):
        return
    monkeypatch.setattr(_sandbox, "_can_enforce_network", lambda: True)


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


@pytest.fixture
def patch_python_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[object, Callable], None]:
    """Like :func:`patch_run_python`, for modules that reuse one VM across runs.

    The stub has the same signature; it is just handed out by a context manager
    instead of being called directly.
    """

    def _patch(module: object, fake: Callable) -> None:
        @asynccontextmanager
        async def _runner():
            yield fake

        monkeypatch.setattr(module, "python_runner", _runner)

    return _patch
