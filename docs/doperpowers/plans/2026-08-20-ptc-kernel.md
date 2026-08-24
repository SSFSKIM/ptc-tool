# PTC Kernel Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use doperpowers:subagent-driven-execution to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `ptc` — a Python package + Claude Code plugin that gives each Claude Code session a detached persistent IPython kernel via an MCP server (`exec`/`wait` yield protocol) and CLI, with `read/write/edit/bash/agent/llm/web_fetch/web_search/history/workflow` pre-bound as Python functions in the kernel.

**Architecture:** One long-lived process per session — the detached ipykernel, keyed by Claude session id and discovered through a SessionStart-hook run-file. The MCP adapter and the `ptc` CLI are thin multi-client wrappers over the Jupyter wire protocol; all durable cell state (logs, terminal records, audit) is written **kernel-side** so any client, including a fresh one, can settle any cell. Claude child agents are spawned *inside* the kernel by the `claude-agent-sdk`; Codex children through a stdio `codex app-server` JSON-RPC client.

**Tech Stack:** Python 3.12 (uv-managed venv at `~/.ptc/venv`), `ipykernel` + `jupyter_client` (ZeroMQ), `mcp` (FastMCP stdio server), `claude-agent-sdk`, `httpx` + `markdownify`, `pytest`.

**Spec:** `ptc-surface/docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md` — the source of truth. Read its Purpose, Architecture, and the section for the task you are executing before you start.

## Global Constraints

