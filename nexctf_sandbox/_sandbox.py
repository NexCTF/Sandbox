"""Shared microsandbox helper for solution plugins that execute untrusted code."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from microsandbox import Image, Network, RootDisk, Sandbox

logger = logging.getLogger(__name__)

_IMAGE = "python:3.12-slim"
_CPUS = 1
_ROOT_DISK_MIB = 64
_MEMORY_MIB = 256 + _ROOT_DISK_MIB  # the tmpfs root disk is charged to guest memory


@asynccontextmanager
async def _ephemeral():
    name = f"nexctf-{uuid4().hex}"
    sb = None
    try:
        sb = await Sandbox.create(
            name,
            # tmpfs root disk: guest writes are RAM-backed and capped, so code that
            # fills the disk costs the host nothing and dies with the VM.
            image=Image.oci(_IMAGE, root_disk=RootDisk.tmpfs(_ROOT_DISK_MIB)),
            cpus=_CPUS,
            memory=_MEMORY_MIB,
            network=Network.none(),
        )
        yield sb
    finally:
        if sb is not None:
            try:
                await sb.kill()
            except Exception:
                logger.warning("sandbox.kill failed name=%s", name, exc_info=True)
        try:
            # kill() only stops the VM; without remove() the registration and its
            # disk survive every submission, forever.
            await Sandbox.remove(name)
        except Exception:
            logger.warning("sandbox.remove failed name=%s", name, exc_info=True)


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
