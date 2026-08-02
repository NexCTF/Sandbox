"""Shared microsandbox helper for solution plugins that execute untrusted code."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from microsandbox import Image, Network, RootDisk, Sandbox

logger = logging.getLogger(__name__)

_IMAGE = "python:3.12-slim"
_CPUS = 1
_ROOT_DISK_MIB = 64
_MEMORY_MIB = 256 + _ROOT_DISK_MIB  # the tmpfs root disk is charged to guest memory
_MAX_OUTPUT_BYTES = 64 * 1024

# The per-run timeout is the only budget: one verify() costs N x (~1.1s boot + timeout),
# so a runner question still tops out around MAX_TEST_CASES x MAX_TIMEOUT.
MIN_TIMEOUT = 1  # 0 times out at 0ns, making every answer silently wrong
MAX_TIMEOUT = 30

# Per-process, so the host ceiling is this x worker count.
_SLOTS = asyncio.Semaphore(8)

# Truncate in the guest: untrusted code outprints the timeout, and it lands in our RAM.
# Past _ROOT_DISK_MIB the guest OOM-kills instead (exit 137, no output) — long past the cap.
_RUN = (
    f"python3 /code.py >/out 2>/err; rc=$?; "
    f"head -c {_MAX_OUTPUT_BYTES} /out; head -c {_MAX_OUTPUT_BYTES} /err >&2; exit $rc"
)


@asynccontextmanager
async def _ephemeral():
    name = f"nexctf-{uuid4().hex}"
    sb = None
    try:
        sb = await Sandbox.create(
            name,
            # tmpfs root disk: guest writes are RAM-backed, so they cost no host disk.
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
            # kill() only stops the VM; without remove() it and its disk survive forever.
            await Sandbox.remove(name)
        except Exception:
            logger.warning("sandbox.remove failed name=%s", name, exc_info=True)


async def run_python(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Run Python *code* in an isolated microVM.

    Returns ``(exit_code, stdout)``. Raises ``ExecTimeoutError`` on timeout.
    """
    async with _SLOTS, _ephemeral() as sb:
        logger.info("sandbox.start stdin=%s timeout=%ds", bool(stdin), timeout)
        await sb.fs.write("/code.py", code.encode())
        result = await sb.shell(
            _RUN,
            stdin=stdin.encode() if stdin else None,
            timeout=float(timeout),
        )
        # Sizes only, at every level: a checker that raises prints the flag onto stderr.
        out = result.stdout_text
        logger.info(
            "sandbox.run exit_code=%d out=%dB err=%dB",
            result.exit_code,
            len(out),
            len(result.stderr_text),
        )
        return result.exit_code, out
