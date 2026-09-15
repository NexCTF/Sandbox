"""Tests for the admin-tunable microVM settings.

This suite runs outside the host app, so ``_settings`` cannot reach the host's
config and falls back to the defaults. That is not just an edge case here: it is
the path every other test in this repo implicitly boots on, so it is covered
first.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import nexctf_sandbox  # noqa: F401 — import for side effect: registers the config defs
from nexctf_sandbox import _sandbox


class _FakeFs:
    async def write(self, path: str, data: bytes) -> None:
        pass


class _FakeSandbox:
    def __init__(self) -> None:
        self.fs = _FakeFs()

    async def shell(self, cmd, *, stdin=None, timeout=None):
        return SimpleNamespace(exit_code=0, stdout_text="", stderr_text="")

    async def kill(self) -> None:
        pass


@pytest.fixture
def boot_kwargs(monkeypatch) -> dict:
    """Swap Sandbox.create for a fake and capture the kwargs the VM was booted with."""
    seen: dict = {}

    async def _fake_create(name, **kwargs):
        seen.update(kwargs)
        return _FakeSandbox()

    async def _fake_remove(name) -> None:
        pass

    monkeypatch.setattr(_sandbox.Sandbox, "create", _fake_create)
    monkeypatch.setattr(_sandbox.Sandbox, "remove", _fake_remove)
    return seen


def _settings_returning(**values):
    """Build a ``_settings`` stub returning the defaults with *values* overridden."""
    merged = dict(_sandbox._DEFAULTS) | values

    async def _fake() -> dict:
        return merged

    return _fake


async def test_defaults_are_used_when_the_host_config_is_unreachable(
    monkeypatch, boot_kwargs
) -> None:
    """Outside the host app there is no Redis and no config defs — boot anyway."""
    monkeypatch.setattr(_sandbox, "_no_host", False)

    await _sandbox.run_python("print('hi')", timeout=5)

    assert boot_kwargs["cpus"] == _sandbox.DEFAULT_CPUS
    assert boot_kwargs["memory"] == (
        _sandbox.DEFAULT_MEMORY_MIB + _sandbox.DEFAULT_ROOT_DISK_MIB
    )
    assert _sandbox._no_host is True  # latched, so the next boot does not retry


async def test_admin_settings_reach_the_vm(monkeypatch, boot_kwargs) -> None:
    monkeypatch.setattr(
        _sandbox,
        "_settings",
        _settings_returning(cpus=4, memory_mib=512, root_disk_mib=128),
    )

    await _sandbox.run_python("print('hi')", timeout=5)

    assert boot_kwargs["cpus"] == 4
    assert boot_kwargs["memory"] == 512 + 128  # guest RAM plus the tmpfs root disk


async def test_root_disk_is_never_dropped_from_the_memory_budget(
    monkeypatch, boot_kwargs
) -> None:
    """Regression guard: ``memory=`` is guest RAM *plus* the RAM-backed root disk.

    Passing ``memory_mib`` straight through would under-provision the VM and OOM
    it the moment an admin raises the root disk.
    """
    monkeypatch.setattr(
        _sandbox, "_settings", _settings_returning(memory_mib=256, root_disk_mib=512)
    )

    await _sandbox.run_python("print('hi')", timeout=5)

    assert boot_kwargs["memory"] == 256 + 512


async def test_base_image_is_applied(monkeypatch, boot_kwargs) -> None:
    monkeypatch.setattr(
        _sandbox, "_settings", _settings_returning(base_image="python:3.13-slim")
    )

    await _sandbox.run_python("print('hi')", timeout=5)

    assert "python:3.13-slim" in repr(boot_kwargs["image"])


def test_max_concurrent_sizes_the_semaphore(monkeypatch) -> None:
    """The semaphore is built once, from the setting, on first use."""
    monkeypatch.setattr(_sandbox, "_SLOTS", None)

    assert _sandbox._slots(3)._value == 3
    # Built once: a later, different limit does not resize it. This is why the
    # setting is documented as taking effect only after a restart.
    assert _sandbox._slots(9)._value == 3


async def test_a_redis_failure_is_not_latched(monkeypatch) -> None:
    """A transient outage must fall back without disabling settings for good."""
    monkeypatch.setattr(_sandbox, "_no_host", False)

    async def _boom(_redis) -> dict:
        raise ConnectionError("redis is down")

    _install_fake_host(monkeypatch, fetch_overrides=_boom)

    assert await _sandbox._settings() == dict(_sandbox._DEFAULTS)
    assert _sandbox._no_host is False


async def test_a_bad_key_falls_back_to_that_key_alone(monkeypatch) -> None:
    """One unusable key must not take the other settings down with it."""
    monkeypatch.setattr(_sandbox, "_no_host", False)

    def _get(key, overrides, *, plugin_slug=None):
        if key == "cpus":
            raise KeyError(key)  # never registered, or an override that will not cast
        return 999 if key == "memory_mib" else _sandbox._DEFAULTS[key]

    _install_fake_host(monkeypatch, get_plugin_config=_get)

    cfg = await _sandbox._settings()

    assert cfg["cpus"] == _sandbox.DEFAULT_CPUS
    assert cfg["memory_mib"] == 999  # the healthy keys still came through


def _install_fake_host(monkeypatch, *, fetch_overrides=None, get_plugin_config=None):
    """Stand in for the host modules ``_settings`` imports lazily.

    ``from nexctf.core import appconfig`` resolves as an attribute on the
    already-imported package, so that one has to be patched with setattr;
    the other two are plain sys.modules lookups.
    """
    import nexctf.core

    async def _empty(_redis) -> dict:
        return {}

    monkeypatch.setitem(
        sys.modules,
        "nexctf.core.cache",
        SimpleNamespace(get_client=lambda: object()),
    )
    monkeypatch.setattr(
        nexctf.core,
        "appconfig",
        SimpleNamespace(fetch_overrides=fetch_overrides or _empty),
    )
    monkeypatch.setitem(
        sys.modules,
        "nexctf.plugins",
        SimpleNamespace(
            get_plugin_config=get_plugin_config
            or (lambda key, o, *, plugin_slug=None: _sandbox._DEFAULTS[key])
        ),
    )


def test_every_setting_is_registered_with_the_host() -> None:
    """Each key in ``_DEFAULTS`` needs a ConfigDef, or it silently stays on its default."""
    from nexctf.core import appconfig

    for key in _sandbox._DEFAULTS:
        assert f"{_sandbox.PLUGIN_SLUG}.{key}" in appconfig._DEFS


def test_the_slug_matches_the_key_the_host_tracks_this_plugin_under() -> None:
    """A mismatch would file the settings under a category the admin UI never shows."""
    # Not part of the plugins package's public re-exports — the host calls it,
    # plugins do not. Imported from its module for the sake of this assertion.
    from nexctf.plugins.loader import plugin_key

    assert _sandbox.PLUGIN_SLUG == plugin_key("nexctf-sandbox")


def test_the_timeout_and_payload_limits_are_not_settings() -> None:
    """They are baked into DB CHECK constraints and Pydantic bounds at import time,
    so they must not become runtime settings — Postgres would reject the value."""
    assert not {"timeout", "max_timeout", "min_timeout", "max_output_bytes"} & set(
        _sandbox._DEFAULTS
    )


class _Hostile:
    """Stands in for nexctf.core, failing with something other than ImportError."""

    def __getattr__(self, name):
        raise RuntimeError("something unexpected")


async def test_an_unexpected_import_failure_is_not_latched(monkeypatch) -> None:
    """Only a missing or unbuildable host latches. Anything else stays retryable,
    or one odd hiccup pins every setting to its default until the next restart."""
    monkeypatch.setattr(_sandbox, "_no_host", False)
    monkeypatch.setitem(sys.modules, "nexctf.core", _Hostile())

    assert await _sandbox._settings() == dict(_sandbox._DEFAULTS)
    assert _sandbox._no_host is False


async def test_a_missing_host_is_latched(monkeypatch) -> None:
    """The case the latch exists for: no host app, so stop re-paying for the import."""
    monkeypatch.setattr(_sandbox, "_no_host", False)
    monkeypatch.setitem(sys.modules, "nexctf.core", None)  # forces ImportError

    assert await _sandbox._settings() == dict(_sandbox._DEFAULTS)
    assert _sandbox._no_host is True


def test_base_image_is_registered_as_a_plain_string() -> None:
    """A str default cannot be inferred, so the ConfigDef must say so explicitly."""
    from nexctf.core import appconfig
    from nexctf.plugins import ConfigType

    assert (
        appconfig._DEFS[f"{_sandbox.PLUGIN_SLUG}.base_image"].type is ConfigType.STRING
    )
