"""Shared microsandbox helper for solution plugins that execute untrusted code."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import partial
from uuid import uuid4

from microsandbox import Image, Network, RootDisk, Sandbox
from pydantic import ValidationError

logger = logging.getLogger(__name__)

PLUGIN_SLUG = "nexctf_sandbox"

DEFAULT_BASE_IMAGE = "python:3.12-slim"
DEFAULT_CPUS = 1
DEFAULT_MEMORY_MIB = 256
DEFAULT_ROOT_DISK_MIB = 64
DEFAULT_MAX_CONCURRENT = 8

# What the host's config resolver hands back for any one key.
type ConfigValue = str | int | float | bool

# The admin-tunable settings, and what to fall back to when the host's config
# is out of reach. Keys are bare — register_plugin_configs() prefixes them.
_DEFAULTS: dict[str, ConfigValue] = {
    "base_image": DEFAULT_BASE_IMAGE,
    "cpus": DEFAULT_CPUS,
    "memory_mib": DEFAULT_MEMORY_MIB,
    "root_disk_mib": DEFAULT_ROOT_DISK_MIB,
    "max_concurrent": DEFAULT_MAX_CONCURRENT,
}

_MAX_OUTPUT_BYTES = 64 * 1024
_RUN = (
    f"python3 /code.py >/out 2>/err; rc=$?; "
    f"head -c {_MAX_OUTPUT_BYTES} /out; head -c {_MAX_OUTPUT_BYTES} /err >&2; exit $rc"
)

MAX_PAYLOAD_CHARS = _MAX_OUTPUT_BYTES
MIN_TIMEOUT = 1  # 0 times out at 0ns, making every answer silently wrong
MAX_TIMEOUT = 30

# Built on first use, from max_concurrent — a Semaphore cannot be resized, so
# changing that setting only takes effect on restart.
_SLOTS: asyncio.Semaphore | None = None

# Latched once the host app turns out not to be importable (running outside it,
# e.g. under the plugin's own test suite). A failed import is not cached in
# sys.modules, so without this every boot would retry and re-pay for it.
_no_host = False


def _slots(limit: int) -> asyncio.Semaphore:
    """Return the process-wide VM slot semaphore, building it on first use."""
    global _SLOTS
    if _SLOTS is None:
        _SLOTS = asyncio.Semaphore(limit)
    return _SLOTS


async def _settings() -> dict[str, ConfigValue]:
    """Resolve the admin-set VM settings, falling back to ``_DEFAULTS``.

    Read fresh per submission rather than cached: one Redis ``hgetall`` against
    a ~1.1s VM boot is free, and it lets a changed setting apply immediately.
    Never raises — booting on defaults beats not booting at all.
    """
    global _no_host
    if _no_host:
        return dict(_DEFAULTS)
    try:
        from nexctf.core import appconfig
        from nexctf.core.cache import get_client
        from nexctf.plugins import get_plugin_config
    except ImportError, ValidationError:
        # No host app, or its settings cannot be built from the environment.
        # Deterministic, so latch. Expected when the plugin runs on its own
        # (its test suite), which is why this is debug and not a warning.
        logger.debug("sandbox.config host unavailable; using defaults", exc_info=True)
        _no_host = True
        return dict(_DEFAULTS)
    except Exception:
        # Anything else is not evidence the host is absent. Latching here would
        # pin every setting to its default until the next restart.
        logger.warning(
            "sandbox.config host import failed; using defaults", exc_info=True
        )
        return dict(_DEFAULTS)

    try:
        overrides = await appconfig.fetch_overrides(get_client())
    except Exception:
        # Transient — Redis down, say. Not latched, so the next boot retries.
        logger.warning("sandbox.config fetch failed; using defaults", exc_info=True)
        return dict(_DEFAULTS)

    resolved: dict[str, ConfigValue] = {}
    for key, fallback in _DEFAULTS.items():
        try:
            resolved[key] = get_plugin_config(key, overrides, plugin_slug=PLUGIN_SLUG)
        except Exception:
            # In practice a key we never registered. An override that will not
            # cast does not reach here: since 0.10 the host validates it, warns
            # once and hands back the code default itself.
            logger.warning(
                "sandbox.config bad key=%s; using default", key, exc_info=True
            )
            resolved[key] = fallback
    return resolved


@asynccontextmanager
async def _ephemeral(cfg: dict[str, ConfigValue]):
    root_disk_mib = int(cfg["root_disk_mib"])
    name = f"nexctf-{uuid4().hex}"
    sb = None
    try:
        sb = await Sandbox.create(
            name,
            # tmpfs root disk: guest writes are RAM-backed, so they cost no host disk.
            image=Image.oci(
                str(cfg["base_image"]), root_disk=RootDisk.tmpfs(root_disk_mib)
            ),
            cpus=int(cfg["cpus"]),
            # the tmpfs root disk is charged to guest memory
            memory=int(cfg["memory_mib"]) + root_disk_mib,
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
    """Yield ``run(code, stdin, *, timeout) -> (exit_code, stdout)`` on one microVM."""
    cfg = await _settings()
    async with _slots(int(cfg["max_concurrent"])), _ephemeral(cfg) as sb:
        yield partial(_exec, sb)


async def run_python(code: str, stdin: str = "", *, timeout: int) -> tuple[int, str]:
    """Run Python *code* in its own isolated microVM.

    Returns ``(exit_code, stdout)``. Raises ``ExecTimeoutError`` on timeout.
    """
    async with python_runner() as run:
        return await run(code, stdin, timeout=timeout)
