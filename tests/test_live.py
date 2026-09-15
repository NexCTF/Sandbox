"""Integration tests that boot a real microVM.

The rest of the suite fakes the sandbox, so nothing there exercises the parts
that live outside Python: the ``_RUN`` shell string, exit-code fidelity through
it, ``Network.none()``, and whether teardown actually reclaims the sandbox.
Those were verified by hand while hardening this plugin; this file keeps them.

Skipped without a usable ``/dev/kvm``. Each test boots a VM (~1s), so run them
with ``pytest -m live`` and the fast suite with ``pytest -m "not live"``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from microsandbox import ExecTimeoutError, Sandbox

from nexctf_sandbox._sandbox import _MAX_OUTPUT_BYTES, python_runner, run_python

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.access("/dev/kvm", os.R_OK | os.W_OK),
        reason="needs a usable /dev/kvm",
    ),
]

_SANDBOX_DIR = Path.home() / ".microsandbox" / "sandboxes"


def _sandbox_dir_bytes() -> int:
    return sum(p.stat().st_size for p in _SANDBOX_DIR.rglob("*") if p.is_file())


async def _nexctf_sandboxes() -> list:
    return [s for s in (await Sandbox.list()).sandboxes if "nexctf-" in str(s)]


async def test_exit_code_and_stdout_survive_the_wrapper() -> None:
    """_RUN redirects both streams to files and reads them back, so it has to return
    the program's own exit code rather than head's."""
    assert await run_python("print('hi')", timeout=20) == (0, "hi\n")
    assert await run_python("import sys; sys.exit(7)", timeout=20) == (7, "")


async def test_stdin_reaches_the_program() -> None:
    exit_code, stdout = await run_python(
        "import sys; print(sys.stdin.read().upper())", "abc", timeout=20
    )
    assert (exit_code, stdout) == (0, "ABC\n")


async def test_output_is_truncated_not_returned_whole() -> None:
    """Unbounded output landed in the API process: 256 MiB of stdout took host RSS
    from 118 MB to 892 MB before this was capped."""
    exit_code, stdout = await run_python("print('x' * 5_000_000)", timeout=20)

    assert exit_code == 0
    assert len(stdout) == _MAX_OUTPUT_BYTES


async def test_guest_writes_cost_no_host_disk() -> None:
    """A player filling the guest disk used to leak it onto the host, permanently:
    3.9 GB per submission, at 10 submissions/min/user."""
    before = _sandbox_dir_bytes()

    await run_python("open('/big', 'wb').write(b'x' * (300 * 1024 * 1024))", timeout=30)

    assert _sandbox_dir_bytes() - before < 10 * 1024 * 1024


async def test_sandbox_is_deregistered_after_the_run() -> None:
    """kill() only stops the VM; without remove() the record survives every run."""
    await run_python("print('hi')", timeout=20)

    assert await _nexctf_sandboxes() == []


def _host_can_filter() -> bool:
    """CAP_NET_ADMIN, without which microsandbox cannot program any firewall rules."""
    status = Path("/proc/self/status").read_text()
    caps = int(status.split("CapEff:")[1].split()[0], 16)
    return bool(caps >> 12 & 1)


@pytest.mark.skipif(
    not _host_can_filter(),
    reason="host has no CAP_NET_ADMIN, so sandbox egress is UNFILTERED here — "
    "microsandbox reports default_egress=DENY and lets the guest reach anything",
)
async def test_network_is_denied() -> None:
    """Network.none() is the plugin's whole egress story, and it fails open silently:
    on a host that cannot program firewall rules the policy is simply not applied."""
    exit_code, stdout = await run_python(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
        "    print('REACHED')\n"
        "except OSError:\n"
        "    print('denied')\n",
        timeout=20,
    )

    assert (exit_code, stdout.strip()) == (0, "denied")


async def test_one_vm_serves_several_runs() -> None:
    """RunnerSolution.verify runs all its test cases on a single VM, so the exec path
    has to survive a second call — and the guest state the first run leaves is shared."""
    async with python_runner() as run:
        assert await run("open('/marker', 'w').write('1')", timeout=20) == (0, "")
        assert await run("print(open('/marker').read())", timeout=20) == (0, "1\n")


async def test_timeout_raises_rather_than_hanging() -> None:
    with pytest.raises(ExecTimeoutError):
        await run_python("import time; time.sleep(30)", timeout=5)