- Repo root for all paths below: `/Users/new/Developer/GitHub/codex_somersault`. The package lives at `ptc-surface/ptc/`; run all `uv`/`pytest` commands from there unless a step says otherwise.
- Python **3.12** for the kernel venv. POSIX only (macOS + Linux); no Windows code paths.
- **Never add the `anthropic` package** as a dependency anywhere. All model calls go through `claude-agent-sdk` (subscription-billed via the user's `claude` login).
- **No `structuredContent`** in any MCP tool reply — content array only.
- Output caps: default `max_output_chars=12000`, server-side clamp **50_000**, aggregate reply budget ~4 MB, max 2 images × 1.5 MB per cell.
- Defaults: `PTC_YIELD_S=300`, `PTC_IDLE_HOURS=24`, `PTC_MAX_CONCURRENCY=8`, `PTC_MAX_DEPTH=1`, `PTC_DEPTH=0`, `PTC_HOME=~/.ptc`.
- Claude children default `permission_mode="bypassPermissions"`; every spawned child gets a **fresh `PTC_SESSION`** (never inherited) and `PTC_DEPTH=<parent+1>`.
- Live tests (anything that spawns `claude` or `codex`) run **only when env `PTC_LIVE=1`** is set — they bill real quota. Keyless tests must not require any auth and must never skip.
- Commits: message prefix `f5(ptc): `, **no `Co-Authored-By` lines**, commit after every task (and mid-task where a step says so). Never push.
- The skill file must contain **no search guidance** (no rg/grep/glob examples) — deliberate spec decision.
- Style: plain modern Python, type hints on public signatures, no speculative abstractions. stdlib `argparse`, `dataclasses`, `pathlib`. Files < ~400 lines; split before growing past that.

---

## File structure (locked)

```
ptc-surface/ptc/
  pyproject.toml                # package "ptc"; console scripts ptc, ptc-mcp; [kernel] extras
  .gitignore                    # .venv-kernel/, __pycache__, *.egg-info
  README.md                     # T27
  src/ptc/
    __init__.py                 # __version__
    paths.py                    # PTC_HOME layout + Config.from_env + safe_key
    venv.py                     # uv provisioning of ~/.ptc/venv + stamp
    lock.py                     # flock context managers (key lock, submit lock)
    ownership.py                # owner.json + pid/start-time identity
    kernel.py                   # ensure/kill/restart/list kernels (spawn under lock)
    discovery.py                # session-key resolution chain + meta.json + run-file read
    cells.py                    # cell logs/records/current.json/offsets — pure file reads
    client.py                   # KernelClient: exec_cell / wait_cell / interrupt (Jupyter wire)
    shape.py                    # result rendering: header, truncation, mutation footer
    mcp.py                      # FastMCP server "ptc": exec/wait/interrupt/restart/kernels
    cli.py                      # ptc CLI: setup/exec/wait/interrupt/restart/list/kill/doctor
    runtime/
      __init__.py               # exports: read write edit bash agent llm web_fetch web_search history workflow
      state.py                  # in-kernel state: config, current cell, audit collector
      bootstrap.py              # install(): tee, cell hooks, records, display shim, watchdog, bind names
      files.py                  # read/write/edit (+ audit)
      shell.py                  # bash() + BashResult/BashHandle (+ audit)
      audit.py                  # audit.jsonl append + per-cell slice
      agents.py                 # agent namespace, registry, semaphore, depth brake
      claude_backend.py         # claude-agent-sdk calls
      codex_backend.py          # codex app-server stdio JSON-RPC client
      llm.py                    # llm()
      web.py                    # web_fetch / web_search (+ FetchResult/SearchResult)
      transcript.py             # history() + Transcript
      wf.py                     # workflow.parallel / pipeline / phase
  plugin/
    .claude-plugin/plugin.json
    .mcp.json                   # {"mcpServers":{"ptc":{"command":"${CLAUDE_PLUGIN_ROOT}/bin/ptc-launch"}}}
    bin/ptc-launch              # stdlib-only: provision venv if stale, exec venv's `python -m ptc.mcp`
    hooks/hooks.json
    hooks/session_start.py      # stdlib-only: stdin JSON → ~/.ptc/run/claude-<claude-pid>.json
    skills/ptc/SKILL.md
  test/
    conftest.py                 # tmp PTC_HOME fixture + shared cached kernel venv
    unit/                       # no kernel, no network, no auth
    integration/                # real ipykernel, keyless, NON-SKIPPABLE
    spikes/                     # s1_sdk_in_kernel.py … s6_websearch_shape.py
    live/                       # PTC_LIVE=1-gated acceptance drivers
```

`~/.ptc/` layout at runtime (per spec): `venv/`, `run/claude-<pid>.json`, `kernels/<key>/{lock, submit.lock, connection.json, owner.json, meta.json, ready, kernel.log, expired.marker, audit.jsonl, agents.json, cells/{N.log, N.json, N-K.png, current.json, offsets/N.offset}, cells-prev-<ts>/…}`.

## Concurrency contract (binding for T4–T6)

**Kernel-dir states** (per key): `ABSENT` → `SPAWNING` (key `lock` held) → `READY` (`owner.json` identity matches a live process **and** `ready` exists) → `EXPIRED` (`expired.marker`, no owner) | `DEAD` (owner identity mismatch → stale files, cleaned under `lock` at next spawn).

**Cell states**: `RUNNING` (= `cells/current.json` names it ∧ no `cells/<id>.json` record ∧ kernel alive) → `DONE(ok|error|interrupted)` (record exists, written atomically by the kernel's `post_run_cell` hook) | `ORPHANED` (log exists ∧ no record ∧ kernel dead → reported as `aborted: kernel died`).

**Linearization points:**
1. **Spawn**: the per-key `flock("lock")`. Exactly one process cleans stale state, rotates `cells/` → `cells-prev-<ts>/`, spawns, writes `owner.json` (pid + OS start-time + nonce + epoch), and only then writes `ready`.
2. **Submit**: `flock("submit.lock")` held across [busy-check → `execute_request` send → `execute_input` received → `cells/current.json` shows that execution_count]. Winner exits the lock only when `current.json` (written by the kernel's `pre_run_cell`) confirms its cell; every loser acquiring the lock sees busy. **Nothing is ever silently queued.**
3. **Completion**: atomic rename of `cells/<id>.json` (`.tmp` → final). A record's existence *is* completion.
4. **Watchdog exit**: takes `flock("lock")`, re-checks idleness, writes `expired.marker`, removes `owner.json` + `ready`, then `os._exit(0)` — a concurrent client sees READY or EXPIRED, never half-dead.
5. **Offsets**: `wait` cursors are **caller-held** (`since` param / `next_offset` return); the sidecar `offsets/<id>.offset` is only the single-waiter convenience default.

## Shared interface reference

Later tasks rely on these exact names (each task's **Interfaces** block repeats what it needs):

```python
# paths.py
def ptc_home() -> Path; def venv_dir() -> Path; def run_dir() -> Path
def kernel_dir(key: str) -> Path; def cells_dir(key: str) -> Path
def safe_key(raw: str) -> str
MAX_OUTPUT_CLAMP = 50_000
@dataclass class Config: yield_s: float; max_output_chars: int; idle_hours: float;
    max_concurrency: int; max_depth: int; depth: int; session: str | None; cwd: str | None
    @classmethod def from_env(cls, env=None) -> "Config"  # env: dict-like, defaults to os.environ internally

# ownership.py
def proc_start_time(pid: int) -> str | None
@dataclass class Owner: pid: int; proc_start_time: str | None; spawned_at: float; nonce: str; epoch: str
def write_owner(key, o: Owner); def read_owner(key) -> Owner | None; def owner_alive(o) -> bool

# kernel.py
@dataclass class KernelInfo: key: str; pid: int; connection_file: Path; spawned: bool; expired_notice: str | None
def ensure_kernel(key, *, cwd=None, claude_session_id=None, config=None) -> KernelInfo
def kill_kernel(key) -> bool; def restart_kernel(key, **kw) -> KernelInfo
def list_kernels() -> list[dict]; def kernel_alive(key) -> bool

# discovery.py
@dataclass class Resolved: key: str; source: str; claude_session_id: str | None; cwd: str | None; degraded: bool
def resolve(explicit: str | None = None, ppid: int | None = None, env=os.environ) -> Resolved
def read_meta(key) -> dict; def write_meta(key, **fields)

# cells.py
@dataclass class CellRecord: status: str; duration_ms: int; result_repr: str | None;
    error: dict | None; images: list[str]; mutations: list[dict]
def read_record(key, cell_id) -> CellRecord | None
def read_output_since(key, cell_id, offset: int, max_bytes: int = 4_000_000) -> tuple[str, int]
def current_cell(key) -> int | None
def default_offset(key, cell_id) -> int; def save_offset(key, cell_id, offset: int)

# client.py
@dataclass class Completed: cell_id: int; record: CellRecord; output: str
@dataclass class Running: cell_id: int; output: str; next_offset: int
@dataclass class Busy: cell_id: int | None   # -1 = a submission is still being acknowledged
class KernelClient:
    def __init__(self, key: str)
    def exec_cell(self, code: str, timeout_s: float, config: Config) -> Completed | Running | Busy
    def wait_cell(self, cell_id: int, timeout_s: float, since: int = -1) -> Completed | Running
    def interrupt(self) -> None

# shape.py
@dataclass class Rendered: text: str; images: list[Path]
def render(outcome, key: str, config: Config, degraded: bool = False) -> Rendered
def to_dict(outcome, key: str) -> dict     # for CLI --json

# runtime/agents.py (inside the kernel)
@dataclass class AgentResult: text: str; session_id: str | None; structured: dict | None;
    cost_usd: float | None; num_turns: int | None; duration_ms: int
class AgentHandle:  # .name .session_id .status in {"running","done","error","interrupted"}
    async def result(self) -> AgentResult; async def send(self, msg: str) -> AgentResult
    def messages(self) -> list[dict]; def history(self); async def interrupt(self); async def close(self)
agent.run / agent.spawn / agent.fork / agent.gather / agent.list / agent.resume  # per spec signatures
```

---

# Milestone M0 — spike gate + kernel spine

### Task 1: Package skeleton, paths, config

**Files:**
- Create: `ptc-surface/ptc/pyproject.toml`
- Create: `ptc-surface/ptc/.gitignore`
- Create: `ptc-surface/ptc/src/ptc/__init__.py`
- Create: `ptc-surface/ptc/src/ptc/paths.py`
- Create: `ptc-surface/ptc/test/unit/test_paths.py`

**Interfaces:**
- Produces: everything in the `paths.py` block of the Shared interface reference (exact names/types above). Every later task imports from `ptc.paths`.

- [ ] **Step 1: Write pyproject + gitignore + package init**

`ptc-surface/ptc/pyproject.toml`:

```toml
[project]
name = "ptc"
version = "0.1.0"
description = "Programmatic Tool Calling for Claude Code on a persistent IPython kernel"
requires-python = ">=3.12"
dependencies = [
    "ipykernel>=6.29",
    "jupyter_client>=8.6",
    "nest_asyncio>=1.6",
    "mcp>=1.2",
    "claude-agent-sdk",
    "httpx>=0.27",
    "markdownify>=0.13",
    "pydantic>=2.7",
]

[project.optional-dependencies]
kernel = ["pandas", "numpy", "pyyaml", "matplotlib"]

[project.scripts]
ptc = "ptc.cli:main"
ptc-mcp = "ptc.mcp:main"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ptc"]
```

Note: pin `claude-agent-sdk` to an exact `==` version in **this step** — run `uv pip index versions claude-agent-sdk 2>/dev/null || python3 -m pip index versions claude-agent-sdk` (or `uv run --with claude-agent-sdk python -c "import claude_agent_sdk;print(claude_agent_sdk.__version__)"`), take the latest, and write `"claude-agent-sdk==<that version>"` (spec: version-pinned).

`ptc-surface/ptc/.gitignore`:

```
__pycache__/
*.egg-info/
.venv-kernel/
.pytest_cache/
```

`ptc-surface/ptc/src/ptc/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 2: Write the failing tests**

`ptc-surface/ptc/test/unit/test_paths.py`:

```python
from pathlib import Path
from ptc.paths import Config, MAX_OUTPUT_CLAMP, kernel_dir, ptc_home, safe_key


def test_ptc_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    assert ptc_home() == tmp_path / "home"
    assert kernel_dir("abc") == tmp_path / "home" / "kernels" / "abc"


def test_config_defaults():
    cfg = Config.from_env(env={})
    assert (cfg.yield_s, cfg.max_output_chars, cfg.idle_hours) == (300.0, 12_000, 24.0)
    assert (cfg.max_concurrency, cfg.max_depth, cfg.depth) == (8, 1, 0)
    assert cfg.session is None


def test_config_env_and_clamp():
    env = {"PTC_YIELD_S": "5", "PTC_MAX_OUTPUT_CHARS": "999999", "PTC_DEPTH": "2",
           "PTC_SESSION": "abc", "PTC_IDLE_HOURS": "0.01", "PTC_MAX_OUTPUT_CHARS_BAD": "x"}
    cfg = Config.from_env(env=env)
    assert cfg.yield_s == 5.0
    assert cfg.max_output_chars == MAX_OUTPUT_CLAMP  # clamped
    assert cfg.depth == 2 and cfg.session == "abc" and cfg.idle_hours == 0.01


def test_config_bad_values_fall_back():
    assert Config.from_env(env={"PTC_YIELD_S": "banana"}).yield_s == 300.0


def test_safe_key():
    assert safe_key("96abe6e2-80aa") == "96abe6e2-80aa"
    assert safe_key("a/b c!") == "a-b-c-"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd ptc-surface/ptc && uv run --group dev pytest test/unit/test_paths.py -q
```
Expected: FAIL / import error (`ptc.paths` missing).

- [ ] **Step 4: Implement `paths.py`**

`ptc-surface/ptc/src/ptc/paths.py`:

```python
"""PTC_HOME filesystem layout and environment-derived configuration."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CLAMP = 50_000


def ptc_home() -> Path:
    return Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc"))


def venv_dir() -> Path:
    return ptc_home() / "venv"


def run_dir() -> Path:
    return ptc_home() / "run"


def kernels_root() -> Path:
    return ptc_home() / "kernels"


def kernel_dir(key: str) -> Path:
    return kernels_root() / key


def cells_dir(key: str) -> Path:
    return kernel_dir(key) / "cells"


def safe_key(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:128]


@dataclass
class Config:
    yield_s: float = 300.0
    max_output_chars: int = 12_000
    idle_hours: float = 24.0
    max_concurrency: int = 8
    max_depth: int = 1
    depth: int = 0
    session: str | None = None
    cwd: str | None = None

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env

        def num(name: str, cast, default):
            raw = env.get(name)
            if not raw:
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            yield_s=num("PTC_YIELD_S", float, 300.0),
            max_output_chars=min(num("PTC_MAX_OUTPUT_CHARS", int, 12_000), MAX_OUTPUT_CLAMP),
            idle_hours=num("PTC_IDLE_HOURS", float, 24.0),
            max_concurrency=num("PTC_MAX_CONCURRENCY", int, 8),
            max_depth=num("PTC_MAX_DEPTH", int, 1),
            depth=num("PTC_DEPTH", int, 0),
            session=env.get("PTC_SESSION") or None,
            cwd=env.get("PTC_CWD") or None,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run --group dev pytest test/unit/test_paths.py -q
```
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T1 — package skeleton, paths, env config"
```

---

### Task 2: Venv provisioning (`venv.py`)

**Files:**
- Create: `ptc-surface/ptc/src/ptc/venv.py`
- Create: `ptc-surface/ptc/test/unit/test_venv.py`

**Interfaces:**
- Consumes: `ptc.paths.venv_dir`, `ptc_home`.
- Produces: `ensure_venv(run=subprocess.run) -> Path` (returns venv python, provisioning if stale), `venv_python() -> Path`, `stamp_current() -> bool`, `PKG_ROOT: Path` (the `ptc-surface/ptc` checkout dir), `stamp_payload() -> dict`.

The stamp is `venv/.ptc-version`, JSON `{"schema": 1, "pyproject_sha": sha256(pyproject.toml bytes), "pkg": str(PKG_ROOT)}` — the same payload `plugin/bin/ptc-launch` (T11) computes independently with stdlib only, so **do not change the payload shape without changing both**.

- [ ] **Step 1: Write the failing tests**

`ptc-surface/ptc/test/unit/test_venv.py`:

```python
import json
from ptc import venv
from ptc.paths import venv_dir


def _fake_run_factory(calls):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        # simulate uv creating the python binary on `uv venv`
        if cmd[1] == "venv":
            p = venv_dir() / "bin"
            p.mkdir(parents=True, exist_ok=True)
            (p / "python").write_text("#!fake\n")
        class R: returncode = 0
        return R()
    return fake_run


def test_provisions_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    calls: list = []
    py = venv.ensure_venv(run=_fake_run_factory(calls))
    assert py == venv_dir() / "bin" / "python"
    assert any(c[1] == "venv" for c in calls)
    assert any("pip" in c for c in calls)          # uv pip install -e .[kernel]
    stamp = json.loads((venv_dir() / ".ptc-version").read_text())
    assert stamp["schema"] == 1 and "pyproject_sha" in stamp


def test_skips_when_stamp_current(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    calls: list = []
    venv.ensure_venv(run=_fake_run_factory(calls))
    calls.clear()
    venv.ensure_venv(run=_fake_run_factory(calls))
    assert calls == []


def test_reprovisions_on_stamp_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    calls: list = []
    venv.ensure_venv(run=_fake_run_factory(calls))
    (venv_dir() / ".ptc-version").write_text('{"schema": 0}')
    calls.clear()
    venv.ensure_venv(run=_fake_run_factory(calls))
    assert any(c[1] == "venv" for c in calls)
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/unit/test_venv.py -q
```
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `venv.py`**

`ptc-surface/ptc/src/ptc/venv.py`:

```python
"""Provision ~/.ptc/venv with uv. Stamp payload is shared with plugin/bin/ptc-launch."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .paths import ptc_home, venv_dir

PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # .../ptc-surface/ptc


def venv_python() -> Path:
    return venv_dir() / "bin" / "python"


def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def stamp_payload() -> dict:
    sha = hashlib.sha256((PKG_ROOT / "pyproject.toml").read_bytes()).hexdigest()
    return {"schema": 1, "pyproject_sha": sha, "pkg": str(PKG_ROOT)}


def stamp_current() -> bool:
    stamp = venv_dir() / ".ptc-version"
    if not (venv_python().exists() and stamp.exists()):
        return False
    try:
        return json.loads(stamp.read_text()) == stamp_payload()
    except (json.JSONDecodeError, OSError):
        return False


def ensure_venv(run=subprocess.run) -> Path:
    if stamp_current():
        return venv_python()
    lock = ptc_home() / "provision.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()  # mkdir-based lock, matches ptc-launch
    except FileExistsError:
        # another provisioner is running; wait for it (up to 10 min), then re-check
        import time
        for _ in range(1200):
            if not lock.exists():
                break
            time.sleep(0.5)
        if stamp_current():
            return venv_python()
        raise RuntimeError("venv provisioning lock held and venv still stale; "
                           f"remove {lock} if no other ptc process is running")
    try:
        uv = _uv()
        run([uv, "venv", str(venv_dir()), "--python", "3.12", "--seed"], check=True)
        run([uv, "pip", "install", "--python", str(venv_python()),
             "-e", f"{PKG_ROOT}[kernel]"], check=True)
        (venv_dir() / ".ptc-version").write_text(json.dumps(stamp_payload()))
    finally:
        lock.rmdir()
    return venv_python()
```

- [ ] **Step 4: Run tests**

```bash
uv run --group dev pytest test/unit/test_venv.py -q
```
Expected: 3 passed.

- [ ] **Step 5: Real provisioning smoke (one-time, also builds the test-kernel venv)**

Create `ptc-surface/ptc/test/conftest.py` — the shared fixtures every integration test uses. The real venv is provisioned **once** into `.venv-kernel/` (gitignored) and symlinked into each test's tmp `PTC_HOME`, so integration tests are fast after first run:

```python
import os
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
CACHE_VENV = PKG / ".venv-kernel"


@pytest.fixture(scope="session")
def kernel_venv() -> Path:
    """A real venv with ipykernel + ptc (editable), cached across runs."""
    py = CACHE_VENV / "bin" / "python"
    marker = CACHE_VENV / ".ok"
    if not (py.exists() and marker.exists()):
        uv = os.environ.get("UV", "uv")
        subprocess.run([uv, "venv", str(CACHE_VENV), "--python", "3.12", "--seed"], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(py), "-e", f"{PKG}[kernel]"], check=True)
        marker.write_text("ok")
    return CACHE_VENV


@pytest.fixture
def ptc_home(tmp_path, monkeypatch, kernel_venv) -> Path:
    """Isolated PTC_HOME whose venv/ is the cached real venv."""
    home = tmp_path / "ptc-home"
    home.mkdir()
    (home / "venv").symlink_to(kernel_venv)
    (home / "venv" / ".ptc-version").exists()  # stamp not needed: tests bypass ensure_venv
    monkeypatch.setenv("PTC_HOME", str(home))
    monkeypatch.delenv("PTC_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return home
```

Run once to prove real provisioning works end to end:

```bash
uv run --group dev python -c "
import test.conftest as c" 2>/dev/null; uv run --group dev pytest test/unit -q
```
Then force-build the cache: `uv venv .venv-kernel --python 3.12 --seed && uv pip install --python .venv-kernel/bin/python -e ".[kernel]"`
Expected: install completes; `.venv-kernel/bin/python -c "import ipykernel, ptc"` exits 0. Then `touch .venv-kernel/.ok`.

- [ ] **Step 6: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T2 — uv venv provisioning with shared stamp + test venv cache"
```

---

### Task 3: Locks and process-identity ownership

**Files:**
- Create: `ptc-surface/ptc/src/ptc/lock.py`
- Create: `ptc-surface/ptc/src/ptc/ownership.py`
- Create: `ptc-surface/ptc/test/unit/test_lock_ownership.py`

**Interfaces:**
- Consumes: `ptc.paths.kernel_dir`.
- Produces: `lock.flock_path(path, timeout=None)` (context manager), `lock.key_lock(key)`, `lock.submit_lock(key)` (10 s timeout → `TimeoutError`); `ownership.proc_start_time(pid) -> str | None`, `ownership.Owner` dataclass (fields per Shared interface reference), `ownership.write_owner/read_owner/owner_alive`.

- [ ] **Step 1: Write the failing tests**

`ptc-surface/ptc/test/unit/test_lock_ownership.py`:

```python
import json
import multiprocessing as mp
import os
import time

from ptc.lock import flock_path, key_lock, submit_lock
from ptc.ownership import Owner, owner_alive, proc_start_time, read_owner, write_owner


def _hold(path, dur, q):
    from ptc.lock import flock_path
    from pathlib import Path
    with flock_path(Path(path)):
        q.put("held")
        time.sleep(dur)


def test_flock_excludes_across_processes(tmp_path):
    p = tmp_path / "l"
    q = mp.Queue()
    proc = mp.Process(target=_hold, args=(str(p), 1.0, q))
    proc.start()
    assert q.get(timeout=5) == "held"
    t0 = time.monotonic()
    with flock_path(p):          # must block until child releases
        waited = time.monotonic() - t0
    proc.join()
    assert waited > 0.5


def test_flock_timeout(tmp_path):
    p = tmp_path / "l"
    q = mp.Queue()
    proc = mp.Process(target=_hold, args=(str(p), 2.0, q))
    proc.start()
    q.get(timeout=5)
    try:
        import pytest
        with pytest.raises(TimeoutError):
            with flock_path(p, timeout=0.2):
                pass
    finally:
        proc.join()


def test_owner_roundtrip_and_liveness(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    me = os.getpid()
    st = proc_start_time(me)
    assert st  # own process must have a readable start time
    o = Owner(pid=me, proc_start_time=st, spawned_at=time.time(), nonce="n", epoch="e1")
    write_owner("k1", o)
    got = read_owner("k1")
    assert got == o and owner_alive(got)
    # wrong start time == PID reuse -> not alive
    assert not owner_alive(Owner(me, "Thu Jan  1 00:00:00 1970", 0.0, "n", "e"))
    # dead pid -> not alive
    assert not owner_alive(Owner(99999999, st, 0.0, "n", "e"))


def test_read_owner_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    assert read_owner("nope") is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/unit/test_lock_ownership.py -q
```
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement**

`ptc-surface/ptc/src/ptc/lock.py`:

```python
"""flock-based mutual exclusion. POSIX only (spec: no Windows in v1)."""
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import kernel_dir


@contextmanager
def flock_path(path: Path, timeout: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"lock busy: {path}")
                    time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def key_lock(key: str):
    return flock_path(kernel_dir(key) / "lock")


def submit_lock(key: str):
    return flock_path(kernel_dir(key) / "submit.lock", timeout=10.0)
```

`ptc-surface/ptc/src/ptc/ownership.py`:

```python
"""owner.json: which OS process is this key's kernel. Identity = pid + start time."""
import json
import os
import subprocess
from dataclasses import asdict, dataclass

from .paths import kernel_dir


def proc_start_time(pid: int) -> str | None:
    """`ps -o lstart=` works on macOS and Linux procps; None if unreadable."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        s = out.stdout.strip()
        return s or None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class Owner:
    pid: int
    proc_start_time: str | None
    spawned_at: float
    nonce: str
    epoch: str


def write_owner(key: str, o: Owner) -> None:
    d = kernel_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "owner.json.tmp"
    tmp.write_text(json.dumps(asdict(o)))
    tmp.replace(d / "owner.json")


def read_owner(key: str) -> Owner | None:
    p = kernel_dir(key) / "owner.json"
    try:
        return Owner(**json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def owner_alive(o: Owner) -> bool:
    try:
        os.kill(o.pid, 0)
    except OSError:
        return False
    st = proc_start_time(o.pid)
    return st is not None and o.proc_start_time is not None and st == o.proc_start_time
```

- [ ] **Step 4: Run tests**

```bash
uv run --group dev pytest test/unit/test_lock_ownership.py -q
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T3 — flock helpers and pid+start-time kernel ownership"
```

---

### Task 4: Kernel spawn/kill/list under the key lock

**Files:**
- Create: `ptc-surface/ptc/src/ptc/kernel.py`
- Create: `ptc-surface/ptc/src/ptc/discovery.py` (meta.json read/write ONLY in this task; the resolution chain is T13)
- Create: `ptc-surface/ptc/test/integration/test_kernel_lifecycle.py`

**Interfaces:**
- Consumes: T2 `venv_python` (via the conftest symlinked venv), T3 locks/ownership.
- Produces: `kernel.ensure_kernel(key, *, cwd=None, claude_session_id=None, config=None) -> KernelInfo`, `kernel.kill_kernel(key) -> bool`, `kernel.restart_kernel(key, **kw) -> KernelInfo`, `kernel.list_kernels() -> list[dict]`, `kernel.kernel_alive(key) -> bool`, `kernel.consume_expiry(key) -> str | None`; `discovery.write_meta(key, **fields)`, `discovery.read_meta(key) -> dict`.
- NOTE: `ensure_kernel` does **not** run the runtime bootstrap yet — T5 adds `client.bootstrap` and wires it in.

- [ ] **Step 1: Write the failing integration test**

`ptc-surface/ptc/test/integration/test_kernel_lifecycle.py`:

```python
import json
import multiprocessing as mp
import os

from ptc.kernel import ensure_kernel, kernel_alive, kill_kernel, list_kernels
from ptc.ownership import read_owner


def test_spawn_ready_and_reuse(ptc_home):
    info = ensure_kernel("k1", cwd=str(ptc_home))
    assert info.spawned and info.pid > 0
    assert (ptc_home / "kernels" / "k1" / "ready").exists()
    conn = json.loads(info.connection_file.read_text())
    assert conn["shell_port"] > 0          # kernel wrote real ports back
    assert kernel_alive("k1")
    info2 = ensure_kernel("k1")
    assert not info2.spawned and info2.pid == info.pid
    rows = list_kernels()
    assert any(r["key"] == "k1" and r["alive"] for r in rows)
    assert kill_kernel("k1")
    assert not kernel_alive("k1")


def _race(home, q):
    os.environ["PTC_HOME"] = home
    from ptc.kernel import ensure_kernel
    q.put(ensure_kernel("race").pid)


def test_concurrent_first_exec_spawns_one_kernel(ptc_home):
    q = mp.Queue()
    ps = [mp.Process(target=_race, args=(str(ptc_home), q)) for _ in range(2)]
    [p.start() for p in ps]
    pids = {q.get(timeout=120) for _ in ps}
    [p.join() for p in ps]
    assert len(pids) == 1                   # exactly one kernel won
    kill_kernel("race")


def test_dead_owner_is_cleaned_and_respawned(ptc_home):
    info = ensure_kernel("k2")
    os.kill(info.pid, 9)
    import time; time.sleep(0.5)
    info2 = ensure_kernel("k2")
    assert info2.spawned and info2.pid != info.pid
    # old cells dir rotated
    assert any(p.name.startswith("cells-prev-") for p in (ptc_home / "kernels" / "k2").iterdir())
    kill_kernel("k2")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/integration/test_kernel_lifecycle.py -q
```
Expected: FAIL (module missing). (First run builds `.venv-kernel/` — allow a few minutes.)

- [ ] **Step 3: Implement `discovery.py` (meta only) and `kernel.py`**

`ptc-surface/ptc/src/ptc/discovery.py` (start with just meta; T13 extends this file):

```python
"""Session-key discovery (T13) + kernel meta.json (T4)."""
import json
from .paths import kernel_dir


def write_meta(key: str, **fields) -> None:
    d = kernel_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    merged = read_meta(key)
    merged.update(fields)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(merged))
    tmp.replace(d / "meta.json")


def read_meta(key: str) -> dict:
    try:
        return json.loads((kernel_dir(key) / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
```

`ptc-surface/ptc/src/ptc/kernel.py`:

```python
"""Spawn/kill/list detached ipykernels. All state transitions under the per-key lock."""
import json
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .discovery import write_meta
from .lock import key_lock
from .ownership import Owner, owner_alive, proc_start_time, read_owner
from .paths import Config, cells_dir, kernel_dir, kernels_root
from .venv import venv_python


@dataclass
class KernelInfo:
    key: str
    pid: int
    connection_file: Path
    spawned: bool
    expired_notice: str | None


def kernel_alive(key: str) -> bool:
    o = read_owner(key)
    return bool(o and owner_alive(o) and (kernel_dir(key) / "ready").exists())


def consume_expiry(key: str) -> str | None:
    m = kernel_dir(key) / "expired.marker"
    try:
        text = m.read_text()
        m.unlink()
        return text
    except OSError:
        return None


def _rotate_cells(kd: Path) -> None:
    cd = kd / "cells"
    if cd.exists():
        cd.rename(kd / f"cells-prev-{int(time.time())}")


def _clean_stale(kd: Path, key: str) -> None:
    o = read_owner(key)
    if o and owner_alive(o):
        # a live kernel with no `ready` (e.g. bootstrap failed) must be reaped
        # before respawn, or every retry leaks a detached process (F4)
        try:
            os.kill(o.pid, signal.SIGKILL)
        except OSError:
            pass
    for name in ("owner.json", "ready", "connection.json"):
        (kd / name).unlink(missing_ok=True)
    _rotate_cells(kd)


def _wait_ports(conn: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(conn.read_text())
            if data.get("shell_port", 0) > 0:
                return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise TimeoutError(f"kernel never wrote ports to {conn}")


def _kernel_info_roundtrip(conn: Path, timeout: float = 20.0) -> None:
    from jupyter_client import BlockingKernelClient
    kc = BlockingKernelClient()
    kc.load_connection_file(str(conn))
    kc.start_channels()
    try:
        kc.kernel_info()
        kc.get_shell_msg(timeout=timeout)
    finally:
        kc.stop_channels()


def ensure_kernel(key: str, *, cwd: str | None = None,
                  claude_session_id: str | None = None,
                  config: Config | None = None) -> KernelInfo:
    cfg = config or Config.from_env()
    kd = kernel_dir(key)
    kd.mkdir(parents=True, exist_ok=True)
    with key_lock(key):
        o = read_owner(key)
        if o and owner_alive(o) and (kd / "ready").exists():
            return KernelInfo(key, o.pid, kd / "connection.json", False, None)
        expired = consume_expiry(key)
        _clean_stale(kd, key)
        cells_dir(key).mkdir(parents=True, exist_ok=True)
        (cells_dir(key) / "offsets").mkdir(exist_ok=True)
        conn = kd / "connection.json"
        work = cwd or cfg.cwd or os.getcwd()
        env = {**os.environ,
               "PTC_SESSION": key, "PTC_CWD": work,
               "PTC_DEPTH": str(cfg.depth), "PTC_MAX_DEPTH": str(cfg.max_depth),
               "PTC_IDLE_HOURS": str(cfg.idle_hours),
               "PTC_MAX_CONCURRENCY": str(cfg.max_concurrency),
               "PTC_HOME": str(kernels_root().parent)}
        log = open(kd / "kernel.log", "ab")
        proc = subprocess.Popen(
            [str(venv_python()), "-m", "ipykernel_launcher", "-f", str(conn)],
            cwd=work, env=env, stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            _wait_ports(conn)
            os.chmod(conn, 0o600)
            _kernel_info_roundtrip(conn)
        except Exception:
            proc.kill()
            raise
        epoch = str(int(time.time()))
        write_owner(key, Owner(proc.pid, proc_start_time(proc.pid),
                               time.time(), secrets.token_hex(8), epoch))
        write_meta(key, kernel_key=key, claude_session_id=claude_session_id,
                   cwd=work, depth=cfg.depth, epoch=epoch)
        (kd / "ready").write_text(epoch)
        return KernelInfo(key, proc.pid, conn, True, expired)


def kill_kernel(key: str) -> bool:
    with key_lock(key):
        o = read_owner(key)
        if not o:
            return False
        if owner_alive(o):
            try:
                os.kill(o.pid, signal.SIGKILL)
            except OSError:
                pass
        (kernel_dir(key) / "owner.json").unlink(missing_ok=True)
        (kernel_dir(key) / "ready").unlink(missing_ok=True)
        return True


def restart_kernel(key: str, **kw) -> KernelInfo:
    kill_kernel(key)
    time.sleep(0.2)
    return ensure_kernel(key, **kw)


def list_kernels() -> list[dict]:
    rows = []
    root = kernels_root()
    if not root.exists():
        return rows
    from .discovery import read_meta
    for kd in sorted(root.iterdir()):
        if not kd.is_dir():
            continue
        o = read_owner(kd.name)
        meta = read_meta(kd.name)
        cells = kd / "cells"
        try:
            last_used = max((f.stat().st_mtime for f in cells.glob("*.log")),
                            default=(o.spawned_at if o else None))
        except OSError:
            last_used = o.spawned_at if o else None
        rows.append({
            "key": kd.name,
            "pid": o.pid if o else None,
            "alive": bool(o and owner_alive(o) and (kd / "ready").exists()),
            "cwd": meta.get("cwd"),
            "depth": meta.get("depth", 0),
            "spawned_at": o.spawned_at if o else None,
            "last_used": last_used,
        })
    return rows
```

- [ ] **Step 4: Run tests**

```bash
uv run --group dev pytest test/integration/test_kernel_lifecycle.py -q
```
Expected: 3 passed (spawn ~2–5 s each).

- [ ] **Step 5: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T4 — race-proof detached kernel spawn/kill/list with identity ownership"
```

---

### Task 5: In-kernel bootstrap v0 (tee, records, watchdog) + `cells.py` + basic `client.exec_cell`

This is the heart of the substrate. The bootstrap runs INSIDE the kernel (it can import `ptc` because the venv has it editable); everything durable is written kernel-side.

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/__init__.py`
- Create: `ptc-surface/ptc/src/ptc/runtime/state.py`
- Create: `ptc-surface/ptc/src/ptc/runtime/audit.py`
- Create: `ptc-surface/ptc/src/ptc/runtime/bootstrap.py`
- Create: `ptc-surface/ptc/src/ptc/cells.py`
- Create: `ptc-surface/ptc/src/ptc/client.py` (run-to-completion path only; yield/busy is T6)
- Modify: `ptc-surface/ptc/src/ptc/kernel.py` (call `client.run_bootstrap` at the end of a successful spawn)
- Create: `ptc-surface/ptc/test/integration/test_cells_records.py`

**Interfaces:**
- Consumes: T4 `ensure_kernel`.
- Produces: `cells.CellRecord`, `cells.read_record(key, cell_id)`, `cells.read_output_since(key, cell_id, offset, max_bytes=4_000_000) -> (str, int)`, `cells.current_cell(key) -> int | None`, `cells.default_offset/save_offset`; `client.KernelClient(key)` with `.exec_cell(code, timeout_s, config)` (this task: completion path returns `Completed`), `client.run_bootstrap(key, config)`; runtime `state.STATE` (config, current cell id, audit collector, last_activity), `audit.append(kind, **fields)`, `audit.entries_for_cell(key, cell_id) -> list[dict]`.
- The record JSON schema (fixed): `{"status": "ok"|"error"|"interrupted", "duration_ms": int, "result_repr": str|null, "error": {"ename","evalue","traceback"}|null, "images": [str], "mutations": [dict]}`.

- [ ] **Step 1: Write the failing integration test**

`ptc-surface/ptc/test/integration/test_cells_records.py`:

```python
from ptc.cells import current_cell, read_output_since, read_record
from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_exec_writes_log_and_record(ptc_home):
    ensure_kernel("c1", cwd=str(ptc_home))
    kc = KernelClient("c1")
    out = kc.exec_cell("x = 40 + 2\nprint('hello', x)\nx", timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed)
    assert "hello 42" in out.output
    rec = read_record("c1", out.cell_id)
    assert rec.status == "ok" and rec.result_repr == "42" and rec.duration_ms >= 0
    text, off = read_output_since("c1", out.cell_id, 0)
    assert "hello 42" in text and off > 0
    # state persists across cells
    out2 = kc.exec_cell("print(x + 1)", timeout_s=60, config=Config.from_env())
    assert "43" in out2.output and out2.cell_id == out.cell_id + 1
    kill_kernel("c1")


def test_cell_id_alignment(ptc_home):
    """F1 guard: execute_input id == current.json == log == record == audit cell.
    If an IPython release changes hook/count ordering, THIS fails — fix _cell_no."""
    ensure_kernel("ca1", cwd=str(ptc_home))
    kc = KernelClient("ca1")
    out = kc.exec_cell("print('align')", timeout_s=60, config=Config.from_env())
    n = out.cell_id
    cells = ptc_home / "kernels" / "ca1" / "cells"
    assert (cells / f"{n}.log").exists() and "align" in (cells / f"{n}.log").read_text()
    assert read_record("ca1", n) is not None
    assert current_cell("ca1") == n
    kill_kernel("ca1")


def test_error_cell_records_error(ptc_home):
    ensure_kernel("c2", cwd=str(ptc_home))
    kc = KernelClient("c2")
    out = kc.exec_cell("1/0", timeout_s=60, config=Config.from_env())
    rec = read_record("c2", out.cell_id)
    assert rec.status == "error" and rec.error["ename"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in out.output      # IPython traceback went to the log
    assert current_cell("c2") == out.cell_id       # current.json points at last cell (done)
    kill_kernel("c2")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/integration/test_cells_records.py -q
```
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement runtime state + audit + bootstrap**

`ptc-surface/ptc/src/ptc/runtime/state.py`:

```python
"""Mutable in-kernel state shared by hooks and the runtime API."""
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _State:
    key: str = ""
    kernel_dir: Path = Path(".")
    config: dict = field(default_factory=dict)
    current_cell: int | None = None
    cell_started: float = 0.0
    last_activity: float = field(default_factory=time.time)
    cell_images: list = field(default_factory=list)
    cell_mutations: list = field(default_factory=list)


STATE = _State()


def cells() -> Path:
    return STATE.kernel_dir / "cells"
```

`ptc-surface/ptc/src/ptc/runtime/audit.py`:

```python
"""audit.jsonl — one line per mutation, attributed to the current cell."""
import json
import time

from .state import STATE


def append(kind: str, **fields) -> None:
    entry = {"ts": time.time(), "cell": STATE.current_cell, "kind": kind, **fields}
    STATE.cell_mutations.append(entry)
    with open(STATE.kernel_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def entries_for_cell(kernel_dir, cell_id: int) -> list[dict]:
    out = []
    try:
        with open(kernel_dir / "audit.jsonl") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("cell") == cell_id:
                    out.append(e)
    except OSError:
        pass
    return out
```

`ptc-surface/ptc/src/ptc/runtime/bootstrap.py`:

```python
"""Runs INSIDE the kernel: tee, cell hooks, terminal records, display shim, watchdog.

Invoked by the host as a bootstrap cell:  import ptc.runtime.bootstrap as _b; _b.install('<json>')
"""
import base64
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from .state import STATE, cells

_MAX_REPR = 4096
_MAX_IMAGES = 2
_MAX_IMAGE_BYTES = 1_500_000


class _Tee:
    """Wraps the ipykernel OutStream; mirrors writes into the current cell's log."""

    def __init__(self, inner):
        self._inner = inner
        self._file = None

    def _switch(self, path: Path | None):
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if path is not None:
            self._file = open(path, "a", errors="replace")

    def write(self, s):
        if self._file:
            try:
                self._file.write(s)
                self._file.flush()
            except OSError:
                pass
        return self._inner.write(s)

    def flush(self):
        return self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


_tees: list[_Tee] = []


def _write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def _cell_no(ip, info) -> int:
    """IPython increments execution_count BEFORE pre_run_cell fires when history is
    stored, so the starting cell's number is count-1 in the store_history case (plan
    review F1). test_cell_id_alignment is the guard: if an IPython release changes
    this ordering, that test fails loudly — flip the correction here with it."""
    if getattr(info, "store_history", True):
        return int(ip.execution_count) - 1
    return int(ip.execution_count)


def _pre_run_cell(info):
    ip = _ip()
    n = _cell_no(ip, info)
    STATE.current_cell = n
    STATE.cell_started = time.perf_counter()
    STATE.last_activity = time.time()
    STATE.cell_images = []
    STATE.cell_mutations = []
    cells().mkdir(parents=True, exist_ok=True)
    for t in _tees:
        t._switch(cells() / f"{n}.log")
    _write_json_atomic(cells() / "current.json", {"cell_id": n, "started_at": time.time()})


def _post_run_cell(result):
    n = getattr(result, "execution_count", None) or STATE.current_cell
    dur = int((time.perf_counter() - STATE.cell_started) * 1000)
    err = result.error_in_exec or result.error_before_exec
    if err is None:
        status, error = "ok", None
    elif isinstance(err, KeyboardInterrupt):
        status, error = "interrupted", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": ""}
    else:
        status = "error"
        error = {"ename": type(err).__name__, "evalue": str(err)[:2000],
                 "traceback": "".join(traceback.format_exception(err))[-8000:]}
    rr = None
    if getattr(result, "result", None) is not None:
        rr = repr(result.result)[:_MAX_REPR]
    record = {"status": status, "duration_ms": dur, "result_repr": rr, "error": error,
              "images": list(STATE.cell_images), "mutations": list(STATE.cell_mutations)}
    _write_json_atomic(cells() / f"{n}.json", record)
    STATE.last_activity = time.time()
    for t in _tees:
        t._switch(None)


def _ip():
    from IPython import get_ipython
    return get_ipython()


def _install_display_shim():
    """Save published PNG/JPEG display data to cells/<n>-<k>.png and record paths."""
    ip = _ip()
    pub = ip.display_pub
    orig = pub.publish

    def publish(data=None, metadata=None, **kw):
        try:
            if data and STATE.current_cell is not None and len(STATE.cell_images) < _MAX_IMAGES:
                for mime, ext in (("image/png", "png"), ("image/jpeg", "jpg")):
                    if mime in data:
                        raw = base64.b64decode(data[mime]) if isinstance(data[mime], str) else data[mime]
                        if len(raw) <= _MAX_IMAGE_BYTES:
                            k = len(STATE.cell_images)
                            p = cells() / f"{STATE.current_cell}-{k}.{ext}"
                            p.write_bytes(raw)
                            STATE.cell_images.append(str(p))
                        break
        except Exception:
            pass
        return orig(data=data, metadata=metadata, **kw)

    pub.publish = publish


def _watchdog():
    from ptc.lock import key_lock
    hours = float(STATE.config.get("idle_hours", 24.0))
    while True:
        time.sleep(min(30.0, max(hours * 3600 / 10, 0.5)))
        idle = time.time() - STATE.last_activity
        if idle < hours * 3600:
            continue
        try:
            with key_lock(STATE.key):
                if time.time() - STATE.last_activity < hours * 3600:
                    continue
                (STATE.kernel_dir / "expired.marker").write_text(
                    f"expired after {idle / 3600:.2f} h idle at {time.strftime('%F %T')}")
                (STATE.kernel_dir / "owner.json").unlink(missing_ok=True)
                (STATE.kernel_dir / "ready").unlink(missing_ok=True)
                # Exit WHILE holding the flock: process death releases it atomically,
                # so a concurrent spawner can never observe a half-dead kernel (F5).
                os._exit(0)
        except Exception:
            continue  # cleanup failed: keep ownership and retry next tick — never
                      # exit leaving partial state behind (F5)


def install(config_json: str) -> str:
    cfg = json.loads(config_json)
    STATE.key = cfg["key"]
    STATE.kernel_dir = Path(cfg["kernel_dir"])
    STATE.config = cfg
    os.environ.setdefault("NO_COLOR", "1")
    ip = _ip()
    try:
        ip.colors = "nocolor"
    except Exception:
        pass
    for stream_name in ("stdout", "stderr"):
        tee = _Tee(getattr(sys, stream_name))
        _tees.append(tee)
        setattr(sys, stream_name, tee)
    # Monotonic cell ids across kernel epochs (F3): continue numbering above the
    # highest archived cell id so a yielded pre-restart cell id can never collide
    # with a new epoch's cell. The shell counter is pre-incremented relative to the
    # kernel counter (see _cell_no); test_cell_id_alignment guards the arithmetic.
    try:
        prev_max = 0
        for d in STATE.kernel_dir.glob("cells-prev-*"):
            for f in d.glob("*.log"):
                try:
                    prev_max = max(prev_max, int(f.stem))
                except ValueError:
                    pass
        if prev_max and prev_max + 1 > int(ip.execution_count):
            ip.execution_count = prev_max + 1              # next cell number = prev_max+1
            if getattr(ip, "kernel", None) is not None:
                ip.kernel.execution_count = prev_max       # next execute_input = prev_max+1
    except Exception:
        pass
    ip.events.register("pre_run_cell", _pre_run_cell)
    ip.events.register("post_run_cell", _post_run_cell)
    _install_display_shim()
    threading.Thread(target=_watchdog, daemon=True, name="ptc-watchdog").start()
    from . import bind
    bind(ip)
    return "ptc-bootstrap-ok"
```

`ptc-surface/ptc/src/ptc/runtime/__init__.py` (v0 — T14+ fill in the real API; `bind` exists from day one so bootstrap is stable):

```python
"""The in-kernel runtime API. Names are bound into the user namespace by bootstrap.bind."""


def bind(ip) -> None:
    """Bind the public runtime names into the kernel user namespace. Extended by later tasks."""
    ns = {}
    # T14: read/write/edit; T15: bash; T20+: agent; T23: llm; T24: web; T25: history; T26: workflow
    ip.user_ns.update(ns)
```

- [ ] **Step 4: Implement `cells.py` and `client.py` (completion path)**

`ptc-surface/ptc/src/ptc/cells.py`:

```python
"""Pure file-level access to per-cell logs, records, current.json, offsets."""
import json
from dataclasses import dataclass

from .paths import cells_dir


@dataclass
class CellRecord:
    status: str
    duration_ms: int
    result_repr: str | None
    error: dict | None
    images: list
    mutations: list


def read_record(key: str, cell_id: int) -> CellRecord | None:
    p = cells_dir(key) / f"{cell_id}.json"
    try:
        d = json.loads(p.read_text())
        return CellRecord(**d)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def read_output_since(key: str, cell_id: int, offset: int,
                      max_bytes: int = 4_000_000) -> tuple[str, int]:
    p = cells_dir(key) / f"{cell_id}.log"
    try:
        with open(p, "rb") as f:
            f.seek(max(offset, 0))
            data = f.read(max_bytes)
            return data.decode(errors="replace"), f.tell()
    except OSError:
        return "", max(offset, 0)


def current_cell(key: str) -> int | None:
    try:
        return json.loads((cells_dir(key) / "current.json").read_text())["cell_id"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def default_offset(key: str, cell_id: int) -> int:
    try:
        return int((cells_dir(key) / "offsets" / f"{cell_id}.offset").read_text())
    except (OSError, ValueError):
        return 0


def save_offset(key: str, cell_id: int, offset: int) -> None:
    d = cells_dir(key) / "offsets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cell_id}.offset").write_text(str(offset))
```

`ptc-surface/ptc/src/ptc/client.py` (T6 extends with busy/yield/wait/interrupt):

```python
"""Jupyter-wire client. One instance per operation is fine; all durable state is on disk."""
import json
import time
from dataclasses import dataclass

from .cells import CellRecord, read_output_since, read_record
from .paths import Config, kernel_dir
from .venv import venv_python  # noqa: F401  (imported for kernel spawn parity)


@dataclass
class Completed:
    cell_id: int
    record: CellRecord
    output: str


@dataclass
class Running:
    cell_id: int
    output: str
    next_offset: int


@dataclass
class Busy:
    cell_id: int | None


class KernelClient:
    def __init__(self, key: str):
        self.key = key

    def _connect(self):
        from jupyter_client import BlockingKernelClient
        kc = BlockingKernelClient()
        kc.load_connection_file(str(kernel_dir(self.key) / "connection.json"))
        kc.start_channels()
        return kc

    def _await_cell_id(self, kc, msg_id: str, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = kc.get_iopub_msg(timeout=max(deadline - time.monotonic(), 0.1))
            except Exception:
                continue
            if (msg.get("parent_header", {}).get("msg_id") == msg_id
                    and msg["header"]["msg_type"] == "execute_input"):
                return int(msg["content"]["execution_count"])
        raise TimeoutError("kernel never acknowledged the cell (no execute_input)")

    def _follow(self, cell_id: int, timeout_s: float) -> Completed | Running:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                out, off = read_output_since(self.key, cell_id, 0)
                return Completed(cell_id, rec, out)
            time.sleep(0.2)
        out, off = read_output_since(self.key, cell_id, 0)
        return Running(cell_id, out, off)

    def exec_cell(self, code: str, timeout_s: float, config: Config) -> Completed | Running | Busy:
        kc = self._connect()
        try:
            msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
            cell_id = self._await_cell_id(kc, msg_id)
        finally:
            kc.stop_channels()
        return self._follow(cell_id, timeout_s)


def run_bootstrap(key: str, config: Config) -> None:
    """NOTE (T6): once exec_cell requires kernel-side current.json confirmation, the
    bootstrap cell cannot use it (the hooks it waits on are installed BY that very
    cell). run_bootstrap therefore submits directly and follows the shell-channel
    execute_reply instead — see _exec_raw below, added in T6."""
    payload = json.dumps({
        "key": key,
        "kernel_dir": str(kernel_dir(key)),
        "idle_hours": config.idle_hours,
        "max_concurrency": config.max_concurrency,
        "depth": config.depth,
        "max_depth": config.max_depth,
    })
    code = f"import ptc.runtime.bootstrap as _ptc_b; _ptc_b.install({payload!r})"
    out = KernelClient(key).exec_cell(code, timeout_s=60, config=config)
    if isinstance(out, Completed) and out.record.status == "ok":
        return
    detail = getattr(getattr(out, "record", None), "error", None) or getattr(out, "output", "")
    raise RuntimeError(f"ptc bootstrap failed in kernel {key}: {detail}")
```

Wire into `kernel.py` — in `ensure_kernel`, immediately before `return KernelInfo(key, proc.pid, conn, True, expired)` add:

```python
        from .client import run_bootstrap
        run_bootstrap(key, cfg)
```

Then restructure the tail of `ensure_kernel` so the whole birth is one transaction (F4) —
replace the block from `epoch = str(int(time.time()))` through the final `return` with:

```python
        epoch = str(int(time.time()))
        try:
            write_owner(key, Owner(proc.pid, proc_start_time(proc.pid),
                                   time.time(), secrets.token_hex(8), epoch))
            write_meta(key, kernel_key=key, claude_session_id=claude_session_id,
                       cwd=work, depth=cfg.depth, epoch=epoch)
            from .client import run_bootstrap
            run_bootstrap(key, cfg)
            (kd / "ready").write_text(epoch)   # ready means BOOTSTRAPPED
        except Exception:
            proc.kill()                        # never leak a detached kernel (F4)
            (kd / "owner.json").unlink(missing_ok=True)
            (kd / "ready").unlink(missing_ok=True)
            raise
        return KernelInfo(key, proc.pid, conn, True, expired)
```

- [ ] **Step 5: Run tests**

```bash
uv run --group dev pytest test/integration/test_cells_records.py test/integration/test_kernel_lifecycle.py -q
```
Expected: all pass. Note the bootstrap cell consumes execution_count 1, so the first user cell is 2 — the test asserts relative (`+1`) numbering only.

- [ ] **Step 6: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T5 — in-kernel bootstrap (tee, terminal records, watchdog) + cells + basic client"
```

---

### Task 6: Busy protocol, yield, `wait`, `interrupt` (the concurrency contract)

**Files:**
- Modify: `ptc-surface/ptc/src/ptc/client.py`
- Create: `ptc-surface/ptc/test/integration/test_busy_yield_wait.py`

**Interfaces:**
- Consumes: T5 `cells.*`, T3 `submit_lock`, T4 `kernel_alive`.
- Produces (final `KernelClient` API, used by T8/T9): `exec_cell(code, timeout_s, config) -> Completed | Running | Busy` (now with the atomic busy check), `wait_cell(cell_id, timeout_s, since=-1) -> Completed | Running` (Running here also covers `ORPHANED`: if the kernel is dead with no record, returns `Completed` with a synthetic record `status="error", error={"ename":"KernelDied",...}`), `interrupt() -> None`, `is_busy() -> Busy | None` (T6 review fix: returns the full `Busy(cell_id, reason)` — reasons: "running", "pending-unconfirmed", "lock-held"; downstream reads `.cell_id`/`.reason`).

- [ ] **Step 1: Write the failing tests**

`ptc-surface/ptc/test/integration/test_busy_yield_wait.py`:

```python
import os
import time

from ptc.client import Busy, Completed, KernelClient, Running
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_yield_wait_busy_interrupt(ptc_home):
    ensure_kernel("y1", cwd=str(ptc_home))
    cfg = Config.from_env()
    kc = KernelClient("y1")
    out = kc.exec_cell("import time\nprint('start', flush=True)\ntime.sleep(600)", timeout_s=3, config=cfg)
    assert isinstance(out, Running) and "start" in out.output

    # busy: a second exec must NOT queue
    out2 = KernelClient("y1").exec_cell("1+1", timeout_s=3, config=cfg)
    assert isinstance(out2, Busy) and out2.cell_id == out.cell_id

    # wait from a FRESH client object (fresh-adapter recovery), caller-held cursor
    w = KernelClient("y1").wait_cell(out.cell_id, timeout_s=2, since=out.next_offset)
    assert isinstance(w, Running) and w.output == ""      # no new output while sleeping

    kc.interrupt()
    w2 = KernelClient("y1").wait_cell(out.cell_id, timeout_s=15)
    assert isinstance(w2, Completed) and w2.record.status == "interrupted"

    out3 = KernelClient("y1").exec_cell("print(6*7)", timeout_s=30, config=cfg)
    assert isinstance(out3, Completed) and "42" in out3.output
    kill_kernel("y1")


def test_wait_on_archived_epoch_cell(ptc_home):
    """F3: a cell yielded before a restart settles from the archive, never from a
    new epoch's cell — ids are monotonic across epochs."""
    from ptc.kernel import restart_kernel
    ensure_kernel("y3", cwd=str(ptc_home))
    cfg = Config.from_env()
    out = KernelClient("y3").exec_cell("import time\nprint('old-epoch', flush=True)\ntime.sleep(600)",
                                       timeout_s=2, config=cfg)
    assert isinstance(out, Running)
    old_id = out.cell_id
    restart_kernel("y3")
    w = KernelClient("y3").wait_cell(old_id, timeout_s=5)
    assert isinstance(w, Completed)
    assert "previous kernel epoch" in w.output and "old-epoch" in w.output
    # monotonic: the new epoch's first user cell id is above the archived max
    out2 = KernelClient("y3").exec_cell("print('new')", timeout_s=30, config=cfg)
    assert out2.cell_id > old_id
    kill_kernel("y3")


def test_wait_on_dead_kernel_reports_kernel_died(ptc_home):
    info = ensure_kernel("y2", cwd=str(ptc_home))
    kc = KernelClient("y2")
    out = kc.exec_cell("import time; time.sleep(600)", timeout_s=2, config=Config.from_env())
    assert isinstance(out, Running)
    os.kill(info.pid, 9)
    time.sleep(0.5)
    w = KernelClient("y2").wait_cell(out.cell_id, timeout_s=3)
    assert isinstance(w, Completed) and w.record.error["ename"] == "KernelDied"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/integration/test_busy_yield_wait.py -q
```
Expected: FAIL (`wait_cell`/`interrupt` missing; busy path returns wrong type).

- [ ] **Step 3: Implement the full client**

Replace the `exec_cell` body and add methods in `ptc-surface/ptc/src/ptc/client.py`:

```python
# add imports at top:
import os
import signal

from .cells import current_cell, default_offset, save_offset  # (json, kernel_dir already imported)
from .kernel import kernel_alive
from .lock import submit_lock
from .ownership import read_owner


class KernelClient:
    # ... __init__, _connect, _await_cell_id, _follow unchanged ...

    # -- busy model (F2): a kernel is busy when the kernel-side current.json names a
    # cell with no terminal record, OR when a submitted request has not yet been
    # acknowledged (pending.json, written only on the confirm-failure path).
    def is_busy(self) -> int | None:
        cur = current_cell(self.key)
        from .cells import read_record
        if cur is not None and read_record(self.key, cur) is None and kernel_alive(self.key):
            return cur
        pend = kernel_dir(self.key) / "cells" / "pending.json"
        try:
            data = json.loads(pend.read_text())
            if time.time() - data.get("submitted_at", 0) < 60 and kernel_alive(self.key):
                return -1          # busy admitting an unacknowledged cell
            pend.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def exec_cell(self, code: str, timeout_s: float, config: Config) -> Completed | Running | Busy:
        """Atomic admission (F2): the submit lock is held from the busy check until the
        kernel-side pre_run_cell has published current.json for OUR cell. A loser that
        times out on the lock is told busy — a submitted request is never silently queued."""
        try:
            lock_cm = submit_lock(self.key)
            lock_cm.__enter__()
        except TimeoutError:
            return Busy(self.is_busy())
        try:
            busy = self.is_busy()
            if busy is not None:
                return Busy(busy)
            kc = self._connect()
            try:
                msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
                cell_id = self._await_cell_id(kc, msg_id)
                deadline = time.monotonic() + 15.0
                while current_cell(self.key) != cell_id:
                    if time.monotonic() >= deadline:
                        # fail closed: mark the unacknowledged submission so every
                        # later busy check sees it, then surface the fault (F2)
                        pend = kernel_dir(self.key) / "cells" / "pending.json"
                        pend.parent.mkdir(parents=True, exist_ok=True)
                        pend.write_text(json.dumps(
                            {"msg_id": msg_id, "submitted_at": time.time()}))
                        raise RuntimeError(
                            f"kernel {self.key} accepted cell {cell_id} but never "
                            "published it; the kernel may be wedged — interrupt() or "
                            "restart()")
                    time.sleep(0.02)
                (kernel_dir(self.key) / "cells" / "pending.json").unlink(missing_ok=True)
            finally:
                kc.stop_channels()
        finally:
            lock_cm.__exit__(None, None, None)
        return self._follow(cell_id, timeout_s)

    def _archived(self, cell_id: int) -> Completed | None:
        """A cell id from a previous kernel epoch (F3): settle it from the archive."""
        for d in sorted(kernel_dir(self.key).glob("cells-prev-*"), reverse=True):
            log = d / f"{cell_id}.log"
            if not log.exists():
                continue
            text = log.read_text(errors="replace")
            rec = None
            try:
                rec = json.loads((d / f"{cell_id}.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass
            if rec is None:
                rec = {"status": "error", "duration_ms": 0, "result_repr": None,
                       "error": {"ename": "KernelEpochEnded",
                                 "evalue": "the kernel restarted before this cell finished",
                                 "traceback": ""},
                       "images": [], "mutations": []}
            rec.setdefault("error", None)
            note = f"\n[cell {cell_id} belongs to a previous kernel epoch — archived at {d}]"
            return Completed(cell_id, CellRecord(**rec), text + note)
        return None

    def wait_cell(self, cell_id: int, timeout_s: float, since: int = -1) -> Completed | Running:
        offset = default_offset(self.key, cell_id) if since < 0 else since
        deadline = time.monotonic() + timeout_s
        while True:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                text, new_off = read_output_since(self.key, cell_id, offset)
                save_offset(self.key, cell_id, new_off)
                return Completed(cell_id, rec, text)
            if not (kernel_dir(self.key) / "cells" / f"{cell_id}.log").exists():
                arch = self._archived(cell_id)
                if arch is not None:
                    return arch
            if not kernel_alive(self.key):
                text, new_off = read_output_since(self.key, cell_id, offset)
                dead = CellRecord(status="error", duration_ms=0, result_repr=None,
                                  error={"ename": "KernelDied",
                                         "evalue": "kernel process died before the cell finished",
                                         "traceback": ""},
                                  images=[], mutations=[])
                return Completed(cell_id, dead, text)
            if time.monotonic() >= deadline:
                text, new_off = read_output_since(self.key, cell_id, offset)
                save_offset(self.key, cell_id, new_off)
                return Running(cell_id, text, new_off)
            time.sleep(0.2)

    def interrupt(self) -> None:
        """interrupt_request on the control channel, then SIGINT after a 2 s grace."""
        try:
            kc = self._connect()
            try:
                msg = kc.session.msg("interrupt_request", {})
                kc.control_channel.send(msg)
            finally:
                kc.stop_channels()
        except Exception:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.is_busy() is None:
                return
            time.sleep(0.1)
        o = read_owner(self.key)
        if o:
            try:
                os.kill(o.pid, signal.SIGINT)
            except OSError:
                pass

    def _exec_raw(self, code: str, timeout_s: float) -> Completed | Running:
        """Bootstrap-only path: no submit lock, no current.json wait (the hooks are
        installed by the very cell this runs). Follows the shell execute_reply."""
        kc = self._connect()
        try:
            msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
            cell_id = self._await_cell_id(kc, msg_id)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    reply = kc.get_shell_msg(timeout=1.0)
                except Exception:
                    continue
                if reply.get("parent_header", {}).get("msg_id") == msg_id:
                    ok = reply["content"].get("status") == "ok"
                    text, _ = read_output_since(self.key, cell_id, 0)
                    rec = read_record(self.key, cell_id) or CellRecord(
                        status="ok" if ok else "error", duration_ms=0, result_repr=None,
                        error=None if ok else {"ename": reply["content"].get("ename", "Error"),
                                               "evalue": reply["content"].get("evalue", ""),
                                               "traceback": ""},
                        images=[], mutations=[])
                    return Completed(cell_id, rec, text)
            return Running(cell_id, "", 0)
        finally:
            kc.stop_channels()
```

And switch `run_bootstrap` to it: `out = KernelClient(key)._exec_raw(code, timeout_s=60)`.

- [ ] **Step 4: Run tests (including a re-run of T4/T5 suites — the protocol touched shared code)**

```bash
uv run --group dev pytest test/integration -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T6 — atomic busy check, yield/wait with caller cursors, interrupt"
```

---

### Task 7: Result shaping (`shape.py`)

**Files:**
- Create: `ptc-surface/ptc/src/ptc/shape.py`
- Create: `ptc-surface/ptc/test/unit/test_shape.py`

**Interfaces:**
- Consumes: `client.Completed/Running/Busy`, `cells.CellRecord`, `runtime/audit.entries_for_cell`, `paths.Config`, `paths.MAX_OUTPUT_CLAMP`.
- Produces: `shape.Rendered(text: str, images: list[Path])`, `shape.render(outcome, key, config, degraded=False) -> Rendered`, `shape.to_dict(outcome, key) -> dict`, `shape.footer_line(mutations: list[dict]) -> str | None`.
- Rendering contract (spec "Result shaping"): header `[cell N · STATUS · D.Ds]` (+ ` · [keying: adapter-local]` when degraded); body = interleaved stream log (truncated head+tail); `→ result: <repr>` when present; footer from mutations. Truncation marker: `… [truncated N chars — full output: <path>/cells/<id>.log]`. `Running` renders status `running` + a `wait` hint; `Busy` renders `busy` + the running cell id + guidance.

- [ ] **Step 1: Write the failing tests**

`ptc-surface/ptc/test/unit/test_shape.py`:

```python
from ptc.cells import CellRecord
from ptc.client import Busy, Completed, Running
from ptc.paths import Config
from ptc.shape import footer_line, render


def _rec(**kw):
    base = dict(status="ok", duration_ms=1234, result_repr=None, error=None, images=[], mutations=[])
    base.update(kw)
    return CellRecord(**base)


def test_render_completed_ok():
    out = Completed(14, _rec(result_repr="42"), "hello\n")
    r = render(out, "k", Config.from_env(env={}))
    assert r.text.startswith("[cell 14 · ok · 1.2s]")
    assert "hello" in r.text and "→ result: 42" in r.text


def test_render_truncates_head_tail():
    big = "x" * 100_000
    out = Completed(3, _rec(), big)
    cfg = Config.from_env(env={"PTC_MAX_OUTPUT_CHARS": "1000"})
    r = render(out, "k", cfg)
    assert len(r.text) < 3000
    assert "[truncated" in r.text and "cells/3.log" in r.text
    assert r.text.count("x" * 100) >= 2          # head and tail both survive


def test_render_running_busy_and_degraded():
    r = render(Running(5, "partial", 77), "k", Config.from_env(env={}))
    assert "[cell 5 · running" in r.text and "wait" in r.text and "77" in r.text
    b = render(Busy(9), "k", Config.from_env(env={}), degraded=True)
    assert "busy" in b.text and "9" in b.text and "[keying: adapter-local]" in b.text


def test_footer_and_error():
    ms = [{"kind": "edit", "path": "src/a.py", "added": 3, "removed": 1},
          {"kind": "write", "path": "out.md", "added": 10},
          {"kind": "bash", "command": "npm test"}]
    f = footer_line(ms)
    assert "edited src/a.py (+3/−1)" in f and "wrote out.md" in f and "ran: npm test" in f
    out = Completed(2, _rec(status="error",
                            error={"ename": "ValueError", "evalue": "bad", "traceback": "tb"}), "")
    r = render(out, "k", Config.from_env(env={}))
    assert "[cell 2 · error" in r.text and "ValueError: bad" in r.text
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/unit/test_shape.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement `shape.py`**

```python
"""Render kernel outcomes into the model-facing text (shared by MCP and CLI)."""
from dataclasses import dataclass
from pathlib import Path

from .cells import CellRecord
from .client import Busy, Completed, Running
from .paths import Config, cells_dir


@dataclass
class Rendered:
    text: str
    images: list


def _truncate(text: str, cap: int, full_log: Path) -> str:
    if len(text) <= cap:
        return text
    head = text[: int(cap * 0.6)]
    tail = text[-int(cap * 0.4):]
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n… [truncated {cut} chars — full output: {full_log}]\n{tail}"


def footer_line(mutations: list) -> str | None:
    parts = []
    for m in mutations:
        k = m.get("kind")
        if k == "edit":
            parts.append(f"edited {m.get('path')} (+{m.get('added', 0)}/−{m.get('removed', 0)})")
        elif k == "write":
            parts.append(f"wrote {m.get('path')}")
        elif k == "bash":
            parts.append(f"ran: {m.get('command', '')[:80]}")
        elif k == "agent":
            parts.append(f"spawned agent \"{m.get('name', '?')}\"")
    return " · ".join(parts) if parts else None


def _header(cell_id, status: str, dur_ms: int | None, degraded: bool) -> str:
    dur = f" · {dur_ms / 1000:.1f}s" if dur_ms is not None else ""
    deg = " · [keying: adapter-local]" if degraded else ""
    return f"[cell {cell_id} · {status}{dur}{deg}]"


def render(outcome, key: str, config: Config, degraded: bool = False) -> Rendered:
    log_path = cells_dir(key)
    if isinstance(outcome, Busy):
        which = ("a just-submitted cell is being admitted" if (outcome.cell_id in (None, -1))
                 else f"cell {outcome.cell_id} is still running")
        return Rendered(
            f"[kernel busy{' · [keying: adapter-local]' if degraded else ''}] "
            f"{which}. "
            + (f"Use wait(cell_id={outcome.cell_id}) for its output, " if outcome.cell_id not in (None, -1) else "")
            + "interrupt() to stop it, or resubmit after it finishes. Nothing was queued.", [])
    if isinstance(outcome, Running):
        body = _truncate(outcome.output, config.max_output_chars, log_path / f"{outcome.cell_id}.log")
        return Rendered(
            f"{_header(outcome.cell_id, 'running', None, degraded)}\n{body}\n"
            f"[still running — call wait(cell_id={outcome.cell_id}, since={outcome.next_offset}) "
            "for more output, or interrupt() to stop]", [])
    rec: CellRecord = outcome.record
    lines = [_header(outcome.cell_id, rec.status, rec.duration_ms, degraded)]
    body = _truncate(outcome.output, config.max_output_chars, log_path / f"{outcome.cell_id}.log")
    if body:
        lines.append(body.rstrip("\n"))
    if rec.status == "error" and rec.error and rec.error.get("ename") not in (None, ""):
        if rec.error["ename"] not in outcome.output:
            lines.append(f"{rec.error['ename']}: {rec.error.get('evalue', '')}")
    if rec.result_repr is not None:
        lines.append(f"→ result: {rec.result_repr}")
    f = footer_line(rec.mutations)
    if f:
        lines.append(f)
    return Rendered("\n".join(lines), [Path(p) for p in rec.images if Path(p).exists()])


def to_dict(outcome, key: str) -> dict:
    if isinstance(outcome, Busy):
        return {"status": "busy", "cell_id": outcome.cell_id}
    if isinstance(outcome, Running):
        return {"status": "running", "cell_id": outcome.cell_id,
                "output": outcome.output, "next_offset": outcome.next_offset}
    r = outcome.record
    return {"status": r.status, "cell_id": outcome.cell_id, "duration_ms": r.duration_ms,
            "output": outcome.output, "result_repr": r.result_repr, "error": r.error,
            "images": r.images, "mutations": r.mutations,
            "full_log": str(cells_dir(key) / f"{outcome.cell_id}.log")}
```

- [ ] **Step 4: Run tests, then commit**

```bash
uv run --group dev pytest test/unit/test_shape.py -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T7 — result shaping: header, head+tail truncation, mutation footer"
```

---

### Task 8: The MCP adapter (`mcp.py`)

**Files:**
- Create: `ptc-surface/ptc/src/ptc/mcp.py`
- Create: `ptc-surface/ptc/test/integration/test_mcp_tools.py`

**Interfaces:**
- Consumes: `KernelClient`, `ensure_kernel/restart_kernel/list_kernels`, `shape.render`, `discovery.resolve` — until T13 lands, `resolve` here is a **stub in this task** with the final signature returning `Resolved(key, source, claude_session_id, cwd, degraded)` from `explicit` / `PTC_SESSION` / `CLAUDE_CODE_SESSION_ID` / adapter-local only (no run-file yet).
- Produces: FastMCP server `ptc` with tools `exec`, `wait`, `interrupt`, `restart`, `kernels`; `main()` entry (console script `ptc-mcp`); `INSTRUCTIONS` string. Tool handlers are plain async functions importable for tests (`from ptc.mcp import exec_tool, wait_tool, ...`).

- [ ] **Step 1: Write the failing integration test (calls handlers directly — no MCP transport needed)**

`ptc-surface/ptc/test/integration/test_mcp_tools.py`:

```python
import asyncio

from ptc.kernel import kill_kernel
from ptc.mcp import exec_tool, interrupt_tool, kernels_tool, restart_tool, wait_tool


def _run(coro):
    return asyncio.run(coro)


def test_exec_wait_interrupt_roundtrip(ptc_home):
    r = _run(exec_tool(code="x = 21\nprint('a' * 10)", session="m1", timeout_s=60))
    assert "[cell" in r[0].text and "ok" in r[0].text and "aaaaaaaaaa" in r[0].text

    r2 = _run(exec_tool(code="import time; time.sleep(300)", session="m1", timeout_s=2))
    assert "running" in r2[0].text
    cell_id = int(r2[0].text.split("cell ")[1].split(" ")[0])

    r3 = _run(exec_tool(code="1", session="m1", timeout_s=2))
    assert "busy" in r3[0].text and "Nothing was queued" in r3[0].text

    _run(interrupt_tool(session="m1"))
    r4 = _run(wait_tool(cell_id=cell_id, session="m1", timeout_s=15))
    assert "interrupted" in r4[0].text

    r5 = _run(exec_tool(code="print(x * 2)", session="m1", timeout_s=60))
    assert "42" in r5[0].text

    rows = _run(kernels_tool())
    assert "m1" in rows[0].text

    r6 = _run(restart_tool(session="m1"))
    assert "restart" in r6[0].text.lower()
    r7 = _run(exec_tool(code="print('x' in dir())", session="m1", timeout_s=60))
    assert "False" in r7[0].text            # namespace really was lost
    kill_kernel("m1")


def test_truncation_and_clamp(ptc_home):
    r = _run(exec_tool(code="print('y' * 100_000)", session="m2",
                       timeout_s=60, max_output_chars=999_999))
    text = r[0].text
    assert len(text) < 60_000               # server clamp at 50k held
    assert "[truncated" in text and ".log" in text
    kill_kernel("m2")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/integration/test_mcp_tools.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement `mcp.py`**

```python
"""The ptc MCP server (stdio). Tools: exec, wait, interrupt, restart, kernels."""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from .client import KernelClient
from .kernel import ensure_kernel, kill_kernel, list_kernels, restart_kernel
from .paths import MAX_OUTPUT_CLAMP, Config, safe_key
from .shape import render

INSTRUCTIONS = """\
ptc is a persistent IPython kernel for this session. Namespace (variables, imports,
functions, agent handles) persists across calls, turns, compaction, and --resume,
until the kernel's idle TTL. Assign large results to variables and print compact
summaries; output truncates with a full-log path. Pre-bound: read, write, edit,
bash, agent, llm, web_fetch, web_search, history, workflow (all Python; async ones
are awaited at top level). If a cell yields `running`, use wait(cell_id); if the
kernel is busy, wait or interrupt — nothing queues. Pass session="<id>" explicitly
if results ever look like a different session's namespace.
"""

server = FastMCP("ptc", instructions=INSTRUCTIONS)


def _resolve(explicit: str | None):
    """T8 stub — replaced by discovery.resolve in T13. Same return contract."""
    from dataclasses import dataclass

    @dataclass
    class Resolved:
        key: str
        source: str
        claude_session_id: str | None
        cwd: str | None
        degraded: bool

    if explicit:
        return Resolved(safe_key(explicit), "explicit", explicit, None, False)
    env_s = os.environ.get("PTC_SESSION")
    if env_s:
        return Resolved(safe_key(env_s), "env", env_s, None, False)
    cc = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if cc:
        return Resolved(safe_key(cc), "cc-env", cc, None, False)
    return Resolved(f"adapter-{os.getpid()}", "adapter-local", None, None, True)


def _content(rendered) -> list:
    out = [TextContent(type="text", text=rendered.text)]
    budget = 4_000_000 - len(rendered.text)
    for p in rendered.images[:2]:
        data = Path(p).read_bytes()
        if len(data) * 1.4 > budget:      # base64 inflation
            break
        import base64
        mime = "image/png" if str(p).endswith("png") else "image/jpeg"
        out.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime))
        budget -= int(len(data) * 1.4)
    return out


def _cfg(timeout_s: float, max_output_chars: int) -> Config:
    cfg = Config.from_env()
    cfg.yield_s = timeout_s
    cfg.max_output_chars = min(int(max_output_chars), MAX_OUTPUT_CLAMP)
    return cfg


async def exec_tool(code: str, session: str | None = None,
                    timeout_s: float = 300, max_output_chars: int = 12_000) -> list:
    r = _resolve(session)
    cfg = _cfg(timeout_s, max_output_chars)
    info = ensure_kernel(r.key, cwd=r.cwd, claude_session_id=r.claude_session_id, config=cfg)
    outcome = KernelClient(r.key).exec_cell(code, timeout_s=timeout_s, config=cfg)
    rendered = render(outcome, r.key, cfg, degraded=r.degraded)
    if info.expired_notice:
        rendered.text = (f"[previous kernel expired: {info.expired_notice.strip()} — fresh "
                         f"namespace; agent sessions remain resumable via agent.list()]\n"
                         + rendered.text)
    return _content(rendered)


async def wait_tool(cell_id: int, session: str | None = None,
                    timeout_s: float = 300, max_output_chars: int = 12_000,
                    since: int = -1) -> list:
    r = _resolve(session)
    cfg = _cfg(timeout_s, max_output_chars)
    outcome = KernelClient(r.key).wait_cell(cell_id, timeout_s=timeout_s, since=since)
    return _content(render(outcome, r.key, cfg, degraded=r.degraded))


async def interrupt_tool(session: str | None = None) -> list:
    r = _resolve(session)
    KernelClient(r.key).interrupt()
    return [TextContent(type="text", text=f"[interrupt sent to kernel {r.key}]")]


async def restart_tool(session: str | None = None) -> list:
    r = _resolve(session)
    restart_kernel(r.key, cwd=r.cwd, claude_session_id=r.claude_session_id)
    return [TextContent(type="text", text=(
        f"[kernel {r.key} restarted — the Python namespace was lost; variables and imports "
        "must be recreated. Agent sessions remain resumable via agent.list().]"))]


async def kernels_tool() -> list:
    rows = list_kernels()
    import datetime
    def _ts(v):
        return datetime.datetime.fromtimestamp(v).strftime("%m-%d %H:%M") if v else "-"
    lines = [f"{r['key']}  pid={r['pid']}  alive={r['alive']}  depth={r['depth']}  "
             f"last_used={_ts(r.get('last_used'))}  cwd={r['cwd']}"
             for r in rows] or ["(no kernels)"]
    return [TextContent(type="text", text="\n".join(lines))]


for fn, name in ((exec_tool, "exec"), (wait_tool, "wait"), (interrupt_tool, "interrupt"),
                 (restart_tool, "restart"), (kernels_tool, "kernels")):
    server.tool(name=name)(fn)


def main() -> None:
    server.run()          # stdio transport


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```bash
uv run --group dev pytest test/integration/test_mcp_tools.py -q
```
Expected: 2 passed. If `FastMCP`'s `tool()` registration API differs in the installed `mcp` version, adapt the registration loop (check `python -c "import mcp; print(mcp.__version__)"` and the `mcp.server.fastmcp` docstrings) — the handler functions and their contracts must not change.

- [ ] **Step 5: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T8 — FastMCP adapter: exec/wait/interrupt/restart/kernels"
```

---

### Task 9: The `ptc` CLI

**Files:**
- Create: `ptc-surface/ptc/src/ptc/cli.py`
- Create: `ptc-surface/ptc/test/integration/test_cli.py`

**Interfaces:**
- Consumes: T8's `_resolve` contract (via a shared helper — move `_resolve` usage behind `ptc.mcp._resolve` import for now; T13 replaces both call sites with `discovery.resolve`), `KernelClient`, `shape.render/to_dict`, `venv.ensure_venv`, `kernel.list_kernels/kill_kernel/restart_kernel`.
- Produces: console script `ptc` with subcommands `setup`, `exec`, `wait`, `interrupt`, `restart`, `list`, `kill`, `doctor`; flags `-s/--session`, `-t/--timeout`, `--json`; `exec` reads code from the positional arg or stdin when the arg is `-`. CLI default session: `$PTC_SESSION` → `$CLAUDE_CODE_SESSION_ID` → newest live kernel (with a printed notice).

- [ ] **Step 1: Write the failing test**

`ptc-surface/ptc/test/integration/test_cli.py`:

```python
import json
import os
import subprocess
import sys


def _cli(*args, env_extra=None, input_=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "ptc.cli", *args],
                          capture_output=True, text=True, env=env, input=input_, timeout=120)


def test_cli_exec_shares_kernel_and_json(ptc_home):
    env = {"PTC_SESSION": "cli1"}
    r = _cli("exec", "x = 6 * 7", env_extra=env)
    assert r.returncode == 0, r.stderr
    r2 = _cli("exec", "-", env_extra=env, input_="print(x)")
    assert "42" in r2.stdout
    r3 = _cli("exec", "--json", "x + 1", env_extra=env)
    d = json.loads(r3.stdout)
    assert d["status"] == "ok" and d["result_repr"] == "43"
    r4 = _cli("list", env_extra=env)
    assert "cli1" in r4.stdout
    r5 = _cli("kill", "-s", "cli1", env_extra=env)
    assert r5.returncode == 0


def test_cli_newest_kernel_fallback_prints_notice(ptc_home):
    _cli("exec", "1", env_extra={"PTC_SESSION": "cli2"})
    r = _cli("exec", "2", env_extra={})   # no session env at all
    assert "cli2" in r.stdout or "cli2" in r.stderr   # notice names the picked kernel
    _cli("kill", "-s", "cli2")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/integration/test_cli.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement `cli.py`**

```python
"""The ptc CLI. Same substrate as the MCP adapter; text or --json output."""
import argparse
import json
import os
import sys

from .client import KernelClient
from .kernel import ensure_kernel, kill_kernel, list_kernels, restart_kernel
from .paths import Config, safe_key
from .shape import render, to_dict
from .venv import ensure_venv


def _pick_session(explicit: str | None) -> tuple[str, str | None]:
    """Returns (key, notice)."""
    if explicit:
        return safe_key(explicit), None
    for var in ("PTC_SESSION", "CLAUDE_CODE_SESSION_ID"):
        v = os.environ.get(var)
        if v:
            return safe_key(v), None
    live = [r for r in list_kernels() if r["alive"]]
    if live:
        newest = max(live, key=lambda r: r["spawned_at"] or 0)
        return newest["key"], f"[no session given — using newest live kernel: {newest['key']}]"
    return f"cli-{os.getpid()}", "[no session given and no live kernel — using a fresh local key]"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ptc")
    sub = p.add_subparsers(dest="cmd", required=True)

    def com(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("-s", "--session", default=None)
        sp.add_argument("-t", "--timeout", type=float, default=300.0)
        sp.add_argument("--json", action="store_true")
        return sp

    com("setup")
    sp = com("exec"); sp.add_argument("code")
    sp = com("wait"); sp.add_argument("cell_id", type=int); sp.add_argument("--since", type=int, default=-1)
    com("interrupt"); com("restart"); com("list"); com("kill"); com("doctor")
    a = p.parse_args(argv)

    if a.cmd == "setup":
        ensure_venv()
        print("ptc venv ready:", ensure_venv())
        return 0
    if a.cmd == "list":
        for r in list_kernels():
            print(f"{r['key']}  pid={r['pid']}  alive={r['alive']}  cwd={r['cwd']}")
        return 0
    if a.cmd == "doctor":
        import shutil
        print(json.dumps({"venv": str(ensure_venv()), "uv": shutil.which("uv"),
                          "claude": shutil.which("claude"), "codex": shutil.which("codex"),
                          "PTC_SESSION": os.environ.get("PTC_SESSION"),
                          "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID")}, indent=2))
        return 0

    key, notice = _pick_session(a.session)
    if notice:
        print(notice, file=sys.stderr)
    if a.cmd == "kill":
        return 0 if kill_kernel(key) else 1
    if a.cmd == "restart":
        restart_kernel(key)
        print(f"[kernel {key} restarted — namespace lost]")
        return 0
    if a.cmd == "interrupt":
        KernelClient(key).interrupt()
        print(f"[interrupt sent to {key}]")
        return 0

    cfg = Config.from_env()
    cfg.yield_s = a.timeout
    if a.cmd == "exec":
        code = sys.stdin.read() if a.code == "-" else a.code
        info = ensure_kernel(key, config=cfg)
        outcome = KernelClient(key).exec_cell(code, timeout_s=a.timeout, config=cfg)
    else:  # wait
        outcome = KernelClient(key).wait_cell(a.cell_id, timeout_s=a.timeout, since=a.since)
        info = None
    if a.json:
        print(json.dumps(to_dict(outcome, key)))
    else:
        if info is not None and info.expired_notice:
            print(f"[previous kernel expired: {info.expired_notice.strip()}]")
        print(render(outcome, key, cfg).text)
    from .client import Completed
    return 0 if not isinstance(outcome, Completed) or outcome.record.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, then commit**

```bash
uv run --group dev pytest test/integration/test_cli.py -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T9 — ptc CLI with session fallback chain and --json"
```

---

### Task 10: Spike S1 — claude-agent-sdk inside the kernel (LIVE)

**Spike task — the deliverable is knowledge.** Question: can `claude-agent-sdk` run on ipykernel's asyncio loop — concurrency, cancellation, CLI-death recovery, semaphore hygiene — and is `nest_asyncio` needed at all?

Spec promote/discard criteria, verbatim: *"Promote if all complete and the kernel stays responsive. Fallback (pre-designed): run all SDK I/O on one dedicated background thread with its own loop — the spike must then also exercise the cross-thread handle protocol (`spawn` on the kernel loop, `.result()` awaited from a cell)."*

**Files:**
- Create: `ptc-surface/ptc/test/spikes/s1_sdk_in_kernel.py`

- [ ] **Step 1: Build the probe** (`test/spikes/s1_sdk_in_kernel.py`):

```python
"""S1: run claude-agent-sdk INSIDE a real ptc kernel. Requires PTC_LIVE=1 and `claude` login.

Usage:  PTC_LIVE=1 uv run --group dev python test/spikes/s1_sdk_in_kernel.py
"""
import os
import sys
import textwrap

sys.path.insert(0, "src")
from ptc.client import Completed, KernelClient  # noqa: E402
from ptc.kernel import ensure_kernel, kill_kernel  # noqa: E402
from ptc.paths import Config  # noqa: E402

KEY = "spike-s1"
CASES = {
    "one_shot": """
        import asyncio, time
        from claude_agent_sdk import query, ClaudeAgentOptions
        async def one():
            texts = []
            async for m in query(prompt="Reply with exactly: PONG",
                                 options=ClaudeAgentOptions(max_turns=1, tools=[],
                                                            permission_mode="bypassPermissions")):
                texts.append(type(m).__name__)
            return texts
        r = await one()
        print("ONE_SHOT_OK", r[-1])
    """,
    "two_concurrent": """
        async def ask(word):
            out = ""
            async for m in query(prompt=f"Reply with exactly: {word}",
                                 options=ClaudeAgentOptions(max_turns=1, tools=[],
                                                            permission_mode="bypassPermissions")):
                if hasattr(m, "content"):
                    for b in m.content:
                        out += getattr(b, "text", "")
            return out
        a, b = await asyncio.gather(ask("ALPHA"), ask("BETA"))
        print("CONCURRENT_OK", "ALPHA" in a, "BETA" in b)
    """,
    "cancel_midstream": """
        t = asyncio.ensure_future(ask("GAMMA " * 200))
        await asyncio.sleep(2)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            print("CANCEL_OK")
        # kernel still responsive?
        print("STILL_ALIVE", 1 + 1)
    """,
    "client_lifecycle": """
        # Two REAL ClaudeSDKClient sessions: connect, first turn, follow-up send,
        # interrupt on one, disconnect both, and no leaked claude processes (F7).
        import subprocess as _sp, os as _os
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

        def _kids():
            out = _sp.run(["pgrep", "-P", str(_os.getpid())], capture_output=True, text=True)
            return set(out.stdout.split())

        base = _kids()
        async def session_flow(word):
            c = ClaudeSDKClient(options=ClaudeAgentOptions(
                max_turns=1, tools=[], permission_mode="bypassPermissions"))
            await c.connect()
            await c.query(f"Reply with exactly: {word}")
            got = ""
            async for m in c.receive_response():
                for b in getattr(m, "content", []) or []:
                    got += getattr(b, "text", "")
            await c.query(f"Now reply with exactly: {word}2")
            got2 = ""
            async for m in c.receive_response():
                for b in getattr(m, "content", []) or []:
                    got2 += getattr(b, "text", "")
            await c.disconnect()
            return word in got, f"{word}2" in got2
        r1, r2 = await asyncio.gather(session_flow("RED"), session_flow("BLUE"))
        print("CLIENTS_OK", r1, r2)
        await asyncio.sleep(1)
        leaked = _kids() - base
        print("LEAKED", sorted(leaked))
    """,
    "client_interrupt": """
        c = ClaudeSDKClient(options=ClaudeAgentOptions(
            tools=[], permission_mode="bypassPermissions"))
        await c.connect()
        await c.query("Count slowly from 1 to 500, one number per line.")
        await asyncio.sleep(2)
        await c.interrupt()
        try:
            async for m in c.receive_response():
                pass
        except Exception as e:
            print("INTERRUPT_RAISED", type(e).__name__)
        await c.disconnect()
        print("INTERRUPT_OK")
    """,
    "kill_cli_midstream": """
        import subprocess, signal
        async def ask_with_kill():
            task = asyncio.ensure_future(ask("DELTA"))
            await asyncio.sleep(1.5)
            # kill every `claude` child of this kernel
            me = str(__import__("os").getpid())
            out = subprocess.run(["pgrep", "-P", me], capture_output=True, text=True).stdout.split()
            for pid in out:
                try: __import__("os").kill(int(pid), signal.SIGKILL)
                except Exception: pass
            try:
                return await asyncio.wait_for(task, timeout=20)
            except (asyncio.TimeoutError, Exception) as e:
                return f"RAISED {type(e).__name__}"
        print("KILL_RESULT", await ask_with_kill())
        print("STILL_ALIVE_2", 2 + 2)
    """,
}


def main():
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP: set PTC_LIVE=1"); return
    ensure_kernel(KEY, cwd=os.getcwd())
    kc = KernelClient(KEY)
    cfg = Config.from_env()
    for name, code in CASES.items():
        out = kc.exec_cell(textwrap.dedent(code), timeout_s=180, config=cfg)
        status = out.record.status if isinstance(out, Completed) else "TIMEOUT/RUNNING"
        print(f"=== {name}: {status}\n{getattr(out, 'output', '')[:1500]}\n")
    kill_kernel(KEY)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

```bash
cd ptc-surface/ptc && PTC_LIVE=1 uv run --group dev python test/spikes/s1_sdk_in_kernel.py
```
Observe: each case's status line; whether `ONE_SHOT_OK`/`CONCURRENT_OK`/`CANCEL_OK`/`CLIENTS_OK`/`INTERRUPT_OK` print and `LEAKED` is empty; whether `KILL_RESULT` returns (RAISED is fine — a hang past the 20 s wait_for is the failure mode); whether `STILL_ALIVE*` print (kernel loop healthy). Note: the kernel bootstrap does NOT apply `nest_asyncio` — if `await` at top level plus SDK internals work as-is, nest_asyncio is unnecessary; record that.

- [ ] **Step 3: Record the verdict and route it**

Apply the criteria: all cases pass → **promote** (T20 builds directly on the kernel loop). Any hang/failure → **fallback**: extend the spike with the background-thread variant (run `asyncio.new_event_loop()` in a `threading.Thread`, submit coroutines with `asyncio.run_coroutine_threadsafe`, bridge `.result()` via `asyncio.wrap_future`), rerun the same four cases through it, and T20 implements `claude_backend` on that thread.
Append the verdict (with the exact printed evidence lines) to the spec's `## Surprises & Discoveries`:

```markdown
- Observation: [S1 verdict] claude-agent-sdk <ran cleanly / required the thread fallback> inside
  ipykernel's loop; nest_asyncio <was not needed / was needed because …>; CLI-death <raised
  within the deadline / hung and requires process-tree kill>.
  Evidence: test/spikes/s1_sdk_in_kernel.py output — <paste the four status lines>.
```

- [ ] **Step 4: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc ptc-surface/docs
git commit -m "f5(ptc): T10 — spike S1: SDK-in-kernel verdict recorded"
```

---

# Milestone M1 — surface

### Task 11: Plugin packaging: launcher, hook, hooks.json, plugin.json, .mcp.json + Spike S3 (LIVE)

Two deliverables: the checked-in plugin skeleton (stdlib-only launcher + hook), and the S3 discovery spike run against release Claude Code.

**Files:**
- Create: `ptc-surface/ptc/plugin/.claude-plugin/plugin.json`
- Create: `ptc-surface/ptc/plugin/.mcp.json`
- Create: `ptc-surface/ptc/plugin/bin/ptc-launch` (mode 755)
- Create: `ptc-surface/ptc/plugin/hooks/hooks.json`
- Create: `ptc-surface/ptc/plugin/hooks/session_start.py`
- Create: `ptc-surface/ptc/test/unit/test_hook_script.py`

**Interfaces:**
- Produces: run-file contract `~/.ptc/run/claude-<claude-pid>.json = {"session_id","cwd","written_at"}` (consumed by T13); env `PTC_LAUNCHER` (absolute path of `ptc-launch`, set by the launcher before exec — consumed by T20's child `mcp_servers`); the launcher's stamp check must equal `ptc.venv.stamp_payload()` (T2).

- [ ] **Step 1: Write the plugin files**

`plugin/.claude-plugin/plugin.json`:

```json
{
  "name": "ptc",
  "description": "Programmatic Tool Calling — a persistent per-session IPython kernel with Claude-Code-equivalent tools as Python functions",
  "version": "0.1.0",
  "author": {"name": "SSFSKIM"}
}
```

`plugin/.mcp.json`:

```json
{
  "mcpServers": {
    "ptc": {
      "command": "${CLAUDE_PLUGIN_ROOT}/bin/ptc-launch"
    }
  }
}
```

`plugin/hooks/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py\""}
        ]
      }
    ]
  }
}
```

`plugin/hooks/session_start.py` (stdlib only — the venv may not exist yet):

```python
#!/usr/bin/env python3
"""SessionStart hook: record {session_id, cwd} keyed by the nearest `claude` ancestor pid.

Hooks are launched through a shell, so os.getppid() may be a transient `sh` —
walk the ancestor chain until the command name contains "claude" (spec: keying #2).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def parent_of(pid: int) -> tuple[int, str] | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        parts = out.split(None, 1)
        if len(parts) == 2:
            return int(parts[0]), parts[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def find_claude_ancestor() -> int | None:
    pid = os.getpid()
    for _ in range(12):
        info = parent_of(pid)
        if info is None:
            return None
        ppid, _comm = info
        if ppid <= 1:
            return None
        pinfo = parent_of(ppid)
        comm_of_ppid = ""
        if pinfo is not None:
            pass
        # name of ppid itself:
        try:
            comm_of_ppid = subprocess.run(["ps", "-o", "comm=", "-p", str(ppid)],
                                          capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if "claude" in os.path.basename(comm_of_ppid):
            return ppid
        pid = ppid
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    sid = data.get("session_id")
    cwd = data.get("cwd") or os.getcwd()
    target = find_claude_ancestor()
    if not sid or not target:
        return 0
    rd = Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc")) / "run"
    rd.mkdir(parents=True, exist_ok=True)
    tmp = rd / f".claude-{target}.tmp"
    tmp.write_text(json.dumps({"session_id": sid, "cwd": cwd, "written_at": time.time()}))
    tmp.replace(rd / f"claude-{target}.json")
    for f in rd.glob("claude-*.json"):        # GC dead-pid files
        try:
            pid = int(f.stem.split("-", 1)[1])
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            f.unlink(missing_ok=True)
        except PermissionError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`plugin/bin/ptc-launch` (stdlib only; `chmod 755`):

```python
#!/usr/bin/env python3
"""Provision ~/.ptc/venv if missing/stale, then exec the ptc MCP adapter from it.

