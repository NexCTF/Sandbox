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
_LOG_PREVIEW_CHARS = 1024

# One verify() costs N x (~1.1s boot + timeout) and nothing bounds the total, so the
# per-run timeout is the only budget there is. Submission re-verification runs under
# an exclusive lock on the submissions table, where an unbounded timeout stalls
# scoring platform-wide.
MIN_TIMEOUT = 1  # 0 makes every answer silently wrong: exec times out at 0ns
MAX_TIMEOUT = 30

# Rate limiting is per user, so concurrent submitters each demand a vCPU and
# _MEMORY_MIB from the host with nothing in between. Queue here instead: waiting a
# beat for a slot beats every sandbox on the box thrashing.
# ponytail: per-process, so the host ceiling is this x worker count; make it a
# setting when that math stops working.
_SLOTS = asyncio.Semaphore(8)

# Buffer output in the guest and hand back only the first _MAX_OUTPUT_BYTES of each
# stream: untrusted code can print faster than the timeout ends, and whatever it
# prints is held in the API process's memory. The exit code is the program's own.
# Ceiling: the buffers live on the tmpfs root disk, so a program printing more than
# _ROOT_DISK_MIB is OOM-killed (exit 137, no output) rather than truncated. That is
# far past the cap either way; pipe through head if partial output ever matters.
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
    async with _SLOTS, _ephemeral() as sb:
        logger.info("sandbox.start stdin=%s timeout=%ds", bool(stdin), timeout)
        await sb.fs.write("/code.py", code.encode())
        result = await sb.shell(
            _RUN,
            stdin=stdin.encode() if stdin else None,
            timeout=float(timeout),
        )
        # Payloads stay at debug: for script solutions the checker source is where the
        # flag lives, and a checker that raises puts the offending source line into a
        # traceback on stderr. Logs travel further than the flag should.
        logger.info(
            "sandbox.run exit_code=%d out=%dB err=%dB",
            result.exit_code,
            len(result.stdout_text),
            len(result.stderr_text),
        )
        logger.debug(
            "sandbox.io stdout=%r stderr=%r",
            result.stdout_text[:_LOG_PREVIEW_CHARS],
            result.stderr_text[:_LOG_PREVIEW_CHARS],
        )
        return result.exit_code, result.stdout_text
