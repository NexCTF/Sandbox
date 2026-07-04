"""Tests for the shared microsandbox helper (``_sandbox.run_python``)."""

from __future__ import annotations

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


async def test_run_python_sends_stdin_as_bytes(monkeypatch) -> None:
    """microsandbox reads a bare str as a stdin mode name, not data — regression guard
    for a bug where plain str stdin raised ``ValueError: unknown stdin mode``."""
    fake = _FakeSandbox()

    async def _fake_create(name, **kwargs):
        return fake

    monkeypatch.setattr(_sandbox.Sandbox, "create", _fake_create)

    await _sandbox.run_python("print('hi')", "hello world", timeout=5)

    assert fake.shell_kwargs is not None
    assert fake.shell_kwargs["stdin"] == b"hello world"


async def test_run_python_sends_none_for_empty_stdin(monkeypatch) -> None:
    fake = _FakeSandbox()

    async def _fake_create(name, **kwargs):
        return fake

    monkeypatch.setattr(_sandbox.Sandbox, "create", _fake_create)

    await _sandbox.run_python("print('hi')", "", timeout=5)

    assert fake.shell_kwargs is not None
    assert fake.shell_kwargs["stdin"] is None
