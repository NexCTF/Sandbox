"""Integration tests that boot a real microVM."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from microsandbox import ExecTimeoutError, Sandbox

from nexctf_sandbox import _sandbox
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


@pytest.fixture(autouse=True)
def _ask_for_what_this_host_can_deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a host without CAP_NET_ADMIN, run the suite under 'all'.

    ``_check_enforceable`` refuses the isolating settings there, failing every
    test here for a reason none of them is about. Not a workaround: on a host
    that cannot filter, unfiltered egress is exactly what the guest gets.
    """
    if not _host_can_filter():
        monkeypatch.setattr(
            _sandbox, "_settings", _settings_with(network_access=_sandbox.NETWORK_ALL)
        )


_REACH = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection((%r, %d), timeout=5)\n"
    "    print('REACHED')\n"
    "except OSError:\n"
    "    print('denied')\n"
)


def _settings_with(**values):
    """A ``_settings`` stub returning the defaults with *values* overridden."""
    merged = dict(_sandbox._DEFAULTS) | values

    async def _fake() -> dict:
        return merged

    return _fake


def _require_host_internet() -> None:
    """Skip unless the host itself has egress to give; the allow path asserts REACHED.

    Called from inside the test, never from a ``skipif``: decorator arguments
    evaluate at import, and pytest imports this module even when it deselects
    every test in it — opening a socket on every run of the fast suite.
    """
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    except OSError:
        pytest.skip(
            "host itself has no outbound network, so the guest cannot reach "
            "anything no matter what the policy allows"
        )


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
    """With ``network_access`` on its default the guest gets Network.none(), which
    fails open silently: on a host that cannot program firewall rules the policy is
    simply not applied — which is why _check_enforceable refuses to boot there."""
    exit_code, stdout = await run_python(_REACH % ("1.1.1.1", 53), timeout=20)

    assert (exit_code, stdout.strip()) == (0, "denied")


@pytest.mark.skipif(
    not _host_can_filter(),
    reason="host has no CAP_NET_ADMIN, so _check_enforceable refuses 'internet' here",
)
async def test_internet_access_opens_the_public_internet(monkeypatch) -> None:
    """The other half of the egress story: under 'internet' a public address is
    reachable. Without this, a policy that silently denied everything would look
    exactly like the default and no test would notice."""
    _require_host_internet()
    monkeypatch.setattr(
        _sandbox, "_settings", _settings_with(network_access=_sandbox.NETWORK_INTERNET)
    )

    exit_code, stdout = await run_python(_REACH % ("1.1.1.1", 53), timeout=20)

    assert (exit_code, stdout.strip()) == (0, "REACHED")


async def test_one_vm_serves_several_runs() -> None:
    """RunnerSolution.verify runs all its test cases on a single VM, so the exec path
    has to survive a second call — and the guest state the first run leaves is shared."""
    async with python_runner() as run:
        assert await run("open('/marker', 'w').write('1')", timeout=20) == (0, "")
        assert await run("print(open('/marker').read())", timeout=20) == (0, "1\n")


async def test_timeout_raises_rather_than_hanging() -> None:
    with pytest.raises(ExecTimeoutError):
        await run_python("import time; time.sleep(30)", timeout=5)