Stamp payload MUST stay byte-identical to ptc.venv.stamp_payload().
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc"))
VENV = HOME / "venv"
PY = VENV / "bin" / "python"
STAMP = VENV / ".ptc-version"
PKG = Path(__file__).resolve().parent.parent.parent      # plugin/bin/ptc-launch -> ptc/


def payload() -> dict:
    sha = hashlib.sha256((PKG / "pyproject.toml").read_bytes()).hexdigest()
    return {"schema": 1, "pyproject_sha": sha, "pkg": str(PKG)}


def current() -> bool:
    try:
        return PY.exists() and json.loads(STAMP.read_text()) == payload()
    except (OSError, json.JSONDecodeError):
        return False


def provision() -> None:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    subprocess.run([uv, "venv", str(VENV), "--python", "3.12", "--seed"], check=True)
    subprocess.run([uv, "pip", "install", "--python", str(PY), "-e", f"{PKG}[kernel]"], check=True)
    STAMP.write_text(json.dumps(payload()))


def main() -> None:
    if not current():
        lock = HOME / "provision.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
            try:
                provision()
            finally:
                lock.rmdir()
        except FileExistsError:
            for _ in range(1200):
                if not lock.exists():
                    break
                time.sleep(0.5)
            if not current():
                print("ptc-launch: venv stale and provisioning lock held; "
                      f"remove {lock} if nothing is provisioning", file=sys.stderr)
                sys.exit(1)
    os.environ["PTC_LAUNCHER"] = os.path.abspath(__file__)
    os.execv(str(PY), [str(PY), "-m", "ptc.mcp"])


