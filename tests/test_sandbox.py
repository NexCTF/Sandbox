"""Tests for the shared microsandbox helper (``_sandbox.run_python``)."""

from __future__ import annotations

import asyncio

import pytest

from nexctf_sandbox import _sandbox


class _FakeShellResult:
    def __init__(self, exit_code: int, stdout_text: str) -> None:
        self.exit_code = exit_code
        self.stdout_text = stdout_text
        self.stderr_text = ""


class _FakeFs:
    async def write(self, path: str, data: bytes) -> None:
        pass


class _FakeSandbox:
    def __init__(self) -> None:
        self.fs = _FakeFs()
        self.shell_kwargs: dict | None = None

    async def shell(self, cmd: str, *, stdin=None, timeout=None):
        self.shell_kwargs = {"cmd": cmd, "stdin": stdin, "timeout": timeout}
        return _FakeShellResult(0, "ok")

    async def kill(self) -> None:
        pass


def _patch_sandbox(monkeypatch) -> tuple[_FakeSandbox, list[str]]:
    """Swap Sandbox.create/remove for fakes; return the fake VM and the removed names."""
    fake = _FakeSandbox()
    removed: list[str] = []

    async def _fake_create(name, **kwargs):
        return fake

    async def _fake_remove(name):
        removed.append(name)

    monkeypatch.setattr(_sandbox.Sandbox, "create", _fake_create)
    monkeypatch.setattr(_sandbox.Sandbox, "remove", _fake_remove)
    return fake, removed


async def test_run_python_sends_stdin_as_bytes(monkeypatch) -> None:
    """microsandbox reads a bare str as a stdin mode name, not data — regression guard
    for a bug where plain str stdin raised ``ValueError: unknown stdin mode``."""
    fake, _ = _patch_sandbox(monkeypatch)

    await _sandbox.run_python("print('hi')", "hello world", timeout=5)

    assert fake.shell_kwargs is not None
    assert fake.shell_kwargs["stdin"] == b"hello world"


async def test_run_python_sends_none_for_empty_stdin(monkeypatch) -> None:
    fake, _ = _patch_sandbox(monkeypatch)

    await _sandbox.run_python("print('hi')", "", timeout=5)

    assert fake.shell_kwargs is not None
    assert fake.shell_kwargs["stdin"] is None


async def test_output_is_capped_in_the_guest(monkeypatch) -> None:
    """Untrusted code can print faster than the timeout ends and the whole payload
    lands in the API process, so both streams are truncated before they come back."""
    fake, _ = _patch_sandbox(monkeypatch)

    await _sandbox.run_python("print('hi')", timeout=5)

    assert fake.shell_kwargs is not None
    cmd = fake.shell_kwargs["cmd"]
    assert cmd.count(f"head -c {_sandbox._MAX_OUTPUT_BYTES}") == 2
    assert "exit $rc" in cmd  # the program's own exit code, not head's


async def test_sandbox_is_removed_not_just_killed(monkeypatch) -> None:
    """kill() leaves the registration and its disk on the host — remove() must follow,
    or every submission leaks host disk permanently."""
    _, removed = _patch_sandbox(monkeypatch)

    await _sandbox.run_python("print('hi')", timeout=5)

    assert len(removed) == 1
    assert removed[0].startswith("nexctf-")


async def test_sandbox_is_removed_when_the_run_raises(monkeypatch) -> None:
    fake, removed = _patch_sandbox(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("exec blew up")

    monkeypatch.setattr(fake, "shell", _boom)

    with pytest.raises(RuntimeError):
        await _sandbox.run_python("print('hi')", timeout=5)

    assert len(removed) == 1


async def test_concurrent_runs_are_capped(monkeypatch) -> None:
    """Nothing else limits how many microVMs exist at once — each is a vCPU plus
    _MEMORY_MIB, and rate limiting is per user, not per host."""
    live = 0
    peak = 0

    class _SlowSandbox(_FakeSandbox):
        async def shell(self, cmd, *, stdin=None, timeout=None):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0)
            live -= 1
            return await super().shell(cmd, stdin=stdin, timeout=timeout)

    async def _fake_create(name, **kwargs):
        return _SlowSandbox()

    monkeypatch.setattr(_sandbox.Sandbox, "create", _fake_create)
    monkeypatch.setattr(_sandbox.Sandbox, "remove", lambda name: _noop())
    monkeypatch.setattr(_sandbox, "_SLOTS", asyncio.Semaphore(2))

    await asyncio.gather(*(_sandbox.run_python("x", timeout=5) for _ in range(10)))

    assert peak <= 2


async def _noop() -> None:
    pass
