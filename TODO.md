# TODO

Findings from the 2026-08-02 speed/security review. Numbers were measured on a
real host with live microVMs, not estimated.

## Fixed

One commit each on `hardening`, verified against live microVMs (see foot of file).

- [x] **1. Sandbox leak → host disk exhaustion** (critical) — `nexctf_sandbox/_sandbox.py:30-34`
  `kill()` stops the VM but never deregisters it or deletes its root disk, and
  `Sandbox.remove()` is never called. A player whose code fills the guest's 4 GiB
  root disk leaks 3.9 GB of host disk *per submission*, permanently; at 10
  submissions/min/user that is ~39 GB/min from one account. Benign traffic still
  leaks a record + ~400 KB every submission.
  Fix: `await Sandbox.remove(name)` after `kill()`, plus
  `Image.oci(_IMAGE, root_disk=RootDisk.tmpfs(64))` so guest writes are RAM-backed
  and cost no host disk at all.

- [x] **2. Unbounded stdout buffered in the API process** (high) — `nexctf_sandbox/_sandbox.py:45-56`
  Nothing caps output. Measured: a guest asked for 256 MiB of stdout returned all
  268435456 bytes and took host RSS from 118 MB to 892 MB — then the INFO log wrote
  a second copy to disk. This runs inside the FastAPI process, so it OOMs the
  backend, not a worker.
  Fix: cap in the shell command itself (`head -c 65536`), exit code preserved.

- [x] **3. No bound on `timeout` or test-case count** (high) — `nexctf_sandbox/solutions/runner.py:40-55`, `nexctf_sandbox/solutions/script.py:52-70`
  Total `verify()` cost is `N × (1.1 s boot + timeout)` with no total budget. 50
  cases × `timeout=30` is a 26-minute HTTP request. Worse: NexCTF re-verifies every
  stored submission for a question under an exclusive lock on `submissions`, and
  players control how many submissions exist — 500 × 5 cases × 5 s ≈ 4 hours of
  frozen scoring.
  Fix: `ge=1, le=30` on `timeout` and `max_length=20` on `test_cases` in all four
  Create/Update schemas, plus DB `CheckConstraint`s (folded into migration `0001`,
  the plugin being unpublished) so fixtures cannot bypass Pydantic. Also closes `timeout=0`, which currently makes every answer silently
  wrong with no admin-visible error.

- [x] **4. No concurrency cap on microVM creation** (high) — `nexctf_sandbox/_sandbox.py:19-27`
  Each sandbox is 1 vCPU + 256 MiB and nothing limits how many exist at once. The
  rate limit is per user, so 100 concurrent submitters demand 100 vCPUs and 25 GiB.
  Fix: one module-level semaphore in `_sandbox.py` — the single chokepoint both
  solution types route through.

- [x] **5. Flags, answers and PII logged at INFO** (medium) — `nexctf_sandbox/_sandbox.py:50-55`
  `stdout`/`stderr` are logged verbatim on every run. For `script` solutions the
  checker source is where the flag lives, and any checker exception puts the
  offending source line into a traceback on stderr. Log shipping has a far wider
  audience than the flag does. Formatting multi-MB payloads through a synchronous
  handler also stalls the event loop for every other request on the worker.
  Fix: log lengths only, at every level — demoting to DEBUG was a severity knob
  on a secrecy problem.
  (`%r` already escapes newlines — log forging is *not* possible, do not add escaping.)

- [x] **6. Timeouts are scored as wrong answers** — `run_code` in `runner.py`, `run_checker`
  in `script.py`
  The platform has a first-class `SolutionTimeoutError` and emits `solution.timeout`
  admin events; this plugin swallowed both into `False`, so a timeout was
  indistinguishable from a wrong answer and a player driving the box into timeouts was
  invisible.
  Fix: `run_code`/`run_checker` re-raise `ExecTimeoutError`; `verify()` converts it to
  `SolutionTimeoutError(self.id)`, which is where the solution id lives. Everything
  else is still swallowed into `False`.
  Scope: this buys admin visibility, not a different verdict — both call sites
  (`api/routes/challenge.py`, `module/submission.py`) still leave `is_correct` False
  and count the malus. Changing that is the platform's call.

- [x] **7. One microVM per test case, sequentially** (speed, ~9x) — `RunnerSolution.verify`
  Fix: `_sandbox.python_runner()`, a context manager yielding a `run()` bound to one
  booted VM; `verify()` opens it once per submission. Re-measured after the change:
  **10 cases 8.78s → 1.03s (8.5x)**, 3 cases 2.62s → 0.92s.
  The semaphore slot is now held for the whole block instead of per case — fewer total
  VM-seconds, and a submission that has a slot finishes on it.
  Behavioral: cases within one submission share guest state. No cross-player reach and
  no way to learn `expected_output`, so not a cheat vector.

- [x] **8. No size bound on anything crossing the boundary** — `ScriptSolutionCreate.checker_code`,
  `TestCase` in `runner.py`
  `CodeStr` carries a UI hint, not a length, so a 50 MB checker was formatted, encoded
  and written into the guest on every submission — and stored rows are re-verified in a
  loop under an exclusive lock, so the cost is paid per row.
  Fix: one cap, `MAX_PAYLOAD_CHARS` = 64 KiB, on `checker_code`, `TestCase.input` and
  `expected_output`. It is the output cap by construction: stdout never returns longer
  than `_MAX_OUTPUT_BYTES`, so a longer `expected_output` could never match. DB
  `CheckConstraint`s alongside for the same reason as 3 — `length(checker_code)` exactly,
  and one ceiling on the whole `test_cases` blob, because a `CHECK` cannot walk a jsonb
  array (no subqueries). Both verified against postgres:17.