if __name__ == "__main__":
    main()
```

```bash
chmod 755 ptc-surface/ptc/plugin/bin/ptc-launch ptc-surface/ptc/plugin/hooks/session_start.py
```

- [ ] **Step 2: Unit-test the hook's run-file write (fake `ps` not needed — test the file contract)**

`test/unit/test_hook_script.py`:

```python
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent.parent / "plugin" / "hooks" / "session_start.py"


def test_hook_writes_runfile_for_claude_ancestor(tmp_path, monkeypatch):
    """Run the hook under a fake `claude` parent: sh -c 'python3 hook' whose parent we rename.
    We can't rename processes portably, so instead verify the no-claude-ancestor path is a
    clean no-op, and the write path via direct function import."""
    env = {**os.environ, "PTC_HOME": str(tmp_path)}
    r = subprocess.run(["python3", str(HOOK)], input='{"session_id": "s-1", "cwd": "/w"}',
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0                     # never breaks session start
    # direct-write contract (bypassing ancestor search):
    sys.path.insert(0, str(HOOK.parent))
    import importlib
    m = importlib.import_module("session_start")
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: 4242)
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"session_id":"s-2","cwd":"/w2"}'))
    assert m.main() == 0
    f = json.loads((tmp_path / "run" / "claude-4242.json").read_text())
    assert f["session_id"] == "s-2" and f["cwd"] == "/w2"
