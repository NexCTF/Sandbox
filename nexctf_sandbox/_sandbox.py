"""Shared microsandbox helper for solution plugins that execute untrusted code."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from microsandbox import Network, Sandbox

logger = logging.getLogger(__name__)

_IMAGE = "python:3.12-slim"
_CPUS = 1
_MEMORY_MIB = 256


@asynccontextmanager
async def _ephemeral():
    name = f"nexctf-{uuid4().hex}"
    sb = await Sandbox.create(
        name,
        image=_IMAGE,
        cpus=_CPUS,
        memory=_MEMORY_MIB,
        network=Network.none(),
    )
    try:
        yield sb
    finally:
        try:
            await sb.kill()
        except Exception:
            logger.debug("sandbox.kill failed name=%s", name, exc_info=True)


async def run_python(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Run Python *code* in an isolated microVM.

    Returns ``(exit_code, stdout)``. Raises ``ExecTimeoutError`` on timeout.
    """
    async with _ephemeral() as sb:
        logger.info("sandbox.start stdin=%s timeout=%ds", bool(stdin), timeout)
        await sb.fs.write("/code.py", code.encode())
        result = await sb.shell(
            "python3 /code.py",
            stdin=stdin.encode() if stdin else None,
            timeout=float(timeout),
        )
        logger.info(
            "sandbox.run exit_code=%d stdout=%r stderr=%r",
            result.exit_code,
            result.stdout_text,
            result.stderr_text,
        )
        return result.exit_code, result.stdout_text