## Next

- [ ] **10. `Network.none()` is not enforced — untrusted code has full egress** (critical,
  operational) — `_ephemeral` in `_sandbox.py`
  Measured on this host: the guest opened a TCP connection to `1.1.1.1:53`, to private
  `10.0.0.1` and to the host subnet. `Network.none()`, `Network.allow_all()`, an
  explicit `default_egress=DENY` policy with a `deny@any` rule, and the CLI's own
  `--no-net` all behave identically — the policy is accepted and silently not applied.
  Cause: microsandbox programs firewall rules on the host, and this host has
  `CapEff=0` (no `CAP_NET_ADMIN`) with no `nft`/`iptables` binary. `msb doctor` still
  reports "Host setup is ready" — it checks KVM, not network enforcement. It **fails
  open**, so nobody finds out.
  Impact if the deploy host is likewise unprivileged: players can exfiltrate flags,
  reach the platform's own Postgres/Redis/backend (SSRF from inside the sandbox), and
  attack third parties from the CTF's IP.
  Not fixable in this plugin — it is a host capability, and CI cannot check it either:
  `test_network_is_denied` skips on GitHub runners too (no `CAP_NET_ADMIN`), so the
  other seven live tests passing there says nothing about egress. Still true on
  microsandbox 0.6.14 — 0.6.9's network work is DNS failover and TCP half-close, not
  enforcement. **Run `pytest -m live`
  on the real sandbox host before an event** and give that host its own egress
  firewalling.

- [ ] **9. Mutable image tag, pulled inside the request** — `_IMAGE` in `_sandbox.py`
  `python:3.12-slim` is unpinned with an `if-missing` pull policy, so the first
  submission after a deploy pays a ~50 MB pull inside the HTTP request. Player code
  also runs on 3.12 while the platform now targets 3.14. Pin by digest, warm the
  image at startup, and move to 3.14 deliberately — it changes what code passes.
  Left for the operator on purpose: the digest pin is two characters of work, but it
  freezes base-image security updates until someone bumps it, and 3.12 → 3.14 decides
  which player submissions still pass. Neither is the plugin's call to make quietly.

## Accepted, documented

- **Timing oracle in `verify()`** — early-exit on the first failing test case leaks
  how many leading cases a submission passes, at ~1.1 s resolution, so the hidden
  test suite can be probed. The obvious fix (always run every case) directly
  multiplies findings 3 and 4. Not worth it.

## Reviewed and deliberately left alone

- **Check constraint SQL duplicated between the models and `0001`.** A migration is a
  frozen snapshot and must keep describing `1..30` after the constants move; importing
  live constants into it is the anti-pattern, not the duplication.
- **`_SLOTS` as a module global rather than a plugin setting.** `asyncio.Semaphore` has
  no resize, so an admin editing it in a settings UI would see nothing until restart —
  surfacing a control that silently does not apply is worse than the constant. The
  `ponytail:` marker names the upgrade path.
- **`ge=/le=` repeated across four schema fields.** An `Annotated` alias would trade
  eight explicit half-lines for one line plus indirection that merges invisibly with
  each field's own `Field(...)`.
- **A `[tool.ruff]` block replacing the two `# noqa: RUF012`.** Three new lines to
  delete two.

## Do not build

Verified as already handled by microsandbox — adding defenses here is pure complexity:

- **Fork bombs and memory exhaustion are contained.** Host RSS stayed flat under
  `while True: os.fork()`; guest `MemTotal` is a hard 265 MiB. No `Rlimit` needed.
- **No credentials reach the guest** — no `env=` is passed, and the local backend
  has no API key to steal.
- **`_WRAPPER.format()` is not an injection vector.** `str.format` does not re-scan
  substituted text, so braces in checker code are inert; the player's answer travels
  as JSON data and is parsed at runtime, never as code.
- **The per-exec timeout is real.** Fires at T and propagates through the `finally`.
- **No warm VM pool, snapshots, concurrent-VM-per-case, fire-and-forget teardown, or
  HTTP session pooling.** The last one has nothing to pool — the client is a native
  extension over a local unix socket.

## Live verification (2026-08-02, re-run 2026-08-23 on microsandbox 0.6.14)

Now encoded as `tests/test_live.py` (`pytest -m live`, auto-skipped without
`/dev/kvm`); the fast suite is `pytest -m "not live"`. On 0.6.14: 7 passed, 1
skipped (`test_network_is_denied`, per item 10). Findings when written:

- exit code and both streams survive the guest-side buffering (`rc=3`, stdout
  `hello`, stderr captured); stdin still round-trips.
- 5 MB of stdout comes back as exactly 65536 bytes with `rc=0` preserved — the cap
  fires for realistic output.
- 200 MB of stdout: `rc=137`, no output, host memory flat. The buffers live on the
  tmpfs root disk, so a program printing more than `_ROOT_DISK_MIB` is OOM-killed
  rather than truncated — far past the 64 KiB cap either way.
- 300 MB written to `/big`: `rc=137`, **zero host disk consumed** (was 3.9 GB leaked
  per submission before).
- `Sandbox.list()` delta after four runs: **0**. One `kill()` genuinely failed
  (`IoError: No child processes`) and `remove()` still reclaimed the sandbox — which
  is exactly the case the old DEBUG-level log would have hidden.