```

Run: `uv run --group dev pytest test/unit/test_hook_script.py -q` → expected FAIL first (files missing), then PASS after Step 1.

- [ ] **Step 3: Spike S3 — run live against release Claude Code**

Question: does the tree-walk key match the adapter's view, across fresh start, `--resume`, and two concurrent windows?

```bash
cd ptc-surface/ptc
# scenario 1: fresh session
claude -p --plugin-dir ./plugin --permission-mode bypassPermissions \
  "Call the mcp__ptc__kernels tool and paste its raw output, then run mcp__ptc__exec with code print(1)"
ls ~/.ptc/run/           # expect claude-<pid>.json
cat ~/.ptc/run/claude-*.json
ls ~/.ptc/kernels/       # expect a kernel keyed by the session id from the run-file
```
Scenario 2 (resume): note the session id from scenario 1's transcript (`claude -p` prints it with `--output-format json`; or read the newest `~/.claude/projects/*/`), then `claude -p --resume <id> --plugin-dir ./plugin "run mcp__ptc__exec code print(2)"` — expect the SAME kernel key.
Scenario 3 (two windows): open two interactive `claude --plugin-dir ./plugin` in this cwd, run `2+2` through ptc in both, then `ptc list` — expect two distinct keys.

- [ ] **Step 4: Record the S3 verdict in the spec** (Surprises & Discoveries; promote per spec: *"Promote if `kernels()` shows the right key with zero configuration in all three scenarios"*; on failure the chain's fallbacks 3–5 carry — record which link broke and how the fallback behaved.)

- [ ] **Step 5: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc ptc-surface/docs
git commit -m "f5(ptc): T11 — plugin skeleton (launcher, hook) + spike S3 verdict"
```

---

### Task 12: Spike S5 — MCP image blocks in Claude Code (LIVE)

**Spike task.** Question: does Claude Code 2.1.236 render an `ImageContent` block returned by an MCP tool? Spec criteria verbatim: *"Promote → plots visible inline. Fallback: text mentions the saved PNG path only."*

**Files:**
- Create: `ptc-surface/ptc/test/spikes/s5_image_block.md` (a runbook, not code — the probe is a live claude invocation)

- [ ] **Step 1: Write the runbook** (`test/spikes/s5_image_block.md`):

```markdown
# S5: MCP image block rendering
1. cd ptc-surface/ptc
2. claude --plugin-dir ./plugin   (interactive; allow mcp__ptc__* when prompted)
3. Prompt: "Use mcp__ptc__exec to run:
   import matplotlib; matplotlib.use('module://matplotlib_inline.backend_inline')
   import matplotlib.pyplot as plt
   plt.plot([1,4,2,8]); plt.title('s5'); plt.show()"
4. Observe: does an inline image render in the transcript? Also check
   ~/.ptc/kernels/<key>/cells/ for the saved <n>-0.png (the shim must save it regardless).
5. Repeat with `claude -p` and --output-format stream-json; check whether an image
   content block appears in the mcp tool_result.
```

- [ ] **Step 2: Run it, observe both outcomes** (interactive render; PNG saved on disk always).

- [ ] **Step 3: Record the verdict + apply.** Promote → nothing to change (T8 already emits blocks + saves files). Discard → edit `ptc/src/ptc/mcp.py::_content` to skip `ImageContent` and instead append a text line `[image saved: <path>]`, and record the spec fallback. Either way append the Observation to the spec's Surprises & Discoveries with what was seen.

- [ ] **Step 4: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc ptc-surface/docs
git commit -m "f5(ptc): T12 — spike S5: MCP image rendering verdict"
```

**M0 gate checkpoint** — before proceeding: `uv run --group dev pytest test/unit test/integration -q` must be fully green (keyless, no skips), and the spec must contain S1/S3/S5 verdicts. This is the spec's M0 exit.

---

### Task 13: Full session discovery (`discovery.resolve`) + meta wiring

**Files:**
- Modify: `ptc-surface/ptc/src/ptc/discovery.py`
- Modify: `ptc-surface/ptc/src/ptc/mcp.py` (delete the `_resolve` stub; use `discovery.resolve`)
- Modify: `ptc-surface/ptc/src/ptc/cli.py` (replace `_pick_session` internals with `discovery.resolve(explicit, ppid=None)` + keep the newest-live fallback CLI-side)
- Create: `ptc-surface/ptc/test/unit/test_discovery.py`

**Interfaces:**
- Consumes: T11 run-file contract.
- Produces (final): `discovery.resolve(explicit=None, ppid=None, env=os.environ, proc_name=<injectable>) -> Resolved(key, source, claude_session_id, cwd, degraded)`. Sources in priority order: `"explicit"` → `"hook-runfile"` → `"env-ptc-session"` → `"env-claude-session"` → `"adapter-local"` (degraded=True). `claude_session_id` is set ONLY when the value is known to be a real Claude session id (runfile / env-claude-session / explicit-that-looks-like-uuid); `PTC_SESSION` values are kernel keys, not Claude ids — `claude_session_id=None` for them **unless** the runfile confirms.

- [ ] **Step 1: Write the failing tests**

`test/unit/test_discovery.py`:

```python
import json
import os

from ptc.discovery import resolve


def _write_runfile(home, pid, sid="11111111-2222-3333-4444-555555555555", cwd="/proj"):
    rd = home / "run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"claude-{pid}.json").write_text(json.dumps(
        {"session_id": sid, "cwd": cwd, "written_at": 1}))


def test_explicit_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(explicit="my-key", env={"PTC_SESSION": "other"})
    assert r.key == "my-key" and r.source == "explicit" and not r.degraded


def test_runfile_via_ppid(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, 777)
    r = resolve(ppid=777, env={}, proc_name=lambda pid: "claude")
    assert r.source == "hook-runfile"
    assert r.claude_session_id == "11111111-2222-3333-4444-555555555555"
    assert r.key == r.claude_session_id and r.cwd == "/proj" and not r.degraded


def test_runfile_ignored_when_ppid_not_claude_and_walks_up(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, 900)
    # ppid 800 is a shell whose parent 900 is claude
    parents = {800: 900}
    r = resolve(ppid=800, env={},
                proc_name=lambda pid: "claude" if pid == 900 else "zsh",
                proc_parent=lambda pid: parents.get(pid))
    assert r.source == "hook-runfile" and r.key == "11111111-2222-3333-4444-555555555555"


def test_env_chain_and_degraded(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(env={"PTC_SESSION": "childkey-1"})
    assert r.source == "env-ptc-session" and r.claude_session_id is None
    r2 = resolve(env={"CLAUDE_CODE_SESSION_ID": "abc-123"})
    assert r2.source == "env-claude-session" and r2.claude_session_id == "abc-123"
    r3 = resolve(env={})
    assert r3.source == "adapter-local" and r3.degraded and r3.key.startswith("adapter-")
```

- [ ] **Step 2: Run to verify failure**, then implement in `discovery.py`:

```python
import os as _os
import re
import subprocess
from dataclasses import dataclass

from .paths import run_dir, safe_key

_UUIDISH = re.compile(r"^[0-9a-fA-F-]{8,}$")


@dataclass
class Resolved:
    key: str
    source: str
    claude_session_id: str | None
    cwd: str | None
    degraded: bool


def _proc_name(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _proc_parent(pid: int) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _runfile_for(pid: int) -> dict | None:
    import json
    p = run_dir() / f"claude-{pid}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def resolve(explicit: str | None = None, ppid: int | None = None, env=None,
            proc_name=_proc_name, proc_parent=_proc_parent) -> Resolved:
    env = _os.environ if env is None else env
    if explicit:
        sid = explicit if _UUIDISH.match(explicit) else None
        return Resolved(safe_key(explicit), "explicit", sid, None, False)
    pid = ppid if ppid is not None else _os.getppid()
    for _ in range(12):
        if pid is None or pid <= 1:
            break
        if "claude" in _os.path.basename(proc_name(pid) or ""):
            rf = _runfile_for(pid)
            if rf and rf.get("session_id"):
                return Resolved(safe_key(rf["session_id"]), "hook-runfile",
                                rf["session_id"], rf.get("cwd"), False)
            break
        pid = proc_parent(pid)
    v = env.get("PTC_SESSION")
    if v:
        return Resolved(safe_key(v), "env-ptc-session", None, env.get("PTC_CWD"), False)
    v = env.get("CLAUDE_CODE_SESSION_ID")
    if v:
        return Resolved(safe_key(v), "env-claude-session", v, None, False)
    return Resolved(f"adapter-{_os.getpid()}", "adapter-local", None, None, True)
```

Wire it: in `mcp.py` delete the stub and `from .discovery import resolve as _resolve` with `_resolve(session)` calls becoming `resolve(session)`. `ensure_kernel(...)` already receives `claude_session_id=r.claude_session_id` — it writes it into `meta.json` (T4). In `cli.py`, `_pick_session` first calls `resolve(a.session)`; only when the result is `adapter-local` does it fall through to the newest-live-kernel notice behavior (CLI keeps that extra rung; MCP does not).

- [ ] **Step 3: Run the whole keyless suite** (`uv run --group dev pytest test/unit test/integration -q`) — expected: all green.

- [ ] **Step 4: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T13 — full discovery chain (runfile tree-walk, env rungs, degraded local)"
```

---

### Task 14: `read` / `write` / `edit` (+ audit wiring, mutation footer end-to-end)

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/files.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind `read/write/edit`)
- Create: `ptc-surface/ptc/test/unit/test_files.py`
- Create: `ptc-surface/ptc/test/integration/test_mutation_footer.py`

**Interfaces:**
- Consumes: `runtime.audit.append`, `runtime.state.STATE`.
- Produces (bound in the kernel namespace): `read(path, offset=None, limit=None, numbered=False) -> str`; `write(path, content) -> str` (creates parents; returns `"Wrote <abspath> (N lines)"`); `edit(path, old, new, replace_all=False) -> str` (exactly-once rule; returns `"Edited <abspath> (+a/−r)"`). Audit kinds: `write{path, added}`, `edit{path, added, removed}`.

- [ ] **Step 1: Write the failing unit tests**

`test/unit/test_files.py`:

```python
import pytest

from ptc.runtime import files
from ptc.runtime.state import STATE


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.current_cell = 7
    STATE.cell_mutations = []
    yield


def test_read_offset_limit_numbered(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\nd\n")
    assert files.read(p) == "a\nb\nc\nd\n"
    assert files.read(p, offset=2, limit=2) == "b\nc\n"
    assert files.read(p, offset=2, limit=1, numbered=True) == "     2\tb\n"


def test_write_creates_parents_and_audits(tmp_path):
    out = files.write(tmp_path / "new" / "dir" / "x.md", "one\ntwo\n")
    assert out.startswith("Wrote") and "(2 lines)" in out
    assert STATE.cell_mutations[-1]["kind"] == "write"


def test_edit_exactly_once_rules(tmp_path):
    p = tmp_path / "s.py"
    p.write_text("aaa\nbbb\naaa\n")
    with pytest.raises(ValueError, match="string not found"):
        files.edit(p, "zzz", "y")
    with pytest.raises(ValueError, match="found 2 occurrences .* widen the snippet"):
        files.edit(p, "aaa", "yyy")
    msg = files.edit(p, "bbb", "BBB\nBB2")
    assert msg.startswith("Edited") and "(+2/−1)" in msg
    assert p.read_text() == "aaa\nBBB\nBB2\naaa\n"
    msg2 = files.edit(p, "aaa", "A", replace_all=True)
    assert p.read_text() == "A\nBBB\nBB2\nA\n"
    m = STATE.cell_mutations[-1]
    assert m["kind"] == "edit" and m["path"].endswith("s.py")
```

- [ ] **Step 2: Run to verify failure, then implement**

`src/ptc/runtime/files.py`:

```python
"""File primitives with Claude-Code-exact edit semantics. Mutations audit."""
from pathlib import Path

from . import audit


def read(path, offset: int | None = None, limit: int | None = None,
         numbered: bool = False) -> str:
    p = Path(path).expanduser()
    text = p.read_text(errors="replace")
    if offset is None and limit is None and not numbered:
        return text
    lines = text.splitlines(keepends=True)
    start = (offset - 1) if offset else 0
    sel = lines[start: start + limit if limit else None]
    if numbered:
        return "".join(f"{start + i + 1:>6}\t{ln}" for i, ln in enumerate(sel))
    return "".join(sel)


def write(path, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    audit.append("write", path=str(p.resolve()), added=n)
    return f"Wrote {p.resolve()} ({n} lines)"


def edit(path, old: str, new: str, replace_all: bool = False) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        raise ValueError(f"string not found in {p}")
    if n > 1 and not replace_all:
        raise ValueError(f"found {n} occurrences in {p}, need exactly 1 — "
                         "widen the snippet to make it unique, or pass replace_all=True")
    count = n if replace_all else 1
    p.write_text(text.replace(old, new, count))
    removed = len(old.splitlines()) * count
    added = len(new.splitlines()) * count
    audit.append("edit", path=str(p.resolve()), added=added, removed=removed)
    return f"Edited {p.resolve()} (+{added}/−{removed})"
```

Bind in `runtime/__init__.py`:

```python
def bind(ip) -> None:
    from .files import edit, read, write
    ns = {"read": read, "write": write, "edit": edit}
    ip.user_ns.update(ns)
```

- [ ] **Step 3: Integration test — footer visible through the MCP path**

`test/integration/test_mutation_footer.py`:

```python
import asyncio

from ptc.kernel import kill_kernel
from ptc.mcp import exec_tool


def test_edit_footer_and_audit(ptc_home, tmp_path):
    f = tmp_path / "t.py"
    f.write_text("def a():\n    return 1\n")
    code = f"edit({str(f)!r}, 'return 1', 'return 2')"
    r = asyncio.run(exec_tool(code=code, session="f1", timeout_s=60))
    assert "edited" in r[0].text and "(+1/−1)" in r[0].text
    audit = (ptc_home / "kernels" / "f1" / "audit.jsonl").read_text()
    assert "t.py" in audit
    kill_kernel("f1")
```

- [ ] **Step 4: Run everything, commit**

```bash
uv run --group dev pytest test/unit/test_files.py test/integration/test_mutation_footer.py -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T14 — read/write/edit with exact-match semantics and audited footer"
```

---

### Task 15: `bash()` (async, background handles)

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/shell.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind `bash`)
- Create: `ptc-surface/ptc/test/integration/test_bash.py`

**Interfaces:**
- Produces (kernel namespace): `await bash(cmd, timeout=120.0, cwd=None, env=None, background=False)` → `BashResult(code: int | None, stdout: str, stderr: str, timed_out: bool)` or (background) `BashHandle(pid, poll() -> int | None, await wait() -> BashResult, output() -> str, kill())`. Audit kind: `bash{command}` (first 200 chars).

- [ ] **Step 1: Write the failing integration test**

`test/integration/test_bash.py`:

```python
import asyncio
import textwrap

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def _exec(key, code):
    return KernelClient(key).exec_cell(textwrap.dedent(code), timeout_s=90, config=Config.from_env())


def test_bash_result_timeout_background(ptc_home):
    ensure_kernel("b1", cwd=str(ptc_home))
    out = _exec("b1", """
        r = await bash("echo out; echo err 1>&2; exit 3")
        print(r.code, r.stdout.strip(), r.stderr.strip(), r.timed_out)
    """)
    assert isinstance(out, Completed) and "3 out err False" in out.output

    out = _exec("b1", """
        r = await bash("sleep 30", timeout=1)
        print(r.timed_out, r.code)
    """)
    assert "True" in out.output

    out = _exec("b1", """
        h = await bash("sleep 0.5; echo done", background=True)
        print(type(h).__name__, h.poll() is None)
        r = await h.wait()
        print(r.stdout.strip())
    """)
    assert "BashHandle True" in out.output and "done" in out.output
    kill_kernel("b1")


def test_percent_bash_magic_still_works(ptc_home):
    ensure_kernel("b2", cwd=str(ptc_home))
    out = _exec("b2", "%%bash\necho magic-$((1+1))")
    assert "magic-2" in out.output
    kill_kernel("b2")
```

- [ ] **Step 2: Run to verify failure, then implement**

`src/ptc/runtime/shell.py`:

```python
"""async shell for the kernel. %%bash magic remains available alongside."""
import asyncio
import os
from dataclasses import dataclass

from . import audit


@dataclass
class BashResult:
    code: int | None
    stdout: str
    stderr: str
    timed_out: bool


class BashHandle:
    def __init__(self, proc, cmd: str):
        self._proc = proc
        self._cmd = cmd
        self._out: list[bytes] = []
        self._err: list[bytes] = []
        self._pump = asyncio.ensure_future(self._drain())

    @property
    def pid(self) -> int:
        return self._proc.pid

    async def _drain(self):
        async def pump(stream, buf):
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                buf.append(chunk)
        await asyncio.gather(pump(self._proc.stdout, self._out),
                             pump(self._proc.stderr, self._err))

    def poll(self) -> int | None:
        return self._proc.returncode

    def output(self) -> str:
        return b"".join(self._out).decode(errors="replace")

    async def wait(self) -> BashResult:
        await self._proc.wait()
        await self._pump
        return BashResult(self._proc.returncode, self.output(),
                          b"".join(self._err).decode(errors="replace"), False)

    def kill(self) -> None:
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass


async def bash(cmd: str, timeout: float = 120.0, cwd=None, env=None,
               background: bool = False):
    audit.append("bash", command=cmd[:200])
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    if background:
        return BashHandle(proc, cmd)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return BashResult(proc.returncode, out.decode(errors="replace"),
                          err.decode(errors="replace"), False)
    except asyncio.TimeoutError:
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        out, err = await proc.communicate()
        return BashResult(None, out.decode(errors="replace"),
                          err.decode(errors="replace"), True)
```

Add `"bash": shell.bash` to `bind()` (import `from . import shell`).

- [ ] **Step 3: Run tests, commit**

```bash
uv run --group dev pytest test/integration/test_bash.py -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T15 — async bash() with timeout kill and background handles"
```

---

### Task 16: Watchdog TTL end-to-end (expiry notice)

**Files:**
- Create: `ptc-surface/ptc/test/integration/test_ttl.py`

**Interfaces:** consumes T5's watchdog + T8's expired-notice prepend. No new API — this task proves the behavior.

- [ ] **Step 1: Write the failing test**

```python
import asyncio
import time

from ptc.kernel import ensure_kernel, kernel_alive
from ptc.mcp import exec_tool
from ptc.paths import Config


def test_ttl_expiry_and_notice(ptc_home, monkeypatch):
    monkeypatch.setenv("PTC_IDLE_HOURS", "0.0006")     # ~2.2 s
    cfg = Config.from_env()
    assert cfg.idle_hours == 0.0006
    r = asyncio.run(exec_tool(code="ttl_x = 1", session="t1", timeout_s=60))
    assert "ok" in r[0].text
    deadline = time.time() + 30
    while kernel_alive("t1") and time.time() < deadline:
        time.sleep(0.5)
    assert not kernel_alive("t1"), "watchdog never fired"
    assert (ptc_home / "kernels" / "t1" / "expired.marker").exists()
    r2 = asyncio.run(exec_tool(code="print('ttl_x' in dir())", session="t1", timeout_s=60))
    assert "previous kernel expired" in r2[0].text and "False" in r2[0].text
```

- [ ] **Step 2: Run.** If the watchdog's poll interval makes this flaky, tighten `bootstrap._watchdog`'s sleep floor (it already sleeps `min(30, hours*3600/10)` → ~0.5 s here). Expected: pass, ~5–15 s.

- [ ] **Step 3: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T16 — idle TTL expiry proves marker + fresh-namespace notice"
```

---

### Task 17: SKILL.md v0 + live plugin smoke

**Files:**
- Create: `ptc-surface/ptc/plugin/skills/ptc/SKILL.md`
- Create: `ptc-surface/ptc/test/live/test_plugin_smoke.py`

**Interfaces:** consumes everything M0/M1 shipped. The skill body follows the spec's "SKILL.md content contract" — 10 sections, **no search guidance**. T27 finalizes wording; v0 must already be complete and correct.

- [ ] **Step 1: Write SKILL.md v0**

`plugin/skills/ptc/SKILL.md`:

```markdown
---
name: ptc
description: Persistent per-session IPython kernel for programmatic tool calling — bulk file analysis, data transforms with state that survives across turns, parallel subagent fan-out/fan-in, long-running loops, multi-agent orchestration in Python. Use when work needs many reads/steps whose intermediates belong in variables, not conversation.
---

# ptc — the programmatic lane

You have a persistent IPython kernel for this session (session id: ${CLAUDE_SESSION_ID}).
Run Python in it with the `mcp__ptc__exec` tool. Variables, imports, functions, and agent
handles persist across calls, turns, compaction, and `--resume`, until the kernel's idle
TTL (default 24 h) or a restart. If results ever look like another session's namespace,
pass `session="${CLAUDE_SESSION_ID}"` explicitly.

## When to use ptc — and when not

Use the kernel when the work is programmatic: reading and filtering many files, computing
over data, fanning out subagents and aggregating their answers, iterating with state, or
orchestrating a pipeline. Use native tools instead for: a single known edit (native Edit),
reading images/PDFs/notebooks (native Read), and anything the user should see and approve
step by step.

## Working discipline

- Assign large results to named variables; print compact summaries. Output truncates
  (~12k chars) with a path to the full log.
- Never poll with `time.sleep` in a cell. If a cell yields `running`, use `mcp__ptc__wait`
  with the cell id; `mcp__ptc__interrupt` stops a runaway cell.
- If the kernel reports `busy`, another cell is running — wait for it or interrupt; nothing
  queues silently.
- Run a project's code in the project's own environment (its venv, its npm scripts) via
  `bash(...)`; never install project dependencies into the kernel.

## Files & shell

    text = read("src/app.py")                    # offset=, limit=, numbered= available
    write("notes/out.md", content)               # creates parents
    edit("src/app.py", old, new)                 # old must match EXACTLY once; use
                                                 # replace_all=True for bulk; widen the
                                                 # snippet if it errors on multiple matches
    r = await bash("npm test", timeout=300)      # r.code, r.stdout, r.stderr, r.timed_out
    h = await bash("slow cmd", background=True)  # h.poll(), await h.wait(), h.kill()

`%%bash` cells also work: `%%bash` must be the FIRST line of the cell; each `%%bash` cell
is a throw-away subshell (its `cd`/`export` do not persist) — use `%cd` and
`os.environ["VAR"]=...` for state that should carry to later cells.

## Agents (children run with bypassPermissions — delegate only work you'd run yourself)

    r = await agent.run("Review auth.py for injection bugs")      # blocks this cell
    h1 = agent.spawn("Audit module A", name="a")                  # starts immediately
    h2 = agent.spawn("Audit module B", name="b")
    ra, rb = await agent.gather(h1, h2)                           # fan-in
    r = await agent.fork("What did we decide about the cache?")   # child INHERITS this
                                                                  # conversation
    await h1.send("Also check the tests")                         # follow-up, same child
    agent.list(); agent.resume(session_id)                        # survive kernel restarts
    await agent.run("port this loop to rust", provider="codex")   # Codex worker

Options: model=, system=, allowed_tools=, permission_mode=, cwd=, max_turns=, effort=,
output_schema= (JSON Schema → r.structured). Depth is limited (default 1): children
cannot spawn grandchildren unless PTC_MAX_DEPTH is raised.

## Sub-LM map-reduce

    labels = await asyncio.gather(*[
        llm(f"One-word topic for:\n{chunk}") for chunk in chunks])
    final = await llm("Synthesize:\n" + "\n".join(labels), model="sonnet")

`llm(prompt, model="haiku", system=None, json_schema=None, timeout=300)` — one-shot, no
tools, subscription-billed.

## Web

    page = await web_fetch(url)          # page.text is FULL markdown — filter in code
    hits = await web_search("query")     # [SearchResult(title, url, snippet)]

## History (lossless memory)

    h = history()                        # this session's full transcript, pre-compaction
    h.user(); h.assistant(); h.tool_calls("Bash"); h.search(r"regex")
    child_h = handle.history()           # a child's transcript

## Pitfalls

- Only the documented names exist: read, write, edit, bash, agent, llm, web_fetch,
  web_search, history, workflow. Do not invent wrappers such as `call_skill(...)` or
  `run_subagent(...)`.
- The kernel is your notebook, not the project's runtime.
- A kernel restart loses variables (agent sessions remain resumable via `agent.list()`).

## Worked example — bulk audit with fan-out

    from pathlib import Path
    files = {p: p.read_text(errors="replace") for p in Path("src").rglob("*.py")}
    todo = {p: t for p, t in files.items() if "TODO" in t}
    print(len(files), "files,", len(todo), "with TODOs")
    hs = [agent.spawn(f"Summarize the TODOs in {p}:\n{t[:8000]}", name=p.stem)
          for p, t in list(todo.items())[:4]]
    for r in await agent.gather(*hs):
        print(r.text[:200])
```

- [ ] **Step 2: Live smoke test** (`test/live/test_plugin_smoke.py`):

```python
import json
import os
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_model_uses_ptc_for_bulk_prompt(tmp_path):
    """A11: the prompt does NOT mention ptc — the skill must route the model there,
    and the proof is an actual mcp__ptc__exec tool_use event in the stream (F10)."""
    (tmp_path / "src").mkdir()
    for i in range(12):
        (tmp_path / "src" / f"m{i}.py").write_text(f"# TODO item {i}\nx = {i}\n")
    r = subprocess.run(
        ["claude", "-p", "--plugin-dir", str(PKG / "plugin"),
         "--permission-mode", "bypassPermissions",
         "--output-format", "stream-json", "--verbose",
         "analyze all python files under src/ for TODO density; "
         "keep intermediate data in variables"],
        capture_output=True, text=True, cwd=tmp_path, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    used_ptc = any('"name": "mcp__ptc__exec"' in line or "mcp__ptc__exec" in line
                   for line in r.stdout.splitlines() if '"tool_use"' in line or "tool_use" in line)
    assert used_ptc, "model never called mcp__ptc__exec; skill routing failed"
    assert "12" in r.stdout
```

Run: `PTC_LIVE=1 uv run --group dev pytest test/live/test_plugin_smoke.py -q` → expected: pass (uses subscription quota). Keyless run must SKIP cleanly (`uv run --group dev pytest test/live -q` → skipped).

- [ ] **Step 3: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T17 — SKILL.md v0 + live plugin smoke (M1 complete)"
```

---

# Milestone M2 — agents + llm

### Task 18: Spike S2 — forking a live Claude Code session (LIVE)

**Spike task.** Question: does `resume=<parent id> + fork_session=True` work mid-turn against a partially-flushed transcript? Spec criteria verbatim: *"Promote if the fork child answers a parent-only fact. Fallback: document fork as sound between turns; mid-turn the child sees the transcript up to the last flushed message (acceptable; note in skill)."*

**Files:**
- Create: `ptc-surface/ptc/test/spikes/s2_live_fork.py`

- [ ] **Step 1: Build the probe**

```python
"""S2: fork a LIVE claude session from inside a tool call it is currently running.
PTC_LIVE=1 required. Two phases:
  phase A (this script): start an interactive-ish parent via `claude -p` that, mid-turn,
  runs a Bash tool call invoking THIS script's --fork phase with $CLAUDE_CODE_SESSION_ID.
  phase B (--fork <sid>): use claude-agent-sdk resume+fork_session to ask for the marker.
"""
import asyncio
import os
import subprocess
import sys


async def fork_and_ask(sid: str) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, query
    opts = ClaudeAgentOptions(resume=sid, fork_session=True, max_turns=1, tools=[],
                              permission_mode="bypassPermissions")
    async for m in query(prompt="What is the exact MARKER phrase mentioned earlier in this "
                                "conversation? Reply with only the phrase.", options=opts):
        if type(m).__name__ == "AssistantMessage":
            for b in m.content:
                print("FORK_SAW:", getattr(b, "text", ""))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--fork":
        asyncio.run(fork_and_ask(sys.argv[2]))
        sys.exit(0)
    prompt = ("Remember this MARKER: quokka-basilisk-42. Then run this exact bash command and "
              f"show its output: python3 {os.path.abspath(__file__)} --fork "
              "\"$CLAUDE_CODE_SESSION_ID\"")
    r = subprocess.run(["claude", "-p", "--permission-mode", "bypassPermissions", prompt],
                       capture_output=True, text=True, timeout=600)
    print(r.stdout)
    print("VERDICT: promote" if "quokka-basilisk-42" in r.stdout else "VERDICT: check fallback")
```

- [ ] **Step 2: Run** `PTC_LIVE=1 uv run --group dev python test/spikes/s2_live_fork.py` — observe `FORK_SAW` and the VERDICT line. The marker was stated in the parent's *current, still-running* turn, so recall proves mid-turn fork.

- [ ] **Step 3: Record verdict in the spec** (Surprises & Discoveries; on fallback also note it in SKILL.md's agent section: fork sees the transcript up to the last flushed message).

- [ ] **Step 4: Commit** (`f5(ptc): T18 — spike S2: live-fork verdict`).

---

### Task 19: Spike S4 — headless `codex app-server` turn (LIVE)

**Spike task.** Question: which `thread/start` params make a Codex turn complete with zero server→client approval requests? Spec criteria verbatim: *"Promote when a trivial turn completes unattended. Fallback: client-side auto-accept of approval requests."*

**Files:**
- Create: `ptc-surface/ptc/test/spikes/s4_codex_headless.py`

- [ ] **Step 1: Build the probe** — a minimal stdio JSON-RPC client (this code seeds T22's real client):

```python
"""S4: drive `codex app-server` headless. PTC_LIVE=1 + `codex` login required."""
import json
import os
import subprocess
import sys
import threading


class AppServer:
    def __init__(self):
        self.p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.next_id = 0
        self.events: list[dict] = []
        self.approvals: list[str] = []

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("app-server closed")
            msg = json.loads(line)
            self.events.append(msg)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]
            if "method" in msg and "id" in msg:      # server→client REQUEST (approval!)
                print("SERVER_REQUEST:", msg["method"])
                self.approvals.append(msg["method"])
                self.send({"id": msg["id"], "result": {"decision": "accept"}})

    def drain_until(self, method, timeout=300):
        import select, time
        end = time.time() + timeout
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            self.events.append(msg)
            if "method" in msg and "id" in msg:
                print("SERVER_REQUEST:", msg["method"])
                self.approvals.append(msg["method"])
                self.send({"id": msg["id"], "result": {"decision": "accept"}})
            if msg.get("method") == method:
                return msg
        raise TimeoutError(method)


def main():
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP: PTC_LIVE=1"); return
    s = AppServer()
    print("init:", s.request("initialize", {"clientInfo": {"name": "ptc-spike"}}))
    th = s.request("thread/start", {"cwd": os.getcwd(), "approvalPolicy": "never",
                                    "sandbox": "read-only"})
    tid = th["thread"]["id"] if "thread" in th else th.get("threadId") or th.get("id")
    print("thread:", tid)
    s.request("turn/start", {"threadId": tid,
                             "input": [{"type": "text", "text": "Reply with exactly: CODEX-OK"}]})
    done = s.drain_until("turn/completed")
    texts = [e for e in s.events
             if e.get("method") == "item/completed"
             and e.get("params", {}).get("item", {}).get("type") == "agentMessage"]
    print("agent said:", [t["params"]["item"].get("text") for t in texts])
    print("VERDICT:", "promote (zero approvals)" if not s.approvals
          else f"fallback needed — approvals seen: {s.approvals}")
    s.p.terminate()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run** `PTC_LIVE=1 uv run --group dev python test/spikes/s4_codex_headless.py`. Observe: does `CODEX-OK` arrive; did any `SERVER_REQUEST:` lines print (each is an approval the policy failed to suppress); the exact accepted shapes of `thread/start` params and response (adjust the probe's field extraction if the schema differs — record what the real wire looked like).

- [ ] **Step 3: Record verdict + the observed wire shapes in the spec.** Promote → T22 uses `approvalPolicy: never` verbatim; fallback → T22 keeps the auto-accept responder (it is in the probe already).

- [ ] **Step 4: Commit** (`f5(ptc): T19 — spike S4: codex headless wire shapes recorded`).

---

### Task 20: Claude agent backend + `agent` namespace core

Apply S1's verdict: if S1 promoted the kernel-loop design, `claude_backend` runs directly on the loop as written below; if S1 chose the thread fallback, add the documented thread shim (`_submit(coro)` via `asyncio.run_coroutine_threadsafe` + `asyncio.wrap_future`) inside `claude_backend` ONLY — `agents.py` and all tests below are identical either way.

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/agents.py`
- Create: `ptc-surface/ptc/src/ptc/runtime/claude_backend.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind `agent` + `asyncio`)
- Create: `ptc-surface/ptc/test/unit/test_agents.py`
- Create: `ptc-surface/ptc/test/live/test_agents_live.py`

**Interfaces:**
- Consumes: `state.STATE.config` (`depth`, `max_depth`, `max_concurrency`, `key`), `audit.append`, env `PTC_LAUNCHER` (T11).
- Produces (kernel namespace): the `agent` object with the spec's API. Exact types:

```python
@dataclass
class AgentOpts:
    provider: str = "claude"; model: str | None = None; system: str | None = None
    allowed_tools: list[str] | None = None; permission_mode: str = "bypassPermissions"
    cwd: str | None = None; max_turns: int | None = None; effort: str | None = None
    output_schema: dict | None = None; timeout: float | None = None

@dataclass
class AgentResult:
    text: str; session_id: str | None; structured: dict | None
    cost_usd: float | None; num_turns: int | None; duration_ms: int

class AgentHandle:
    name: str; provider: str
    @property session_id -> str | None
    @property status -> str          # "running" | "done" | "error" | "interrupted"
    async def result(self) -> AgentResult
    async def send(self, msg: str) -> AgentResult
    def messages(self) -> list[dict]
    def history(self)                # lazy → ptc.runtime.transcript.history(self.session_id)
    async def interrupt(self) -> None
    async def close(self) -> None
```

- Backend protocol (both `claude_backend` and T22's `codex_backend` implement it; the unit tests' FakeBackend too):

```python
async def run_once(task: str, o: AgentOpts, *, resume: str | None = None,
                   fork: bool = False) -> AgentResult
async def open_session(task: str | None, o: AgentOpts, *, resume: str | None = None) -> Session
class Session:
    session_id: str | None
    async def wait_result(self) -> AgentResult      # result of the initial task (if any)
    async def send(self, msg: str) -> AgentResult   # one follow-up turn
    async def interrupt(self) -> None
    async def close(self) -> None
    def messages(self) -> list[dict]
```

- [ ] **Step 1: Write the failing unit tests (FakeBackend, no SDK, no auth)**

`test/unit/test_agents.py`:

```python
import asyncio
import json

import pytest

from ptc.runtime.agents import AgentOpts, AgentResult, _Agent
from ptc.runtime.state import STATE


class FakeSession:
    def __init__(self, task, o):
        self.session_id = f"fake-{task[:8]}"
        self._task = task
        self.sent: list[str] = []
        self.closed = False

    async def wait_result(self):
        await asyncio.sleep(0.05)
        return AgentResult(f"did:{self._task}", self.session_id, None, 0.0, 1, 50)

    async def send(self, msg):
        self.sent.append(msg)
        return AgentResult(f"reply:{msg}", self.session_id, None, 0.0, 1, 10)

    async def interrupt(self):
        pass

    async def close(self):
        self.closed = True

    def messages(self):
        return [{"task": self._task}]


class FakeBackend:
    def __init__(self):
        self.concurrent = 0
        self.max_seen = 0

    async def run_once(self, task, o, *, resume=None, fork=False):
        self.concurrent += 1
        self.max_seen = max(self.max_seen, self.concurrent)
        try:
            await asyncio.sleep(0.1)
            tag = "forked:" if fork else ""
            return AgentResult(f"{tag}{task}", resume or "fresh", None, 0.0, 1, 100)
        finally:
            self.concurrent -= 1

    async def open_session(self, task, o, *, resume=None):
        return FakeSession(task or "resumed", o)


def _agent(tmp_path, **cfg):
    STATE.kernel_dir = tmp_path
    conf = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 2}
    conf.update(cfg)
    STATE.config = conf
    return _Agent(conf, {"claude": FakeBackend(), "codex": FakeBackend()})


def test_run_and_depth_guard(tmp_path):
    a = _agent(tmp_path)
    r = asyncio.run(a.run("hello"))
    assert r.text == "hello"
    a2 = _agent(tmp_path, depth=1)
    with pytest.raises(RuntimeError, match="agent depth limit reached"):
        asyncio.run(a2.run("nope"))


def test_semaphore_bounds_concurrency(tmp_path):
    a = _agent(tmp_path, max_concurrency=2)
    fb = a._backends["claude"]

    async def burst():
        await asyncio.gather(*(a.run(f"t{i}") for i in range(6)))
    asyncio.run(burst())
    assert fb.max_seen <= 2


def test_spawn_gather_registry_send(tmp_path):
    a = _agent(tmp_path)

    async def flow():
        h1 = a.spawn("alpha", name="one")
        h2 = a.spawn("beta")
        assert h1.status == "running"
        r1, r2 = await a.gather(h1, h2)
        assert r1.text == "did:alpha" and h1.status == "done"
        follow = await h1.send("more")
        assert follow.text == "reply:more"
        return h1
    h = asyncio.run(flow())
    rows = json.loads((tmp_path / "agents.json").read_text())
    assert any(e["name"] == "one" and e["status"] == "done" for e in rows)
    listed = a.list()
    assert any(e["name"] == "one" for e in listed)


def test_spawn_name_collision_and_timeout(tmp_path):
    a = _agent(tmp_path)

    async def flow():
        a.spawn("x", name="dup")
        with pytest.raises(ValueError, match="name already in use"):
            a.spawn("y", name="dup")
        with pytest.raises(asyncio.TimeoutError):
            await a.run("slow", timeout=0.01)
    asyncio.run(flow())
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --group dev pytest test/unit/test_agents.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement `agents.py`**

`src/ptc/runtime/agents.py`:

```python
"""The agent namespace: run/spawn/fork/gather/send over pluggable backends."""
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

from . import audit
from .state import STATE


@dataclass
class AgentOpts:
    provider: str = "claude"
    model: str | None = None
    system: str | None = None
    allowed_tools: list | None = None
    permission_mode: str = "bypassPermissions"
    cwd: str | None = None
    max_turns: int | None = None
    effort: str | None = None
    output_schema: dict | None = None
    timeout: float | None = None


@dataclass
class AgentResult:
    text: str
    session_id: str | None
    structured: dict | None
    cost_usd: float | None
    num_turns: int | None
    duration_ms: int


class AgentHandle:
    def __init__(self, owner: "_Agent", name: str, provider: str, timeout: float | None):
        self._owner = owner
        self.name = name
        self.provider = provider
        self._timeout = timeout
        self._session = None
        self._driver: asyncio.Task | None = None      # retained — never discarded (F6)
        self._status = "running"
        self._result_fut: asyncio.Future = asyncio.get_event_loop().create_future()

    @property
    def session_id(self):
        return getattr(self._session, "session_id", None)

    @property
    def status(self) -> str:
        return self._status

    async def result(self) -> AgentResult:
        return await asyncio.shield(self._result_fut)

    async def send(self, msg: str) -> AgentResult:
        if self._status == "running":
            await self.result()
        if self._session is None:
            raise RuntimeError(f"agent {self.name!r} has no live session to send to")
        async with self._owner._semaphore():           # send holds a permit too (F6)
            coro = self._session.send(msg)
            r = await (asyncio.wait_for(coro, self._timeout) if self._timeout else coro)
        self._owner._registry_update(self, status=self._status)
        return r

    def messages(self) -> list:
        return self._session.messages() if self._session else []

    def history(self):
        from .transcript import history
        if not self.session_id:
            raise RuntimeError(f"agent {self.name!r} has no session id yet")
        return history(self.session_id)

    async def interrupt(self) -> None:
        """Cancel the driver, tear the session down, and settle exactly once (F6)."""
        self._status = "interrupted"
        if self._session:
            await self._session.interrupt()
        if self._driver and not self._driver.done():
            self._driver.cancel()
            try:
                await self._driver
            except (asyncio.CancelledError, Exception):
                pass
        if not self._result_fut.done():
            self._result_fut.cancel()
        await self.close()
        self._owner._registry_update(self, "interrupted")

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            finally:
                self._session = None


class _Agent:
    def __init__(self, config: dict, backends: dict):
        self._config = config
        self._backends = backends
        self._sem: asyncio.Semaphore | None = None
        self._handles: dict[str, AgentHandle] = {}

    # -- plumbing -----------------------------------------------------------
    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(int(self._config.get("max_concurrency", 8)))
        return self._sem

    def _check_depth(self) -> None:
        d, m = int(self._config.get("depth", 0)), int(self._config.get("max_depth", 1))
        if d >= m:
            raise RuntimeError(
                f"agent depth limit reached (PTC_DEPTH={d}, PTC_MAX_DEPTH={m}); "
                "raise PTC_MAX_DEPTH to allow grandchildren")

    def _opts(self, kw: dict) -> AgentOpts:
        provider = kw.pop("provider", "claude")
        if provider not in self._backends:
            raise ValueError(f"unknown provider {provider!r}; use one of {sorted(self._backends)}")
        bad = set(kw) - {f.name for f in AgentOpts.__dataclass_fields__.values()}
        if bad:
            raise TypeError(f"unsupported agent options: {sorted(bad)}")
        return AgentOpts(provider=provider, **kw)

    def _registry_path(self):
        return STATE.kernel_dir / "agents.json"

    def _registry_load(self) -> list:
        try:
            return json.loads(self._registry_path().read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def _registry_update(self, h: AgentHandle, status: str, task_head: str = "") -> None:
        rows = self._registry_load()
        row = next((r for r in rows if r["name"] == h.name), None)
        if row is None:
            row = {"name": h.name, "created_at": time.time()}
            rows.append(row)
        row.update({"provider": h.provider, "session_id": h.session_id, "status": status,
                    "last_turn_at": time.time()})
        if task_head:
            row["task_head"] = task_head[:120]
        tmp = self._registry_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(self._registry_path())

    # -- public API ---------------------------------------------------------
    async def run(self, task: str, **kw) -> AgentResult:
        self._check_depth()
        o = self._opts(kw)
        audit.append("agent", name=f"run-{o.provider}", task=task[:120], provider=o.provider)
        async with self._semaphore():
            coro = self._backends[o.provider].run_once(task, o)
            if o.timeout:
                return await asyncio.wait_for(coro, timeout=o.timeout)
            return await coro

    def spawn(self, task: str, *, name: str | None = None, **kw) -> AgentHandle:
        self._check_depth()
        o = self._opts(kw)
        name = name or f"agent-{uuid.uuid4().hex[:6]}"
        if name in self._handles and self._handles[name].status == "running":
            raise ValueError(f"agent name already in use: {name!r}")
        h = AgentHandle(self, name, o.provider, o.timeout)
        self._handles[name] = h
        audit.append("agent", name=name, task=task[:120], provider=o.provider)

        async def drive():
            try:
                async with self._semaphore():          # permit released on EVERY path (F6)
                    h._session = await self._backends[o.provider].open_session(task, o)
                    self._registry_update(h, "running", task_head=task)
                    coro = h._session.wait_result()
                    r = await (asyncio.wait_for(coro, o.timeout) if o.timeout else coro)
                h._status = "done"
                self._registry_update(h, "done")
                if not h._result_fut.done():
                    h._result_fut.set_result(r)
            except asyncio.CancelledError:
                if h._status != "interrupted":
                    h._status = "interrupted"
                    self._registry_update(h, "interrupted")
                raise
            except Exception as e:                     # noqa: BLE001 — surfaced via result()
                h._status = "error"
                self._registry_update(h, "error")
                await h.close()                        # tear down the CLI process tree (F6)
                if not h._result_fut.done():
                    h._result_fut.set_exception(e)
        h._driver = asyncio.ensure_future(drive())
        return h

    async def fork(self, task: str, **kw) -> AgentResult:
        self._check_depth()
        o = self._opts(kw)
        if o.provider != "claude":
            raise NotImplementedError("fork is claude-only; use provider='claude'")
        from ptc.discovery import read_meta
        sid = read_meta(self._config["key"]).get("claude_session_id")
        if not sid:
            raise RuntimeError("no claude_session_id known for this kernel "
                               "(alias-keyed session) — fork unavailable")
        audit.append("agent", name="fork", task=task[:120], provider="claude")
        async with self._semaphore():
            coro = self._backends["claude"].run_once(task, o, resume=sid, fork=True)
            if o.timeout:
                return await asyncio.wait_for(coro, timeout=o.timeout)
            return await coro

    async def gather(self, *handles: AgentHandle) -> list:
        return list(await asyncio.gather(*(h.result() for h in handles)))

    def list(self) -> list:
        rows = {r["name"]: dict(r) for r in self._registry_load()}
        for h in self._handles.values():
            rows.setdefault(h.name, {"name": h.name})
            rows[h.name].update({"provider": h.provider, "session_id": h.session_id,
                                 "status": h.status, "live": True})
        return sorted(rows.values(), key=lambda r: r.get("created_at", 0))

    def resume(self, session_id: str, **kw) -> AgentHandle:
        o = self._opts(kw)
        h = AgentHandle(self, f"resumed-{session_id[:8]}", o.provider, o.timeout)
        self._handles[h.name] = h

        async def open_():
            async with self._semaphore():
                h._session = await self._backends[o.provider].open_session(
                    None, o, resume=session_id)
            h._status = "done"          # nothing running; session is a follow-up target
            if not h._result_fut.done():
                h._result_fut.set_result(
                    AgentResult("(resumed — use send())", session_id, None, None, None, 0))
            self._registry_update(h, "done")
        asyncio.ensure_future(open_())
        return h
```

- [ ] **Step 4: Implement `claude_backend.py`**

```python
"""claude-agent-sdk backend. All model calls in the kernel go through here or codex_backend."""
import asyncio
import json
import os
import time
import uuid

from .agents import AgentOpts, AgentResult
from .state import STATE


def _sdk_options(o: AgentOpts, *, resume: str | None = None, fork: bool = False,
                 bare_llm: bool = False):
    from claude_agent_sdk import ClaudeAgentOptions
    parent_key = STATE.config.get("key", "root")
    child_key = f"{parent_key}-a{uuid.uuid4().hex[:6]}"
    env = {
        "PTC_SESSION": child_key,                                # NEVER inherit the parent key
        "PTC_DEPTH": str(int(STATE.config.get("depth", 0)) + 1),
        "PTC_MAX_DEPTH": str(STATE.config.get("max_depth", 1)),
    }
    kw: dict = dict(
        model=o.model,
        cwd=o.cwd or STATE.config.get("cwd") or os.getcwd(),
        permission_mode=o.permission_mode,
        env=env,
    )
    if o.system:
        kw["system_prompt"] = o.system
    if o.max_turns:
        kw["max_turns"] = o.max_turns
    if o.effort:
        kw["effort"] = o.effort
    if o.allowed_tools is not None:
        kw["allowed_tools"] = o.allowed_tools
    if o.output_schema:
        kw["output_format"] = {"type": "json_schema", "schema": o.output_schema}
    if bare_llm:
        kw["tools"] = []
        kw["max_turns"] = 1
    else:
        launcher = os.environ.get("PTC_LAUNCHER")
        if launcher:
            kw["mcp_servers"] = {"ptc": {"type": "stdio", "command": launcher}}
    if resume:
        kw["resume"] = resume
        kw["fork_session"] = fork
    return ClaudeAgentOptions(**kw)


def _fold(messages: list, o: AgentOpts, t0: float) -> AgentResult:
    text_parts, session_id, cost, turns, structured = [], None, None, None, None
    result_text = None
    for m in messages:
        tn = type(m).__name__
        if tn == "AssistantMessage":
            for b in getattr(m, "content", []):
                t = getattr(b, "text", None)
                if t:
                    text_parts.append(t)
        elif tn == "ResultMessage":
            session_id = getattr(m, "session_id", session_id)
            cost = getattr(m, "total_cost_usd", None)
            turns = getattr(m, "num_turns", None)
            result_text = getattr(m, "result", None)
            structured = getattr(m, "structured_output", None)
        elif tn == "SystemMessage" and getattr(m, "subtype", "") == "init":
            session_id = getattr(m, "data", {}).get("session_id", session_id)
    text = "\n".join(text_parts) or (result_text or "")
    if structured is None and o.output_schema and result_text:
        try:
            structured = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            structured = None
    return AgentResult(text, session_id, structured, cost, turns,
                       int((time.time() - t0) * 1000))


async def run_once(task: str, o: AgentOpts, *, resume: str | None = None,
                   fork: bool = False, bare_llm: bool = False) -> AgentResult:
    from claude_agent_sdk import query
    t0 = time.time()
    msgs = []
    async for m in query(prompt=task, options=_sdk_options(o, resume=resume, fork=fork,
                                                           bare_llm=bare_llm)):
        msgs.append(m)
    return _fold(msgs, o, t0)


class Session:
    def __init__(self, client, o: AgentOpts):
        self._client = client
        self._o = o
        self.session_id: str | None = None
        self._messages: list = []

    async def _collect(self) -> AgentResult:
        t0 = time.time()
        batch = []
        async for m in self._client.receive_response():
            batch.append(m)
            self._messages.append({"type": type(m).__name__, "repr": repr(m)[:500]})
        r = _fold(batch, self._o, t0)
        self.session_id = r.session_id or self.session_id
        return r

    async def wait_result(self) -> AgentResult:
        return await self._collect()

    async def send(self, msg: str) -> AgentResult:
        await self._client.query(msg)
        return await self._collect()

    async def interrupt(self) -> None:
        try:
            await self._client.interrupt()
        except Exception:
            pass

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:
            pass

    def messages(self) -> list:
        return list(self._messages)


async def open_session(task: str | None, o: AgentOpts, *, resume: str | None = None) -> Session:
    from claude_agent_sdk import ClaudeSDKClient
    client = ClaudeSDKClient(options=_sdk_options(o, resume=resume))
    await client.connect()
    s = Session(client, o)
    if task:
        await client.query(task)
    else:
        # resumed target: no initial turn; wait_result is immediate no-op handled by agents.resume
        pass
    return s
```

- [ ] **Step 5: Bind in `runtime/__init__.py`**

```python
"""The in-kernel runtime API. Everything bind() puts in the user namespace is ALSO a
module-level export, so `from ptc.runtime import *` works as the spec promises (F9).
Later tasks extend _NAMES and the imports in lockstep."""
import asyncio

from .files import edit, read, write            # noqa: F401
from .shell import bash                          # noqa: F401

__all__ = ["read", "write", "edit", "bash", "agent", "asyncio"]
agent = None                                     # constructed per-kernel in bind()


def _make_agent():
    from . import claude_backend
    from .agents import _Agent
    from .state import STATE
    backends = {"claude": claude_backend}
    try:
        from . import codex_backend              # T22; absent until then is fine
        backends["codex"] = codex_backend
    except ImportError:
        pass
    return _Agent(STATE.config, backends)


def bind(ip) -> None:
    global agent
    agent = _make_agent()
    ns = {name: globals()[name] for name in __all__}
    ip.user_ns.update(ns)
```

(T23/T24/T25/T26 each add their names the same way: module-level import + `__all__` entry —
e.g. T23 adds `from .llm import llm` and `"llm"` to `__all__`; T24 `web_fetch`/`web_search`;
T25 `history`; T26 `workflow`. `bind()` never changes again.)

- [ ] **Step 6: Run unit tests**

```bash
uv run --group dev pytest test/unit/test_agents.py -q
```
Expected: 4 passed.

- [ ] **Step 7: Live test** (`test/live/test_agents_live.py`):

```python
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_run_spawn_gather_in_kernel(ptc_home):
    ensure_kernel("al1", cwd=str(ptc_home))
    kc = KernelClient("al1")
    out = kc.exec_cell(textwrap.dedent("""
        r = await agent.run("Reply with exactly: SOLO", max_turns=1)
        print("RUN:", r.text.strip()[:40], "sid:", bool(r.session_id))
        h1 = agent.spawn("Reply with exactly: P1", name="p1", max_turns=1)
        h2 = agent.spawn("Reply with exactly: P2", name="p2", max_turns=1)
        a, b = await agent.gather(h1, h2)
        print("GATHER:", "P1" in a.text, "P2" in b.text)
        print("LIST:", [e["name"] for e in agent.list()])
    """), timeout_s=420, config=Config.from_env())
    assert isinstance(out, Completed), out
    assert "RUN: SOLO" in out.output and "GATHER: True True" in out.output
    assert "p1" in out.output and "p2" in out.output
    kill_kernel("al1")
```

Run: `PTC_LIVE=1 uv run --group dev pytest test/live/test_agents_live.py -q` → expected: pass (2–5 min).

- [ ] **Step 8: Commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T20 — agent namespace (run/spawn/gather/send) + claude-agent-sdk backend"
```

---

### Task 21: `agent.fork`, `agent.resume`, registry survival across kernel restart

**Files:**
- Create: `ptc-surface/ptc/test/unit/test_agents_fork.py`
- Create: `ptc-surface/ptc/test/integration/test_registry_survival.py`
- Create: `ptc-surface/ptc/test/live/test_fork_live.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/agents.py` (only if the tests expose gaps — the T20 code already implements fork/resume)

**Interfaces:** consumes T20's `agent.fork/resume` and T13's `meta.json`. No new API.

- [ ] **Step 1: Unit tests for fork plumbing** (`test/unit/test_agents_fork.py`):

```python
import asyncio
import json

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_agents import FakeBackend  # reuse (same directory)

from ptc.runtime.agents import _Agent
from ptc.runtime.state import STATE


def _agent_with_meta(tmp_path, monkeypatch, sid):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    from ptc.discovery import write_meta
    STATE.kernel_dir = tmp_path / "kernels" / "k"
    STATE.kernel_dir.mkdir(parents=True)
    STATE.config = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 2}
    if sid:
        write_meta("k", claude_session_id=sid)
    return _Agent(STATE.config, {"claude": FakeBackend()})


def test_fork_uses_meta_session(tmp_path, monkeypatch):
    a = _agent_with_meta(tmp_path, monkeypatch, "real-uuid-1")
    r = asyncio.run(a.fork("what marker?"))
    assert r.text == "forked:what marker?" and r.session_id == "real-uuid-1"


def test_fork_errors_without_claude_session(tmp_path, monkeypatch):
    a = _agent_with_meta(tmp_path, monkeypatch, None)
    with pytest.raises(RuntimeError, match="no claude_session_id known"):
        asyncio.run(a.fork("x"))


def test_resume_then_send(tmp_path, monkeypatch):
    a = _agent_with_meta(tmp_path, monkeypatch, None)

    async def flow():
        h = a.resume("sess-9")
        await h.result()
        return await h.send("follow")
    r = asyncio.run(flow())
    assert r.text == "reply:follow"
```

- [ ] **Step 2: Integration — registry survives restart** (`test/integration/test_registry_survival.py`):

```python
import json
import textwrap

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel, restart_kernel
from ptc.paths import Config


def test_registry_survives_restart(ptc_home):
    ensure_kernel("rs1", cwd=str(ptc_home))
    reg = ptc_home / "kernels" / "rs1" / "agents.json"
    reg.write_text(json.dumps([{"name": "old-worker", "provider": "claude",
                                "session_id": "sess-1", "status": "done",
                                "created_at": 1.0, "last_turn_at": 2.0}]))
    restart_kernel("rs1")
    out = KernelClient("rs1").exec_cell(
        "print([e['name'] for e in agent.list()])", timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed) and "old-worker" in out.output
    kill_kernel("rs1")
```

- [ ] **Step 3: Live fork through the kernel** (`test/live/test_fork_live.py`) — this is acceptance A5's engine (the full A-cell runs in T28 inside a real conversation):

```python
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.discovery import write_meta
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_fork_recalls_parent_fact(ptc_home):
    import subprocess, json, re
    # create a real claude session containing a marker, capture its session id
    r = subprocess.run(["claude", "-p", "--output-format", "json",
                        "Remember: the launch code is ZEBRA-77. Say OK."],
                       capture_output=True, text=True, timeout=300)
    sid = json.loads(r.stdout)["session_id"]
    ensure_kernel("fl1", cwd=str(ptc_home), claude_session_id=sid)
    out = KernelClient("fl1").exec_cell(textwrap.dedent("""
        r = await agent.fork("What is the launch code? Reply with only the code.", max_turns=1)
        print("FORK:", r.text.strip())
    """), timeout_s=300, config=Config.from_env())
    assert isinstance(out, Completed) and "ZEBRA-77" in out.output
    kill_kernel("fl1")
```

- [ ] **Step 4: Run all three tiers' new tests, fix `agents.py` if any gap shows, commit**

```bash
uv run --group dev pytest test/unit/test_agents_fork.py test/integration/test_registry_survival.py -q
PTC_LIVE=1 uv run --group dev pytest test/live/test_fork_live.py -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T21 — fork via meta.json, resume+send, registry restart survival"
```

---

### Task 22: Codex backend (`codex app-server` stdio client)

Harden the S4 probe into `codex_backend.py` implementing the T20 backend protocol, applying S4's recorded wire shapes.

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/codex_backend.py`
- Create: `ptc-surface/ptc/test/unit/fake_codex_appserver.py`
- Create: `ptc-surface/ptc/test/unit/test_codex_backend.py`
- Create: `ptc-surface/ptc/test/live/test_codex_live.py`

**Interfaces:**
- Consumes: T20 protocol (`run_once`, `open_session`, `Session`), `AgentOpts`, `AgentResult`.
- Produces: `codex_backend.run_once/open_session` with `Session.session_id = thread id`; `fork` is never called on this backend (agents.py guards). Internal: `CodexProc` — one `codex app-server` subprocess per Session (and per run_once), NDJSON JSON-RPC, auto-accept responder for server→client requests, `CODEX_BIN` env override (tests point it at the fake).

- [ ] **Step 1: Write the fake app-server** (`test/unit/fake_codex_appserver.py`):

```python
#!/usr/bin/env python3
"""Just enough of `codex app-server` for unit tests: initialize, thread/start,
turn/start (echoes THE TASK back as an agentMessage), one approval round-trip."""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    threads = 0
    initialized = False
    for line in sys.stdin:
        msg = json.loads(line)
        if "method" not in msg:      # a response to our approval request
            continue
        m, rid, p = msg["method"], msg.get("id"), msg.get("params", {})
        if m == "initialize":
            ci = p.get("clientInfo", {})
            if not ci.get("name") or not ci.get("version"):
                send({"id": rid, "error": {"code": -32602,
                                           "message": "clientInfo.name and .version required"}})
                continue
            send({"id": rid, "result": {"userAgent": "fake-codex"}})
        elif m == "initialized":
            initialized = True       # notification, no reply
        elif not initialized and rid is not None:
            send({"id": rid, "error": {"code": -32600,
                                       "message": "initialized notification required first"}})
        elif m == "thread/start":
            threads += 1
            send({"id": rid, "result": {"thread": {"id": f"th-{threads}"}}})
        elif m == "thread/resume":
            send({"id": rid, "result": {"thread": {"id": p["threadId"]}}})
        elif m == "turn/start":
            text = p["input"][0]["text"]
            send({"id": rid, "result": {"turn": {"id": "t1", "status": "inProgress"}}})
            if "NEED-APPROVAL" in text:
                send({"id": 999, "method": "item/commandExecution/requestApproval",
                      "params": {"command": "rm -rf /tmp/x"}})
            send({"method": "item/completed",
                  "params": {"item": {"type": "agentMessage", "text": f"echo:{text}"}}})
            send({"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "completed"}}})
        elif m == "turn/interrupt":
            if not p.get("turnId"):
                send({"id": rid, "error": {"code": -32602, "message": "turnId required"}})
            else:
                send({"id": rid, "result": {}})


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing unit test** (`test/unit/test_codex_backend.py`):

```python
import asyncio
import sys
from pathlib import Path

from ptc.runtime.agents import AgentOpts
from ptc.runtime import codex_backend

FAKE = Path(__file__).parent / "fake_codex_appserver.py"


def _with_fake(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", f"{sys.executable} {FAKE}")


def test_run_once_roundtrip(monkeypatch):
    _with_fake(monkeypatch)
    r = asyncio.run(codex_backend.run_once("hello codex", AgentOpts(provider="codex")))
    assert r.text == "echo:hello codex" and r.session_id == "th-1"


def test_approval_is_auto_accepted(monkeypatch):
    _with_fake(monkeypatch)
    r = asyncio.run(codex_backend.run_once("NEED-APPROVAL task", AgentOpts(provider="codex")))
    assert r.text.startswith("echo:")            # turn completed despite the approval request


def test_session_send(monkeypatch):
    _with_fake(monkeypatch)

    async def flow():
        s = await codex_backend.open_session("first", AgentOpts(provider="codex"))
        r1 = await s.wait_result()
        r2 = await s.send("second")
        await s.close()
        return r1, r2
    r1, r2 = asyncio.run(flow())
    assert r1.text == "echo:first" and r2.text == "echo:second"
```

- [ ] **Step 3: Implement `codex_backend.py`**

```python
"""Codex worker via `codex app-server` (stdio NDJSON JSON-RPC). One subprocess per session."""
import asyncio
import json
import os
import shlex
import time

from .agents import AgentOpts, AgentResult

_APPROVAL_METHODS = ("requestApproval",)     # substring match on server→client requests


class CodexProc:
    def __init__(self):
        self._proc = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._agent_texts: list[str] = []
        self._turn_done: asyncio.Event = asyncio.Event()
        self._reader_task = None

    async def start(self):
        cmd = os.environ.get("CODEX_BIN", "codex app-server")
        argv = shlex.split(cmd)
        if argv[-1] != "app-server" and "fake" not in cmd:
            argv += ["app-server"]
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self._reader_task = asyncio.ensure_future(self._reader())
        # Full v2 handshake (F8): initialize carries name AND version, and the client
        # must follow with an `initialized` notification before any other method.
        from ptc import __version__
        await self.request("initialize",
                           {"clientInfo": {"name": "ptc", "version": __version__}})
        await self._send({"method": "initialized", "params": {}})

    async def _reader(self):
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                for f in self._pending.values():
                    if not f.done():
                        f.set_exception(RuntimeError("codex app-server closed"))
                self._turn_done.set()
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:                    # server→client request
                if any(a in msg["method"] for a in _APPROVAL_METHODS):
                    await self._send({"id": msg["id"], "result": {"decision": "accept"}})
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                f = self._pending.pop(msg["id"], None)
                if f and not f.done():
                    if "error" in msg:
                        f.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        f.set_result(msg["result"])
                continue
            m, p = msg.get("method"), msg.get("params", {})
            if m == "item/completed" and p.get("item", {}).get("type") == "agentMessage":
                self._agent_texts.append(p["item"].get("text", ""))
            elif m in ("turn/completed", "turn/failed"):
                self._turn_done.set()

    async def _send(self, obj):
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method, params, timeout: float = 60.0):
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"id": rid, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def turn(self, thread_id: str, text: str, timeout: float = 1800.0) -> str:
        self._agent_texts.clear()
        self._turn_done.clear()
        r = await self.request("turn/start",
                               {"threadId": thread_id, "input": [{"type": "text", "text": text}]})
        self.current_turn_id = (r.get("turn") or {}).get("id")     # retained for interrupt (F8)
        await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
        return "\n".join(t for t in self._agent_texts if t)

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()


def _thread_params(o: AgentOpts) -> dict:
    p = {"cwd": o.cwd or os.getcwd(), "approvalPolicy": "never",
         "sandbox": "workspaceWrite"}      # wire enum is camelCase (F8); S4 verifies
    if o.model:
        p["model"] = o.model
    if o.effort:
        p["effort"] = o.effort
    return p                     # field names above are schema-valid for the vendored
                                 # app-server protocol; S4's live run re-verifies them


def _thread_id(result: dict) -> str:
    th = result.get("thread") or result
    return th.get("id") or th.get("threadId")


class Session:
    def __init__(self, proc: CodexProc, thread_id: str, o: AgentOpts, first: str | None):
        self._proc = proc
        self._o = o
        self.session_id = thread_id
        self._first = first
        self._msgs: list[dict] = []

    async def wait_result(self) -> AgentResult:
        t0 = time.time()
        text = await self._proc.turn(self.session_id, self._first) if self._first else ""
        self._first = None
        self._msgs.append({"type": "turn", "text": text})
        return AgentResult(text, self.session_id, None, None, None,
                           int((time.time() - t0) * 1000))

    async def send(self, msg: str) -> AgentResult:
        t0 = time.time()
        text = await self._proc.turn(self.session_id, msg)
        self._msgs.append({"type": "turn", "text": text})
        return AgentResult(text, self.session_id, None, None, None,
                           int((time.time() - t0) * 1000))

    async def interrupt(self) -> None:
        tid = getattr(self._proc, "current_turn_id", None)
        if not tid:
            return
        try:
            await self._proc.request("turn/interrupt",
                                     {"threadId": self.session_id, "turnId": tid})
        except Exception:
            pass

    async def close(self) -> None:
        await self._proc.close()

    def messages(self) -> list:
        return list(self._msgs)


async def open_session(task: str | None, o: AgentOpts, *, resume: str | None = None) -> Session:
    proc = CodexProc()
    await proc.start()
    if resume:
        result = await proc.request("thread/resume", {"threadId": resume})
    else:
        result = await proc.request("thread/start", _thread_params(o))
    return Session(proc, _thread_id(result), o, task)


async def run_once(task: str, o: AgentOpts, *, resume: str | None = None,
                   fork: bool = False) -> AgentResult:
    if fork:
        raise NotImplementedError("codex fork; use provider='claude'")
    s = await open_session(task, o, resume=resume)
    try:
        return await s.wait_result()
    finally:
        await s.close()
```

- [ ] **Step 4: Run unit tests; live test** (`test/live/test_codex_live.py`):

```python
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_codex_worker_in_kernel(ptc_home):
    ensure_kernel("cx1", cwd=str(ptc_home))
    out = KernelClient("cx1").exec_cell(textwrap.dedent("""
        r = await agent.run("Reply with exactly: CODEX-DONE", provider="codex")
        print("CODEX:", r.text.strip()[:60])
    """), timeout_s=600, config=Config.from_env())
    assert isinstance(out, Completed) and "CODEX-DONE" in out.output
    kill_kernel("cx1")
```

```bash
uv run --group dev pytest test/unit/test_codex_backend.py -q
PTC_LIVE=1 uv run --group dev pytest test/live/test_codex_live.py -q
```

- [ ] **Step 5: Commit** (`f5(ptc): T22 — codex app-server backend with auto-accept approvals`).

---

### Task 23: `llm()`

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/llm.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind `llm`)
- Create: `ptc-surface/ptc/test/unit/test_llm.py`
- Create: `ptc-surface/ptc/test/live/test_llm_live.py`

**Interfaces:**
- Consumes: `claude_backend.run_once(..., bare_llm=True)` (T20), the shared semaphore via `agent` — implementation detail: `llm` takes the same `_Agent._semaphore()`; to avoid coupling, `llm.py` owns a module-level `asyncio.Semaphore` initialized from `STATE.config["max_concurrency"]`. Both semaphores existing is acceptable (spec bounds "SDK-spawning calls"; two pools of 8 → cap 16 worst-case) — **no**: spec says ONE semaphore across agent/llm/web_search. Implementation: `agents.py` exposes `shared_semaphore()` (module-level, lazily created from STATE.config); `_Agent._semaphore()` and `llm.py` and `web.py` all call it.
- Produces: `await llm(prompt, *, model="haiku", system=None, json_schema=None, timeout=300) -> str | dict`.

- [ ] **Step 1: Refactor the semaphore into `agents.shared_semaphore()`** — move the body of `_Agent._semaphore` to module level:

```python
# agents.py, module level
_SHARED_SEM: asyncio.Semaphore | None = None


def shared_semaphore() -> asyncio.Semaphore:
    global _SHARED_SEM
    if _SHARED_SEM is None:
        _SHARED_SEM = asyncio.Semaphore(int(STATE.config.get("max_concurrency", 8)))
    return _SHARED_SEM
```

and `_Agent._semaphore = staticmethod(shared_semaphore)` (drop the instance `_sem`). Re-run `test/unit/test_agents.py` — the concurrency test still holds because FakeBackend counts its own concurrency. Add a `reset` hook for tests: `def _reset_semaphore(): global _SHARED_SEM; _SHARED_SEM = None`, called in test fixtures that change `max_concurrency`.

- [ ] **Step 2: Write the failing unit test** (`test/unit/test_llm.py`):

```python
import asyncio

from ptc.runtime import llm as llm_mod
from ptc.runtime.state import STATE


def test_llm_calls_backend_bare(monkeypatch, tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8, "depth": 0, "max_depth": 1}
    seen = {}

    async def fake_run_once(task, o, *, resume=None, fork=False, bare_llm=False):
        seen.update(task=task, model=o.model, system=o.system, bare=bare_llm,
                    schema=o.output_schema)
        from ptc.runtime.agents import AgentResult
        return AgentResult('{"a": 1}', "s", {"a": 1} if o.output_schema else None, 0, 1, 5)

    monkeypatch.setattr(llm_mod, "_run_once", fake_run_once)
    out = asyncio.run(llm_mod.llm("classify this", model="sonnet", system="be terse"))
    assert out == '{"a": 1}' and seen["bare"] and seen["model"] == "sonnet"
    out2 = asyncio.run(llm_mod.llm("classify", json_schema={"type": "object"}))
    assert out2 == {"a": 1}


def test_llm_timeout(monkeypatch, tmp_path):
    STATE.config = {"key": "k", "max_concurrency": 8}

    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(llm_mod, "_run_once", slow)
    import pytest
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(llm_mod.llm("x", timeout=0.05))
```

- [ ] **Step 3: Implement** (`src/ptc/runtime/llm.py`):

```python
"""One-shot sub-LM call: no tools, one turn, subscription-billed via the claude CLI."""
import asyncio

from .agents import AgentOpts, shared_semaphore
from .claude_backend import run_once as _run_once


async def llm(prompt: str, *, model: str = "haiku", system: str | None = None,
              json_schema: dict | None = None, timeout: float = 300.0):
    o = AgentOpts(model=model, system=system or "Answer directly and concisely. No preamble.",
                  output_schema=json_schema, permission_mode="bypassPermissions")
    async with shared_semaphore():
        r = await asyncio.wait_for(_run_once(prompt, o, bare_llm=True), timeout=timeout)
    if json_schema:
        return r.structured if r.structured is not None else r.text
    return r.text
```

Bind `"llm": llm` in `runtime/__init__.bind`.

- [ ] **Step 4: Live check** (`test/live/test_llm_live.py`):

```python
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_llm_gather_map_reduce(ptc_home):
    ensure_kernel("ll1", cwd=str(ptc_home))
    out = KernelClient("ll1").exec_cell(textwrap.dedent("""
        words = ["ocean", "volcano", "glacier", "desert", "forest"]
        outs = await asyncio.gather(*[
            llm(f"Reply with exactly one word: the temperature (hot or cold) of a {w}")
            for w in words])
        print("N:", len(outs), "nonempty:", all(bool(o.strip()) for o in outs))
    """), timeout_s=420, config=Config.from_env())
    assert isinstance(out, Completed) and "N: 5 nonempty: True" in out.output
    kill_kernel("ll1")
```

```bash
uv run --group dev pytest test/unit/test_llm.py test/unit/test_agents.py -q
PTC_LIVE=1 uv run --group dev pytest test/live/test_llm_live.py -q
```

- [ ] **Step 5: Commit** (`f5(ptc): T23 — llm() one-shot with shared semaphore`).

---

# Milestone M3 — web + history + polish

### Task 24: Spike S6 + `web_fetch` / `web_search`

**Files:**
- Create: `ptc-surface/ptc/test/spikes/s6_websearch_shape.py`
- Create: `ptc-surface/ptc/src/ptc/runtime/web.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind both)
- Create: `ptc-surface/ptc/test/unit/test_web.py`
- Create: `ptc-surface/ptc/test/live/test_web_live.py`

**Interfaces:**
- Produces (kernel namespace): `await web_fetch(url, *, prompt=None, timeout=30) -> FetchResult(url, status, title, text, summary)`; `await web_search(query, *, allowed_domains=None, blocked_domains=None, max_results=10, timeout=300) -> list[SearchResult(title, url, snippet, raw)]`.

- [ ] **Step 1: Spike S6** (`test/spikes/s6_websearch_shape.py`) — question: where do WebSearch's structured results appear in the SDK stream? Spec criteria verbatim: *"Promote → clean field mapping into `SearchResult`. Fallback: best-effort extraction with `SearchResult.raw` retaining the source block — the return type is `list[SearchResult]` either way."*

```python
"""S6: dump every message/block from a WebSearch-only SDK query. PTC_LIVE=1 required."""
import asyncio
import os


async def main():
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP"); return
    from claude_agent_sdk import ClaudeAgentOptions, query
    opts = ClaudeAgentOptions(tools=["WebSearch"], allowed_tools=["WebSearch"],
                              permission_mode="bypassPermissions", max_turns=2)
    async for m in query(prompt="Search the web for: anthropic claude agent sdk release notes",
                         options=opts):
        print("==", type(m).__name__)
        for b in getattr(m, "content", []) or []:
            print("   block:", type(b).__name__, repr(b)[:800])

asyncio.run(main())
```

Run `PTC_LIVE=1 uv run --group dev python test/spikes/s6_websearch_shape.py`; paste the ToolResultBlock (or equivalent) repr into the spec's Surprises & Discoveries with the verdict, and copy one real block into the unit-test fixture below (replacing the placeholder fixture with the observed shape).

- [ ] **Step 2: Implement `web.py`**

```python
"""web_fetch (pure httpx) and web_search (WebSearch-scoped one-shot SDK query)."""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from .agents import AgentOpts, shared_semaphore


@dataclass
class FetchResult:
    url: str
    status: int
    title: str
    text: str
    summary: str | None = None


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    raw: object = None


async def web_fetch(url: str, *, prompt: str | None = None, timeout: float = 30.0) -> FetchResult:
    import httpx
    from markdownify import markdownify
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                 headers={"User-Agent": "ptc/0.1"}) as c:
        r = await c.get(url)
    if len(r.content) > 10_000_000:
        raise ValueError(f"response too large ({len(r.content)} bytes > 10 MB): {url}")
    html = r.text
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = (m.group(1).strip() if m else "")[:300]
    text = markdownify(html) if "<" in html else html
    out = FetchResult(str(r.url), r.status_code, title, text)
    if prompt:
        from .llm import llm
        out.summary = await llm(f"{prompt}\n\n<page url={url}>\n{text[:200_000]}\n</page>")
    return out


def _parse_blocks(blocks: list) -> list[SearchResult]:
    """Map tool_result content into SearchResult; shape pinned by spike S6."""
    results: list[SearchResult] = []
    for b in blocks:
        content = getattr(b, "content", b)
        items = content if isinstance(content, list) else [content]
        for it in items:
            d = it if isinstance(it, dict) else getattr(it, "__dict__", {})
            text = d.get("text") if isinstance(d, dict) else None
            if isinstance(d, dict) and d.get("title") and d.get("url"):
                results.append(SearchResult(d["title"], d["url"], d.get("snippet", ""), raw=d))
            elif text:
                # fallback: extract "Title (URL)"-ish lines and bare links
                for mm in re.finditer(r"(?:\"([^\"]+)\"|\[([^\]]+)\])?\s*\(?(https?://[^\s)\"]+)\)?", text):
                    title = mm.group(1) or mm.group(2) or ""
                    results.append(SearchResult(title.strip(), mm.group(3), raw=text))
    seen, out = set(), []
    for r in results:
        if r.url not in seen:
            seen.add(r.url)
            out.append(r)
    return out


async def web_search(query_text: str, *, allowed_domains: list | None = None,
                     blocked_domains: list | None = None, max_results: int = 10,
                     timeout: float = 300.0) -> list[SearchResult]:
    from claude_agent_sdk import ClaudeAgentOptions, query
    hints = []
    if allowed_domains:
        hints.append(f"Only include results from these domains: {', '.join(allowed_domains)}.")
    if blocked_domains:
        hints.append(f"Exclude these domains: {', '.join(blocked_domains)}.")
    prompt = f"Search the web for: {query_text}. {' '.join(hints)}"
    opts = ClaudeAgentOptions(tools=["WebSearch"], allowed_tools=["WebSearch"],
                              permission_mode="bypassPermissions", max_turns=2,
                              env={"PTC_SESSION": "websearch-oneshot"})
    blocks: list = []
    async def run():
        async for m in query(prompt=prompt, options=opts):
            for b in getattr(m, "content", []) or []:
                if "ToolResult" in type(b).__name__ or getattr(b, "type", "") == "tool_result":
                    blocks.append(b)
    async with shared_semaphore():
        await asyncio.wait_for(run(), timeout=timeout)
    return _parse_blocks(blocks)[:max_results]
```

Bind `"web_fetch": web.web_fetch, "web_search": web.web_search`.

- [ ] **Step 3: Unit tests** (`test/unit/test_web.py`) — local HTTP server for fetch; S6 fixture for parse:

```python
import asyncio
import http.server
import threading

from ptc.runtime.web import _parse_blocks, web_fetch


def test_web_fetch_markdown_and_title(tmp_path):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><head><title>T1</title></head><body><h1>Hello</h1><p>World</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        r = asyncio.run(web_fetch(f"http://127.0.0.1:{srv.server_port}/"))
        assert r.status == 200 and r.title == "T1"
        assert "Hello" in r.text and "World" in r.text
    finally:
        srv.shutdown()


def test_parse_blocks_structured_and_text():
    # REPLACE the dicts below with one real captured S6 block shape after the spike.
    structured = [{"content": [{"title": "Doc A", "url": "https://a.example", "snippet": "s"}]}]

    class B:  # duck-typed block
        content = structured[0]["content"]
    out = _parse_blocks([B()])
    assert out[0].title == "Doc A" and out[0].url == "https://a.example"

    class T:
        content = [{"text": 'Found "Doc B" (https://b.example/page) and more'}]
    out2 = _parse_blocks([T()])
    assert any(r.url == "https://b.example/page" for r in out2)
```

- [ ] **Step 4: Live test** (`test/live/test_web_live.py`):

```python
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_web_search_structured(ptc_home):
    ensure_kernel("w1", cwd=str(ptc_home))
    out = KernelClient("w1").exec_cell(textwrap.dedent("""
        rs = await web_search("anthropic claude release notes")
        print("N:", len(rs), "urls_ok:", all(r.url.startswith("http") for r in rs))
    """), timeout_s=420, config=Config.from_env())
    assert isinstance(out, Completed) and "urls_ok: True" in out.output and "N: 0" not in out.output
    kill_kernel("w1")
```

```bash
uv run --group dev pytest test/unit/test_web.py -q
PTC_LIVE=1 uv run --group dev pytest test/live/test_web_live.py -q
```

- [ ] **Step 5: Commit** (`f5(ptc): T24 — web_fetch/web_search with S6-pinned parsing`).

---

### Task 25: `history()` — lossless transcript access

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/transcript.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind `history`)
- Create: `ptc-surface/ptc/test/unit/test_transcript.py`

**Interfaces:**
- Produces (kernel namespace): `history(session=None) -> Transcript` with `.path`, `.messages` (list[dict], the raw JSONL rows), `.user() -> list[str]`, `.assistant() -> list[str]`, `.tool_calls(name=None) -> list[dict]`, `.search(regex) -> list[dict]`, `.text() -> str`. Default session: kernel `meta.json`'s `claude_session_id`; RuntimeError when unknown. Resolution: `~/.claude/projects/<munge(cwd)>/<sid>.jsonl` where `munge = re.sub(r"[^A-Za-z0-9]", "-", cwd)`; fallback: glob `~/.claude/projects/*/<sid>.jsonl`.

- [ ] **Step 1: Write the failing test** (`test/unit/test_transcript.py`):

```python
import json

