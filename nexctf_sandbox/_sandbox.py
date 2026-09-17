"""Shared microsandbox helper for solution plugins that execute untrusted code."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from uuid import uuid4

from microsandbox import (
    BackendKind,
    Image,
    Network,
    NetworkProfile,
    RootDisk,
    Sandbox,
    default_backend_kind,
)
from pydantic import ValidationError

logger = logging.getLogger(__name__)

PLUGIN_SLUG = "nexctf_sandbox"

DEFAULT_BASE_IMAGE = "python:3.12-slim"
DEFAULT_CPUS = 1
DEFAULT_MEMORY_MIB = 256
DEFAULT_ROOT_DISK_MIB = 64
DEFAULT_MAX_CONCURRENT = 8

NETWORK_DISABLED = "disabled"  # no egress at all
NETWORK_INTERNET = "internet"  # public addresses and DNS, but nothing host-side
NETWORK_ALL = "all"  # unfiltered, including the host's own network
NETWORK_ACCESS_CHOICES = [NETWORK_DISABLED, NETWORK_INTERNET, NETWORK_ALL]
DEFAULT_NETWORK_ACCESS = NETWORK_DISABLED

type ConfigValue = str | int | float | bool

_DEFAULTS: dict[str, ConfigValue] = {
    "base_image": DEFAULT_BASE_IMAGE,
    "cpus": DEFAULT_CPUS,
    "memory_mib": DEFAULT_MEMORY_MIB,
    "root_disk_mib": DEFAULT_ROOT_DISK_MIB,
    "max_concurrent": DEFAULT_MAX_CONCURRENT,
    "network_access": DEFAULT_NETWORK_ACCESS,
}

_CAP_NET_ADMIN = 12
_MAX_OUTPUT_BYTES = 64 * 1024
_RUN = (
    f"python3 /code.py >/out 2>/err; rc=$?; "
    f"head -c {_MAX_OUTPUT_BYTES} /out; head -c {_MAX_OUTPUT_BYTES} /err >&2; exit $rc"
)
_SLOTS: asyncio.Semaphore | None = None

MAX_PAYLOAD_CHARS = _MAX_OUTPUT_BYTES
MIN_TIMEOUT = 1
MAX_TIMEOUT = 30


_no_host = False


def _slots(limit: int) -> asyncio.Semaphore:
    """Return the process-wide VM slot semaphore, building it on first use."""
    global _SLOTS
    if _SLOTS is None:
        _SLOTS = asyncio.Semaphore(limit)
    return _SLOTS


async def _settings() -> dict[str, ConfigValue]:
    """Resolve the admin-set VM settings, falling back to ``_DEFAULTS``."""
    global _no_host
    if _no_host:
        return dict(_DEFAULTS)
    try:
        from nexctf.core import appconfig
        from nexctf.core.cache import get_client
        from nexctf.plugins import get_plugin_config
    except ImportError, ValidationError:
        logger.debug("sandbox.config host unavailable; using defaults", exc_info=True)
        _no_host = True
        return dict(_DEFAULTS)
    except Exception:
        logger.warning(
            "sandbox.config host import failed; using defaults", exc_info=True
        )
        return dict(_DEFAULTS)

    try:
        overrides = await appconfig.fetch_overrides(get_client())
    except Exception:
        logger.warning("sandbox.config fetch failed; using defaults", exc_info=True)
        return dict(_DEFAULTS)

    resolved: dict[str, ConfigValue] = {}
    for key, fallback in _DEFAULTS.items():
        try:
            resolved[key] = get_plugin_config(key, overrides, plugin_slug=PLUGIN_SLUG)
        except Exception:
            logger.warning(
                "sandbox.config bad key=%s; using default", key, exc_info=True
            )
            resolved[key] = fallback
    return resolved


def _can_enforce_network() -> bool:
    """Whether a network policy set on a microVM is actually programmed.

    Without CAP_NET_ADMIN microsandbox reports the policy as applied and leaves
    egress unfiltered.
    """
    try:
        if default_backend_kind() != BackendKind.LOCAL:
            return True
        status = Path("/proc/self/status").read_text()
        caps = int(status.split("CapEff:")[1].split()[0], 16)
    except Exception:
        logger.warning(
            "sandbox.network capability probe failed; assuming policy is enforced",
            exc_info=True,
        )
        return True
    return bool(caps >> _CAP_NET_ADMIN & 1)


def _network(access: ConfigValue) -> Network:
    """Build the guest's egress policy from the ``network_access`` setting."""
    if access == NETWORK_INTERNET:
        return Network.from_profiles(NetworkProfile.PUBLIC)
    if access == NETWORK_ALL:
        return Network.allow_all()
    if access != NETWORK_DISABLED:
        logger.warning("sandbox.network unknown access=%r; denying egress", access)
    return Network.none()


def _check_enforceable(access: ConfigValue, network: Network) -> None:
    """Refuse to boot when *network* would silently not be applied."""
    if network != Network.allow_all() and not _can_enforce_network():
        raise RuntimeError(
            f"network_access={access!r} cannot be enforced: this process has no "
            "CAP_NET_ADMIN, so microsandbox would leave guest egress unfiltered. "
            "Grant the capability, or set network_access="
            f"{NETWORK_ALL!r} to accept unfiltered egress."
        )


if not _can_enforce_network():
    logger.warning(
        "sandbox.network host has no CAP_NET_ADMIN: microVM egress cannot be "
        "filtered, so network_access=%r and %r are refused and only %r will boot",
        NETWORK_DISABLED,
        NETWORK_INTERNET,
        NETWORK_ALL,
    )


@asynccontextmanager
async def _ephemeral(cfg: dict[str, ConfigValue]):
    access = cfg["network_access"]
    network = _network(access)
    _check_enforceable(access, network)
    root_disk_mib = int(cfg["root_disk_mib"])
    name = f"nexctf-{uuid4().hex}"
    sb = None
    try:
        sb = await Sandbox.create(
            name,
            image=Image.oci(
                str(cfg["base_image"]), root_disk=RootDisk.tmpfs(root_disk_mib)
            ),
            cpus=int(cfg["cpus"]),
            memory=int(cfg["memory_mib"]) + root_disk_mib,
            network=network,
        )
        yield sb
    finally:
        if sb is not None:
            try:
                await sb.kill()
            except Exception:
                logger.warning("sandbox.kill failed name=%s", name, exc_info=True)
        try:
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
