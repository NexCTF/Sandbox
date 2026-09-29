# NexCTF Sandbox

A [NexCTF](https://github.com/NexCTF/NexCTF) plugin that checks answers by running Python: a code runner that tests players' programs against test cases, and a script checker that validates answers with a function you write. Every run happens in a throwaway [microsandbox](https://github.com/microsandbox/microsandbox) microVM, booted for the submission and removed right after.

[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)

## Quick Start

Add the plugin to NexCTF's `NEXCTF_PLUGINS` and restart it; NexCTF installs it and applies its migrations:

```bash
NEXCTF_PLUGINS=git+https://github.com/NexCTF/Sandbox.git
```

The NexCTF process that checks submissions boots the microVMs, so it needs read and write access to `/dev/kvm`. Nothing else: the network setting is enforced by microsandbox's own network stack, with no extra capability or root.

With NexCTF's `compose.yml`, that goes on the `app` service, e.g. in a `compose.override.yml`. `/dev/kvm` belongs to the host's `kvm` group, which the image's unprivileged user has to join; get its id with `stat -c %g /dev/kvm`:

```yaml
services:
  app:
    devices:
      - /dev/kvm
    group_add:
      - "993"  # the kvm group id on the host
    environment:
      NEXCTF_PLUGINS: git+https://github.com/NexCTF/Sandbox.git
```

Then, on a question, pick the solution type **runner** or **script**.

## Features

### 🏃 Code runner
- Runs the player's Python 3 code against up to 20 test cases, feeding each one's input on stdin
- Accepted only when every case's stdout matches the expected output, whitespace stripped
- A crash or a non-zero exit fails the case
- All the cases of a submission share one microVM, run one after the other
- For questions with a **code** input

### ✅ Script checker
- Validates the answer with your own Python function, `check(answer, team_id, team_fields) -> bool`
- Hands the checker the team's custom fields, typed, private ones included: per-team flags and seeds work out of the box
- The starter checker lists the team custom fields that exist
- Checkers written without `team_fields` keep working as they are
- For questions with an **input**, **text** or **code** input

## Writing a checker

The checker is a function named `check` that returns `True` to accept the answer. `team_id` is the team's id as a string, or `None`; `team_fields` maps each team custom field name to its value (`int`, `bool` or `str`), and a key is missing when the team left the field empty. Declare `team_fields` only if you need it:

```python
import hashlib


def check(answer: str, team_id: str | None, team_fields: dict) -> bool:
    seed = team_fields.get("seed", "")
    flag = "FLAG{" + hashlib.sha256(seed.encode()).hexdigest()[:16] + "}"
    return answer.strip() == flag
```

The checker runs in its own microVM, with the base image's Python and the network access set below.

## Settings

Under **Settings → Plugins → Sandbox**:

| Setting | Default |
|---|---|
| Base image | `python:3.12-slim`; any OCI image with `python3` on `PATH` |
| vCPUs | 1 |
| Memory | 256 MiB, plus the root disk |
| Root disk | 64 MiB, in RAM |
| Network access | `disabled`; `internet` for public addresses and the host's DNS, `all` for unfiltered egress, the host's own network included |
| Max concurrent microVMs | 8, across all challenges; takes effect after a restart |

## Development

The tests stub out the microVMs and need no database. NexCTF is a dev dependency installed from git (currently its `v0.11.0` tag).

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run ty check .
```

The `live` tests boot real microVMs and skip without `/dev/kvm`; the one reaching the internet also skips when the host has none. Run them on the host that will check submissions before an event:

```bash
uv run pytest -m live
```

To load a local checkout in a dev NexCTF, add its path to `NEXCTF_PLUGINS` in NexCTF's `.env.dev.override`, then restart `task dev:backend`.