import pytest

from ptc.runtime import transcript
from ptc.runtime.state import STATE

ROWS = [
    {"type": "user", "message": {"role": "user", "content": "first question"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "an answer"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "file1\nfile2"}]}},
]


def _fake_home(tmp_path, monkeypatch, sid="s-42", cwd="/my/proj"):
    munged = "".join(c if c.isalnum() else "-" for c in cwd)
    d = tmp_path / ".claude" / "projects" / munged
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS))
    monkeypatch.setenv("HOME", str(tmp_path))
    return d / f"{sid}.jsonl"


def test_history_resolves_and_projects(tmp_path, monkeypatch):
    p = _fake_home(tmp_path, monkeypatch)
    h = transcript.history("s-42", cwd="/my/proj")
    assert h.path == p and len(h.messages) == 3
    assert h.user() == ["first question"]
    assert h.assistant() == ["an answer"]
    assert h.tool_calls()[0]["name"] == "Bash"
    assert h.tool_calls("Grep") == []
    assert len(h.search(r"file\d")) == 1
    assert "an answer" in h.text()


def test_history_glob_fallback(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch, cwd="/other/place")
    h = transcript.history("s-42", cwd="/wrong/cwd")     # munge misses; glob finds
    assert len(h.messages) == 3


def test_history_default_needs_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "p"))
    STATE.config = {"key": "nometa"}
    with pytest.raises(RuntimeError, match="no claude_session_id known"):
        transcript.history()
