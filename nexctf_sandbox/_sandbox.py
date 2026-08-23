"""Shared microsandbox helper for solution plugins that execute untrusted code."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import partial
from uuid import uuid4

from microsandbox import Image, Network, RootDisk, Sandbox

logger = logging.getLogger(__name__)

_IMAGE = "python:3.12-slim"
_CPUS = 1
_ROOT_DISK_MIB = 64
_MEMORY_MIB = 256 + _ROOT_DISK_MIB  # the tmpfs root disk is charged to guest memory
_MAX_OUTPUT_BYTES = 64 * 1024
# Admin-authored text shipped into the VM on every single submission. Same size as
# the output cap, which for expected_output is exactly where it stops being useful:
# stdout never comes back longer, so a longer expectation can never match.
MAX_PAYLOAD_CHARS = _MAX_OUTPUT_BYTES

# The per-run timeout is the only budget: one verify() costs ~1.1s boot + N x timeout
# (one boot, not N, since the cases share a VM), so a runner question still tops out
# around MAX_TEST_CASES x MAX_TIMEOUT.
MIN_TIMEOUT = 1  # 0 times out at 0ns, making every answer silently wrong
MAX_TIMEOUT = 30

# Per-process, so the host ceiling is this x worker count.
# ponytail: fixed at import, not a plugin setting — asyncio.Semaphore has no resize,
# so a settings UI would silently not apply until restart. Make it one if that lands.
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


async def _exec(sb, code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Run *code* in an already-booted microVM. Raises ``ExecTimeoutError`` on timeout."""
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


@asynccontextmanager
async def python_runner():
    """Yield ``run(code, stdin, *, timeout) -> (exit_code, stdout)`` on one microVM.

    Boot is ~1.1s and dominates a short run, so a caller with several runs to make
    should reuse one VM: 10 runs measured 8.78s fresh-VM-each vs 1.03s shared.
    Runs share guest state, so only ever reuse within a single submission.

    A slot is held for the whole block rather than per run — the total VM-seconds
    are lower either way, and a submission that gets a slot now finishes on it.
    ponytail: that makes worst-case occupancy a whole submission (MAX_TEST_CASES x
    MAX_TIMEOUT) rather than one run; split the slot per run if fairness bites.
    """
    async with _SLOTS, _ephemeral() as sb:
        yield partial(_exec, sb)


async def run_python(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Run Python *code* in its own isolated microVM.

    Returns ``(exit_code, stdout)``. Raises ``ExecTimeoutError`` on timeout.
    """
    async with python_runner() as run:
        return await run(code, stdin, timeout=timeout)