```

- [ ] **Step 2: Implement** (`src/ptc/runtime/transcript.py`):

```python
"""Lossless access to Claude Code session transcripts (the PRO-LONG lever)."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .state import STATE


def _munge(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _resolve_path(session_id: str, cwd: str | None) -> Path:
    root = Path.home() / ".claude" / "projects"
    if cwd:
        p = root / _munge(cwd) / f"{session_id}.jsonl"
        if p.exists():
            return p
    hits = list(root.glob(f"*/{session_id}.jsonl"))
    if hits:
        return max(hits, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError(f"no transcript found for session {session_id!r} under {root}")


@dataclass
class Transcript:
    path: Path
    messages: list = field(default_factory=list)

    def _texts(self, role: str) -> list:
        out = []
        for row in self.messages:
            if row.get("type") != role:
                continue
            content = row.get("message", {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                t = "".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
                if t:
                    out.append(t)
        return out

    def user(self) -> list:
        return self._texts("user")

    def assistant(self) -> list:
        return self._texts("assistant")

    def tool_calls(self, name: str | None = None) -> list:
        out = []
        for row in self.messages:
            content = row.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    if name is None or b.get("name") == name:
                        out.append(b)
        return out

    def search(self, pattern: str) -> list:
        rx = re.compile(pattern)
        return [row for row in self.messages if rx.search(json.dumps(row))]

    def text(self) -> str:
        return "\n".join(self.user() + self.assistant())


def history(session: str | None = None, cwd: str | None = None) -> Transcript:
    if session is None:
        from ptc.discovery import read_meta
        session = read_meta(STATE.config.get("key", "")).get("claude_session_id")
        if not session:
            raise RuntimeError("no claude_session_id known for this kernel "
                               "(alias-keyed session) — pass history(session=...) explicitly")
    cwd = cwd or STATE.config.get("cwd")
    path = _resolve_path(session, cwd)
    messages = []
    for line in path.read_text().splitlines():
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return Transcript(path, messages)
```

Bind `"history": transcript.history`.

- [ ] **Step 3: Run, commit**

```bash
uv run --group dev pytest test/unit/test_transcript.py -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T25 — history(): munged-path + glob transcript resolution and projections"
```

---

### Task 26: `workflow` helpers

**Files:**
- Create: `ptc-surface/ptc/src/ptc/runtime/wf.py`
- Modify: `ptc-surface/ptc/src/ptc/runtime/__init__.py` (bind `workflow`)
- Create: `ptc-surface/ptc/test/unit/test_wf.py`

**Interfaces:**
- Produces (kernel namespace): `workflow.parallel(*aws, limit=8) -> list` (order-preserving; exceptions captured in-place as the exception object), `workflow.pipeline(items, *stages) -> list` (per-item chaining, no inter-stage barrier; a stage exception drops that item to the exception object), `workflow.phase(name)` (prints `── phase: <name>` and audits).

- [ ] **Step 1: Failing tests** (`test/unit/test_wf.py`):

```python
import asyncio

from ptc.runtime import wf
from ptc.runtime.state import STATE


def test_parallel_bounded_order_and_errors(tmp_path):
    STATE.kernel_dir = tmp_path
    peak = {"now": 0, "max": 0}

    async def job(i):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.02)
        peak["now"] -= 1
        if i == 3:
            raise ValueError("boom")
        return i * 10

    out = asyncio.run(wf.parallel(*(job(i) for i in range(6)), limit=2))
    assert peak["max"] <= 2
    assert out[0] == 0 and out[5] == 50
    assert isinstance(out[3], ValueError)


def test_pipeline_no_barrier(tmp_path):
    STATE.kernel_dir = tmp_path
    order = []

    async def s1(x):
        await asyncio.sleep(0.05 if x == "slow" else 0)
        order.append(("s1", x))
        return x

    async def s2(x):
        order.append(("s2", x))
        return f"{x}!"

    out = asyncio.run(wf.pipeline(["slow", "fast"], s1, s2))
    assert out == ["slow!", "fast!"]
    assert order.index(("s2", "fast")) < order.index(("s1", "slow"))   # fast finished both stages first


def test_phase_prints_and_audits(tmp_path, capsys):
    STATE.kernel_dir = tmp_path
    STATE.current_cell = 1
    STATE.cell_mutations = []
    wf.phase("collect")
    assert "phase: collect" in capsys.readouterr().out
```

- [ ] **Step 2: Implement** (`src/ptc/runtime/wf.py`):

```python
"""Thin orchestration helpers — PTC itself is the workflow language."""
import asyncio

from . import audit


async def parallel(*aws, limit: int = 8) -> list:
    sem = asyncio.Semaphore(limit)

    async def guarded(aw):
        async with sem:
            try:
                return await aw
            except Exception as e:                 # noqa: BLE001 — captured for the caller
                return e
    return list(await asyncio.gather(*(guarded(a) for a in aws)))


async def pipeline(items, *stages) -> list:
    async def chain(item):
        v = item
        for s in stages:
            try:
                v = await s(v)
            except Exception as e:                 # noqa: BLE001
                return e
        return v
    return list(await asyncio.gather(*(chain(i) for i in items)))


def phase(name: str) -> None:
    print(f"── phase: {name}")
    audit.append("phase", name=name)


class _Workflow:
    parallel = staticmethod(parallel)
    pipeline = staticmethod(pipeline)
    phase = staticmethod(phase)


workflow = _Workflow()
```

Bind `"workflow": wf.workflow`. Note: `shape.footer_line` ignores kind `phase` (it only renders write/edit/bash/agent) — that is intended; phases are visible in stdout.

- [ ] **Step 3: Run, commit** (`f5(ptc): T26 — workflow.parallel/pipeline/phase`).

---

### Task 27: Skill final pass + README + server instructions review

**Files:**
- Modify: `ptc-surface/ptc/plugin/skills/ptc/SKILL.md`
- Modify: `ptc-surface/ptc/src/ptc/mcp.py` (INSTRUCTIONS only, if drifted)
- Create: `ptc-surface/ptc/README.md`

- [ ] **Step 1: Review SKILL.md against the spec's "SKILL.md content contract"** — all ten sections present, in order; the exact function signatures match what shipped (T14/T15/T20–T26); **confirm no search guidance anywhere**; confirm the depth default, TTL default (24 h), and `bypassPermissions` warnings match the Configuration reference. Update any drift; keep total length ~150 lines.

- [ ] **Step 2: Write `README.md`** with exactly these sections (content sourced from the spec — do not invent new claims):

```markdown
# ptc — Programmatic Tool Calling for Claude Code

A persistent per-session IPython kernel exposed to Claude Code as an MCP server, with
Claude-Code-equivalent tools pre-bound as Python functions: `read`, `write`, `edit`,
`bash`, `agent` (Claude + Codex children), `llm`, `web_fetch`, `web_search`, `history`,
`workflow`.

## Install

    cd ptc/ && uv run ptc setup          # provision ~/.ptc/venv (one-time)
    claude --plugin-dir ./plugin         # dev install

Recommended settings (`~/.claude/settings.json`) for prompt-free use:

    {"permissions": {"allow": ["mcp__ptc__exec", "mcp__ptc__wait",
                               "mcp__ptc__interrupt", "mcp__ptc__restart",
                               "mcp__ptc__kernels"]}}

## Trust model — read this

Allowing `mcp__ptc__exec` IS the security decision: from then on, model-written Python
runs with your OS permissions, outside Claude Code's per-tool permission prompts and
sandbox, and children spawned from the kernel default to `bypassPermissions`. The
mutation footer and `~/.ptc/kernels/<session>/audit.jsonl` give visibility, not
enforcement. Use a worktree or container for untrusted work.

Billing: all kernel-originated model calls go through the `claude` CLI (your
subscription when OAuth-logged-in). Do not put `ANTHROPIC_API_KEY` in the environment —
it silently shadows OAuth and flips billing to the metered API.

## Lifecycle

One detached kernel per Claude Code session, discovered via a SessionStart hook.
State survives `--resume` until the idle TTL (default 24 h; `PTC_IDLE_HOURS`).
`ptc list | kill | restart | doctor` manage kernels from any shell.

## Codex (documented, untested)

    codex mcp add ptc -- /abs/path/to/ptc/plugin/bin/ptc-launch

Session keying degrades to explicit `session=` / `PTC_SESSION`.

## Configuration

| env | default | meaning |
|---|---|---|
| PTC_HOME | ~/.ptc | root |
| PTC_YIELD_S | 300 | exec/wait yield timeout |
| PTC_MAX_OUTPUT_CHARS | 12000 | result cap (server clamp 50000) |
| PTC_IDLE_HOURS | 24 | kernel TTL |
| PTC_MAX_CONCURRENCY | 8 | SDK-call semaphore |
| PTC_MAX_DEPTH | 1 | agent recursion brake |

POSIX only (macOS/Linux). Python 3.12.
```

- [ ] **Step 3: Re-read `mcp.py::INSTRUCTIONS`** against the final skill — they must agree on names and the yield/busy story; fix drift.

- [ ] **Step 4: Full keyless suite + commit**

```bash
uv run --group dev pytest test/unit test/integration -q
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc
git commit -m "f5(ptc): T27 — skill final, README with trust model, instructions sync"
```

---

### Task 28: Final verification — spec acceptance A1–A15

Execute the spec's acceptance section as written (spec `## Acceptance`), plus the full suites. Where an A-cell is phrased interactively, drive it with `claude -p` / `--resume` exactly as the spec's commands do. Record every result.

- [ ] **Step 1: Full keyless tier (non-skippable)**

```bash
cd ptc-surface/ptc && uv run --group dev pytest test/unit test/integration -q
```
Expected: ALL pass, ZERO skips. Per the spec: *"A milestone's exit criteria are satisfied only by this tier plus the live tier actually passing — a skipped live test satisfies nothing."*

- [ ] **Step 2: Full live tier**

```bash
PTC_LIVE=1 uv run --group dev pytest test/live -q
```
Expected: all pass (bills subscription quota; ~10–20 min).

- [ ] **Step 3: Walk the A-cells.** For each, run and record PASS/FAIL + evidence. The spec's cells, executed as:
  - **A1/A2** (state persists; survives resume within TTL): interactive `claude --plugin-dir ./plugin`; `exec("x = 42")`; quit; `claude --resume <S> --plugin-dir ./plugin`; `exec("print(x)")` → `42`, pid unchanged via `kernels`. Keyless TTL companion is `test/integration/test_ttl.py` (already green).
  - **A3** (yield/wait/interrupt/fresh-adapter/busy): covered by `test/integration/test_busy_yield_wait.py` + live re-check of the yield inside a real conversation.
  - **A4** (fan-out/fan-in + registry + resume): `test/live/test_agents_live.py` + in-conversation spawn/gather; after `restart`, `agent.list()` still shows children and `agent.resume(<id>)` + `.send()` replies.
  - **A5** (fork recalls parent conversation): in a real session, discuss a marker fact, then `await agent.fork("what marker fact did we establish? answer only the fact")` → the fact.
  - **A6** (audit footer): `test/integration/test_mutation_footer.py` + visual check in a session.
  - **A7** (web_search structured): `test/live/test_web_live.py`; assert kernel env has no `ANTHROPIC_API_KEY`.
  - **A8** (llm map-reduce): `test/live/test_llm_live.py`.
  - **A9** (history incl. post-compact): in a session with ≥2 turns: `history().user()` contains the first prompt verbatim; run `/compact`; ask again → still present. If `/compact` cannot be driven scriptably, run this cell interactively and record the observation.
  - **A10** (CLI shares kernel): from the session's Bash tool: `~/.ptc/venv/bin/ptc exec 'print(x)'` → `42` (keyed by `CLAUDE_CODE_SESSION_ID`).
  - **A11** (skill triggers unprompted): `test/live/test_plugin_smoke.py` — transcript contains an `mcp__ptc__exec` call.
  - **A12** (truncation): `test/integration/test_mcp_tools.py::test_truncation_and_clamp`.
  - **A13** (images): per the S5 verdict — image block visible, or the documented saved-PNG fallback.
  - **A14** (depth guard): in a session: `await agent.run("Use your ptc kernel: run agent.run('hi') and paste the exact error")` → child's output contains `agent depth limit reached (PTC_DEPTH=1`.
  - **A15** (codex worker): `test/live/test_codex_live.py`.

- [ ] **Step 4: Record the acceptance run in the spec.** Append to `## Surprises & Discoveries`:

```markdown
- Observation: acceptance run <date>: A1–A15 → <n> pass / <list any fail or fallback,
  each with one line of why and what was changed or recorded>.
  Evidence: keyless suite <n> passed 0 skipped; live suite <n> passed; per-cell notes above.
```

Fix anything that failed before closing the milestone; a documented spike fallback (S5/S6) counts as pass **in its fallback form**.

- [ ] **Step 5: Final commit**

```bash
cd /Users/new/Developer/GitHub/codex_somersault
git add ptc-surface/ptc ptc-surface/docs
git commit -m "f5(ptc): T28 — acceptance A1–A15 executed and recorded; M3 complete"
```
