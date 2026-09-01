# Immutable Per-Build Venvs (v0.3 initiative 1) Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use doperpowers:subagent-driven-execution to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running PTC kernel survives plugin upgrades: provisioning creates side-by-side immutable venvs under `~/.ptc/venvs/<build_id>/` instead of rebuilding `~/.ptc/venv` in place, so an upgrade never deletes the tree a live kernel stands on.

**Architecture:** Build identity becomes a path-independent content hash (pyproject + lock + src tree) computed only by the two provisioners (launcher and `ensure_venv`); every provisioned venv carries its stamp inside, so runtimes identify themselves from `sys.prefix` and never need the source. The kernel records which venv it launched from; attach recycles only when that venv is GONE or the protocol integer mismatches — a mere build difference is one header notice. GC (adapter startup) deletes unreferenced builds past a 72 h grace, deferring entirely while any spawn is mid-bootstrap.

**Tech Stack:** Python 3.12, `uv` (locked sync, `--no-editable`), pytest.

**Spec:** `docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md`, section `## Structural follow-on (v0.3 line)` → "Initiative 1 — kernel survivability: immutable per-build venvs" (plus its Decision Log entries dated 2026-09-01). Conflicts found during execution resolve against the spec.

## Global Constraints

- Python for provisioned venvs: **3.12** (`uv venv --python 3.12 --seed`).
- Provisioning installs the checked-in `uv.lock`, never a fresh resolution: `uv sync --locked --inexact --no-dev --extra kernel` — initiative 1 adds `--no-editable`.
- `bin/ptc-launch` runs under **system python3, stdlib only** — it may not import `ptc`. Its payload computation MUST stay semantically identical to `ptc.venv.stamp_payload()` (both sides say so in comments today; keep both comments true).
- Everything created under PTC_HOME is owner-only: dirs 0700, files 0600 (`paths.secure_dir`, `paths.private_open`). New dirs/files follow this.
- Stamp payload schema for the new scheme: `{"schema": 3, "pyproject_sha", "lock_sha", "src_sha"}` — **no package path**. `build_id` = first 12 hex of `sha256(json.dumps(payload, sort_keys=True))`.
- Protocol constant: `PTC_PROTOCOL = 1` in `src/ptc/paths.py`. Absent protocol in meta reads as 0.
- GC grace: 72 h (`72 * 3600.0` seconds, a named default parameter, overridable in tests).
- Notice texts (verbatim, tests pin substrings of them):
  - build note: `kernel running build {was[:12]} (current {now[:12]}) — restart() to pick up the new runtime`
  - venv gone: `replaced because the venv it was running from ({path}) no longer exists — removed by GC or by hand`
  - protocol: `replaced after a ptc protocol change (kernel protocol {have}, current {PTC_PROTOCOL})`
- Do not bump plugin versions or push; the controller does bookkeeping after the final review.
- Run tests with `uv run pytest …` from the repo root (`/Users/new/Developer/GitHub/ptc-tool`).

**Orientation for the executor (read once):** today `~/.ptc/venv` is ONE directory; `bin/ptc-launch` and `src/ptc/venv.py::ensure_venv` both rebuild it in place with `uv venv --clear` when the stamp (`.ptc-version`) mismatches, and `src/ptc/kernel.py::_upgraded_under` deliberately recycles any live kernel whose recorded `build` no longer matches — that recycle is the state annihilation this plan removes. The existing test conventions you will reuse: unit venv tests inject a fake `run` and set `PTC_HOME` to a tmp dir (`test/unit/test_venv.py`); integration tests get a real cached venv via the session-scoped `kernel_venv` fixture and an isolated home via `ptc_home` (`test/conftest.py` — note it symlinks `home/venv` at the cache; that symlink is why GC must skip symlinks).

---

### Task 1: Path-independent identity + versioned provisioning (`ptc.venv`)

**Files:**
- Modify: `src/ptc/paths.py` (add `venvs_root()`, `PTC_PROTOCOL`)
- Modify: `src/ptc/venv.py` (schema-3 payload, `src_tree_sha`, `build_id`, `runtime_venv`, `spawn_venv`, `_identity_of`, rewritten `ensure_venv`/`stamp_current`/`build_identity`/`venv_python`)
- Modify: `src/ptc/cli.py:108-125` (doctor: no-source guard)
- Test: `test/unit/test_venv.py` (rewrite provisioning tests, add identity tests)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (later tasks rely on these exact names):
  - `paths.venvs_root() -> Path` (= `ptc_home()/"venvs"`), `paths.PTC_PROTOCOL: int = 1`
  - `venv.stamp_payload() -> dict` (schema 3, raises `OSError` when no source tree beside the package)
  - `venv.src_tree_sha(root: Path | None = None) -> str`
  - `venv.build_id(payload: dict | None = None) -> str` (12 hex)
  - `venv.build_venv_dir(bid: str) -> Path`
  - `venv.runtime_venv() -> Path | None` (this process's own provisioned venv via `sys.prefix`, else None)
  - `venv.spawn_venv() -> Path` (= `runtime_venv() or paths.venv_dir()` — the legacy dir stays the dev/test fallback)
  - `venv.venv_python() -> Path` (= `spawn_venv()/"bin"/"python"` — name kept; `kernel.py` and `client.py` import it)
  - `venv._identity_of(venv_path: Path) -> str | None` (schema≥3 stamp → `build_id(payload)`; older stamp → `sha256(text)` hex, the legacy identity format, unchanged)
  - `venv.build_identity() -> str | None` (= `_identity_of(spawn_venv())`)
  - `venv.ensure_venv(run=subprocess.run) -> Path` (provisions `venvs/<bid>/`, `--no-editable`; raises `RuntimeError` naming the launcher when no source tree is beside the package)
  - `venv.stamp_current() -> bool` (current build provisioned? requires source; used by CLI doctor/setup)

- [ ] **Step 1: Write the failing identity tests**

Append to `test/unit/test_venv.py` (keep the file's existing imports; add `from pathlib import Path` if missing):

```python
def _write_src_tree(root: Path, extra: str = "") -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (root / "uv.lock").write_text("version = 1\n")
    pkg = root / "src" / "ptc"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("VERSION = 1\n" + extra)
    (pkg / "mod.py").write_text("def f():\n    return 1\n")


def test_build_id_is_path_independent(monkeypatch, tmp_path):
    """The same source at two different absolute paths is ONE build — the old scheme's
    embedded pkg path made every plugin-cache relocation a needless rebuild."""
    a, b = tmp_path / "cache-v1" / "pkg", tmp_path / "cache-v2" / "pkg"
    for root in (a, b):
        root.mkdir(parents=True)
        _write_src_tree(root)
    monkeypatch.setattr(venv, "PKG_ROOT", a)
    ida = venv.build_id()
    monkeypatch.setattr(venv, "PKG_ROOT", b)
    idb = venv.build_id()
    assert ida == idb and len(ida) == 12


def test_build_id_changes_with_source_content(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    _write_src_tree(root)
    monkeypatch.setattr(venv, "PKG_ROOT", root)
    before = venv.build_id()
    _write_src_tree(root, extra="CHANGED = True\n")
    assert venv.build_id() != before


def test_stamp_payload_has_no_path_and_schema_3(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    _write_src_tree(root)
    monkeypatch.setattr(venv, "PKG_ROOT", root)
    p = venv.stamp_payload()
    assert p["schema"] == 3
    assert set(p) == {"schema", "pyproject_sha", "lock_sha", "src_sha"}


def test_identity_of_reads_schema3_and_legacy(monkeypatch, tmp_path):
    v3 = tmp_path / "v3"
    v3.mkdir()
    payload = {"schema": 3, "pyproject_sha": "a", "lock_sha": "b", "src_sha": "c"}
    (v3 / ".ptc-version").write_text(json.dumps(payload))
    assert venv._identity_of(v3) == venv.build_id(payload)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / ".ptc-version").write_text('{"schema": 2, "pkg": "/old"}')
    import hashlib
    assert venv._identity_of(legacy) == hashlib.sha256(
        b'{"schema": 2, "pkg": "/old"}').hexdigest()
    assert venv._identity_of(tmp_path / "nothing-here") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest test/unit/test_venv.py -k "build_id or stamp_payload_has or identity_of" -v`
Expected: FAIL / ERROR with `AttributeError: module 'ptc.venv' has no attribute 'build_id'` (and friends).

- [ ] **Step 3: Rewrite `src/ptc/venv.py`'s identity half**

Add `venvs_root()` and the protocol constant to `src/ptc/paths.py` right after `venv_dir()` (line ~50):

```python
def venvs_root() -> Path:
    return ptc_home() / "venvs"
```

and near the top of `src/ptc/paths.py`, beside the other module constants:

```python
#: The disk/wire contract an adapter shares with a kernel (cells layout, record schema,
#: connection handshake). Recorded in meta.json at spawn; attach requires equality, and an
#: absent field reads as 0 — so pre-v0.3 kernels recycle once at rollout, with notice.
PTC_PROTOCOL = 1
```

Replace `src/ptc/venv.py`'s header/identity functions (keep `_uv()` and the module docstring's
spirit; the provisioning half changes in Step 6):

```python
"""Provision immutable per-build venvs under ~/.ptc/venvs/<build_id>/.

Payload computation lives ONLY in the provisioners — this module's ensure_venv (run from a
checkout or the plugin dir) and bin/ptc-launch (stdlib twin; MUST stay semantically
identical). Runtimes never recompute a payload: a provisioned venv carries its stamp
inside, and a process identifies its own build via sys.prefix (`runtime_venv`).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import ptc_home, venv_dir, venvs_root

PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # .../ptc-tool


def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def src_tree_sha(root: Path | None = None) -> str:
    """Content hash of the packaged source tree — sorted relative posix paths + content
    hashes, so the same source hashes the same from any absolute location."""
    base = (root or PKG_ROOT) / "src" / "ptc"
    parts = [f"{p.relative_to(base).as_posix()}:"
             f"{hashlib.sha256(p.read_bytes()).hexdigest()}"
             for p in sorted(base.rglob("*.py"))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def stamp_payload() -> dict:
    """What a build is MADE FROM — and nothing about where it sits. The old schema's
    embedded `pkg` path made every plugin-cache relocation read as a new build, which is
    why unchanged-dependency upgrades kept killing kernels."""
    def sha(name: str) -> str:
        return hashlib.sha256((PKG_ROOT / name).read_bytes()).hexdigest()
    return {"schema": 3, "pyproject_sha": sha("pyproject.toml"),
            "lock_sha": sha("uv.lock"), "src_sha": src_tree_sha()}


def build_id(payload: dict | None = None) -> str:
    p = stamp_payload() if payload is None else payload
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]


def build_venv_dir(bid: str) -> Path:
    return venvs_root() / bid


def runtime_venv() -> Path | None:
    """This process's own provisioned venv — None for a dev/checkout run."""
    p = Path(sys.prefix)
    return p if (p / ".ptc-version").exists() else None


def spawn_venv() -> Path:
    """The venv kernels are spawned from: our own build when provisioned, else the legacy
    shared directory (dev runs and the test fixtures that symlink it)."""
    return runtime_venv() or venv_dir()


def venv_python() -> Path:
    return spawn_venv() / "bin" / "python"


def _identity_of(venv_path: Path) -> str | None:
    """The identity of the build standing at `venv_path`, read from its own stamp.
    Schema ≥ 3 stamps identify as build_id(payload); older stamps keep the legacy
    identity (sha256 of the raw text) so pre-v0.3 meta.json comparisons still hold."""
    try:
        text = (venv_path / ".ptc-version").read_text()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("schema"), int) \
            and payload["schema"] >= 3:
        return build_id(payload)
    return hashlib.sha256(text.encode()).hexdigest()


def build_identity() -> str | None:
    """Which build THIS process (and the kernels it spawns) runs from."""
    return _identity_of(spawn_venv())
```

- [ ] **Step 4: Run the identity tests to verify they pass**

Run: `uv run pytest test/unit/test_venv.py -k "build_id or stamp_payload_has or identity_of" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Rewrite the provisioning tests for the versioned scheme**

In `test/unit/test_venv.py`, the five existing provisioning tests change meaning. Replace
`_fake_run_factory` and the five tests (`test_provisions_when_missing`,
`test_skips_when_stamp_current`, `test_reprovisions_on_stamp_mismatch` — which becomes a
side-by-side test — plus keep the two lock tests with updated expectations) with:

```python
def _fake_run_factory(calls):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "venv":
            target = Path(cmd[2])
            p = target / "bin"
            if p.exists() and "--clear" not in cmd:
                raise subprocess.CalledProcessError(2, cmd)
            p.mkdir(parents=True, exist_ok=True)
            (p / "python").write_text("#!fake\n")
        class R: returncode = 0
        return R()
    return fake_run


def _project(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    root = tmp_path / "pkg"
    root.mkdir(parents=True, exist_ok=True)
    _write_src_tree(root)
    monkeypatch.setattr(venv, "PKG_ROOT", root)
    return root


def test_provisions_into_versioned_dir(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path)
    calls: list = []
    py = venv.ensure_venv(run=_fake_run_factory(calls))
    bid = venv.build_id()
    assert py == venv.build_venv_dir(bid) / "bin" / "python"
    sync = next(c for c in calls if c[1] == "sync")
    assert "--locked" in sync and "--no-editable" in sync and "kernel" in sync
    stamp = json.loads((venv.build_venv_dir(bid) / ".ptc-version").read_text())
    assert stamp == venv.stamp_payload()


def test_skips_when_build_already_provisioned(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path)
    calls: list = []
    venv.ensure_venv(run=_fake_run_factory(calls))
    calls.clear()
    venv.ensure_venv(run=_fake_run_factory(calls))
    assert calls == []


def test_new_build_lands_beside_old_one(monkeypatch, tmp_path):
    """The survivability core: a source change provisions a SECOND directory and leaves
    the first untouched — nothing is ever rebuilt in place."""
    root = _project(monkeypatch, tmp_path)
    calls: list = []
    venv.ensure_venv(run=_fake_run_factory(calls))
    old = venv.build_venv_dir(venv.build_id())
    _write_src_tree(root, extra="CHANGED = True\n")
    venv.ensure_venv(run=_fake_run_factory(calls))
    new = venv.build_venv_dir(venv.build_id())
    assert new != old
    assert (old / "bin" / "python").exists(), "old build must remain standing"
    assert (new / "bin" / "python").exists()


def test_ensure_venv_without_source_raises_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(venv, "PKG_ROOT", tmp_path / "not-there")
    with pytest.raises(RuntimeError, match="launcher"):
        venv.ensure_venv(run=_must_not_provision)
```

Update the two surviving lock tests in place: in
`test_waits_for_contended_lock_then_returns_without_provisioning`, the fake `sleep` must
create the python and stamp under the VERSIONED dir; call `_project(monkeypatch, tmp_path)`
first and replace the three `venv_dir()`-based lines with:

```python
        vd = venv.build_venv_dir(venv.build_id())
        p = vd / "bin"
        p.mkdir(parents=True, exist_ok=True)
        (p / "python").write_text("#!fake\n")
        (vd / ".ptc-version").write_text(json.dumps(venv.stamp_payload()))
```

and its final assertion becomes `assert py == venv.build_venv_dir(venv.build_id()) / "bin" / "python"`.
In `test_raises_when_lock_contention_exceeds_budget`, add `_project(monkeypatch, tmp_path)`
as the first line (the lock dir moves to `tmp_path / "home" / "provision.lock"` — update
both tests' `lock = …` lines accordingly). Delete `test_a_lock_only_change_reprovisions`
(subsumed: any payload change now produces a new directory — `test_new_build_lands_beside_old_one`
covers the mechanism) and delete the old `from ptc.paths import venv_dir` import if now unused.

- [ ] **Step 6: Run to verify the new provisioning tests fail, then rewrite `ensure_venv`**

Run: `uv run pytest test/unit/test_venv.py -v` — expected: the Step-5 tests FAIL against the
old `ensure_venv`. Then replace `ensure_venv`/`stamp_current` in `src/ptc/venv.py`:

```python
def stamp_current() -> bool:
    """Is the CURRENT SOURCE's build already provisioned? Provisioner-side only."""
    try:
        payload = stamp_payload()
    except OSError:
        return False
    vd = build_venv_dir(build_id(payload))
    if not (vd / "bin" / "python").exists():
        return False
    try:
        return json.loads((vd / ".ptc-version").read_text()) == payload
    except (json.JSONDecodeError, OSError):
        return False


def ensure_venv(run=subprocess.run) -> Path:
    try:
        payload = stamp_payload()
    except OSError as e:
        raise RuntimeError(
            "cannot provision: no source tree beside this install (missing "
            f"{e.filename}) — provisioning happens in bin/ptc-launch or a checkout, "
            "never from a --no-editable runtime") from e
    bid = build_id(payload)
    vd = build_venv_dir(bid)
    if (vd / "bin" / "python").exists() and \
            _stamp_matches(vd, payload):
        return vd / "bin" / "python"
    lock = ptc_home() / "provision.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()  # mkdir-based lock, matches ptc-launch
    except FileExistsError:
        import time
        for _ in range(1200):
            if not lock.exists():
                break
            time.sleep(0.5)
        if (vd / "bin" / "python").exists() and _stamp_matches(vd, payload):
            return vd / "bin" / "python"
        raise RuntimeError("venv provisioning lock held and build still absent; "
                           f"remove {lock} if no other ptc process is running")
    try:
        uv = _uv()
        # --clear is harmless on a fresh directory and load-bearing on a retry over a
        # half-provisioned one (uv refuses to create over an existing venv).
        run([uv, "venv", str(vd), "--python", "3.12", "--seed", "--clear"],
            check=True)
        # `--locked` installs the checked-in uv.lock; `--no-editable` copies the project
        # into the venv so the build outlives the source directory it came from —
        # the self-containment half of survivability.
        run([uv, "sync", "--locked", "--inexact", "--no-dev", "--no-editable",
             "--extra", "kernel", "--project", str(PKG_ROOT)],
            check=True, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(vd)})
        (vd / ".ptc-version").write_text(json.dumps(payload))
    finally:
        lock.rmdir()
    return vd / "bin" / "python"


def _stamp_matches(vd: Path, payload: dict) -> bool:
    try:
        return json.loads((vd / ".ptc-version").read_text()) == payload
    except (json.JSONDecodeError, OSError):
        return False
```

- [ ] **Step 7: Guard CLI doctor against a sourceless runtime**

In `src/ptc/cli.py` (doctor branch, lines ~108-125), `stamp_current()` no longer raises on
missing source (it returns False via the `except OSError`) — but a False that MEANS
"cannot know" must not read as "stale". Replace the two report lines:

```python
        ready = stamp_current()
        report = {"venv": str(venv_python()), "venv_ready": ready,
```

with:

```python
        try:
            venv.stamp_payload()
            ready = stamp_current()
            would = ("nothing (build is provisioned)" if ready
                     else "provision this build (run: ptc setup)")
        except OSError:
            ready = None
            would = ("nothing here — no source tree beside this install; "
                     "the MCP launcher provisions automatically")
        report = {"venv": str(venv_python()), "venv_ready": ready,
```

adjust the following `"setup_would"` line to use `would`, and add `from ptc import venv`
beside the existing `from .venv import …` import (or extend that import) so
`venv.stamp_payload` resolves.

- [ ] **Step 8: Run the full unit-venv + CLI files**

Run: `uv run pytest test/unit/test_venv.py test/unit/test_cli_commands.py -v`
Expected: PASS. If a CLI doctor test pins the old `setup_would` strings, update its
expectation to the new strings above (the test is asserting report shape, not provisioning).

- [ ] **Step 9: Commit**

```bash
git add src/ptc/paths.py src/ptc/venv.py src/ptc/cli.py test/unit/test_venv.py test/unit/test_cli_commands.py
git commit -m "f5(ptc): path-independent build identity + versioned --no-editable provisioning (v0.3 i1)"
```

---

### Task 2: The launcher twin + real-uv integration proof

**Files:**
- Modify: `bin/ptc-launch`
- Test: `test/integration/test_provision_upgrade.py` (rewrite)

**Interfaces:**
- Consumes: from Task 1 the payload/identity semantics (schema 3, sorted-json 12-hex build id, `--no-editable` sync) — the launcher re-implements them stdlib-only and MUST stay semantically identical.
- Produces: `bin/ptc-launch` provisions `~/.ptc/venvs/<build_id>/` and execs `<venv>/bin/python -m ptc.mcp` from it. Its module-level functions (loaded by the integration test): `payload() -> dict`, `build_id() -> str`, `venv_dir() -> Path` (the versioned target), `current() -> bool`, `provision() -> None`.

- [ ] **Step 1: Rewrite the integration test first**

Replace `test/integration/test_provision_upgrade.py` with:

```python
"""Real `uv` provisioning of versioned builds — both provisioner twins.

Three proofs a fake `run` cannot give: (a) a build provisions into venvs/<build_id>/ and
imports work; (b) a source change provisions a SECOND build and leaves the first standing
(the survivability core); (c) --no-editable really copies the project — the venv keeps
working after its source tree is deleted (the self-containment acceptance).
"""
import importlib.util
import json
import shutil
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent


def _copy_minimal_source(dst: Path) -> Path:
    """A real, provisionable copy of this package: manifest, lock, source, launcher."""
    dst.mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(PKG / name, dst / name)
    shutil.copytree(PKG / "src", dst / "src")
    shutil.copytree(PKG / "bin", dst / "bin")
    return dst


def _launcher_provision(src_root: Path):
    loader = SourceFileLoader("ptc_launch_v3", str(src_root / "bin" / "ptc-launch"))
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("ptc_launch_v3", loader))
    loader.exec_module(mod)          # reads PTC_HOME at import
    mod.provision()
    assert mod.current()
    return mod.venv_dir()


def _library_provision(src_root: Path):
    import ptc.venv as pv
    old = pv.PKG_ROOT
    pv.PKG_ROOT = src_root
    try:
        py = pv.ensure_venv()
        assert pv.stamp_current()
        return py.parent.parent
    finally:
        pv.PKG_ROOT = old


@pytest.mark.parametrize("provision", [_launcher_provision, _library_provision],
                         ids=["ptc-launch", "ptc.venv"])
def test_versioned_provision_survives_source_deletion(tmp_path, monkeypatch, provision):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    src = _copy_minimal_source(tmp_path / "cache" / "pkg")
    vd = provision(src)
    assert vd.parent == tmp_path / "home" / "venvs"
    py = vd / "bin" / "python"
    subprocess.run([str(py), "-c", "import ptc, ipykernel"], check=True)
    # (c) self-containment: the plugin cache is pruned; the build must not care
    shutil.rmtree(src)
    subprocess.run([str(py), "-c", "import ptc, ptc.runtime.bootstrap"], check=True)


def test_source_change_provisions_beside_not_over(tmp_path, monkeypatch):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    src = _copy_minimal_source(tmp_path / "cache" / "pkg")
    old_vd = _launcher_provision(src)
    marker = old_vd / "bin" / "python"
    stamp_before = (old_vd / ".ptc-version").read_text()
    (src / "src" / "ptc" / "__init__.py").write_text("VERSION = 'changed'\n")
    new_vd = _launcher_provision(src)
    assert new_vd != old_vd
    assert marker.exists(), "the old build must remain standing"
    assert (old_vd / ".ptc-version").read_text() == stamp_before
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest test/integration/test_provision_upgrade.py -x -v`
Expected: FAIL (`mod.venv_dir` / versioned path assertions against the old launcher; the
library case fails on `--no-editable`/versioned-dir expectations).

- [ ] **Step 3: Rewrite `bin/ptc-launch`**

Replace the identity/provisioning half of `bin/ptc-launch` (keep the shebang, the imports,
the `_RAW_HOME`/`HOME` block, and the PKG comment block explaining plugin-root containment):

```python
HOME = (Path(_RAW_HOME).expanduser().resolve() if _RAW_HOME
        else Path.home() / ".ptc")
PKG = Path(__file__).resolve().parent.parent             # bin/ptc-launch -> ptc/


def payload() -> dict:
    """Semantically identical to ptc.venv.stamp_payload() — keep the twins in step."""
    def sha(name):
        return hashlib.sha256((PKG / name).read_bytes()).hexdigest()
    base = PKG / "src" / "ptc"
    parts = ["{}:{}".format(p.relative_to(base).as_posix(),
                            hashlib.sha256(p.read_bytes()).hexdigest())
             for p in sorted(base.rglob("*.py"))]
    src_sha = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return {"schema": 3, "pyproject_sha": sha("pyproject.toml"),
            "lock_sha": sha("uv.lock"), "src_sha": src_sha}


def build_id() -> str:
    return hashlib.sha256(
        json.dumps(payload(), sort_keys=True).encode()).hexdigest()[:12]


def venv_dir() -> Path:
    return HOME / "venvs" / build_id()


def current() -> bool:
    vd = venv_dir()
    try:
        return (vd / "bin" / "python").exists() and \
            json.loads((vd / ".ptc-version").read_text()) == payload()
    except (OSError, json.JSONDecodeError):
        return False


def provision() -> None:
    vd = venv_dir()
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    # --clear is harmless on a fresh directory and load-bearing on a retry over a
    # half-provisioned one (uv refuses to create over an existing venv).
    subprocess.run([uv, "venv", str(vd), "--python", "3.12", "--seed", "--clear"],
                   check=True)
    # The checked-in uv.lock, copied in (--no-editable): the build outlives the plugin
    # cache directory it was provisioned from. See ptc.venv.ensure_venv, whose two
    # commands this must stay identical to.
    subprocess.run([uv, "sync", "--locked", "--inexact", "--no-dev", "--no-editable",
                    "--extra", "kernel", "--project", str(PKG)],
                   check=True, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(vd)})
    (vd / ".ptc-version").write_text(json.dumps(payload()))


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
                print("ptc-launch: build absent and provisioning lock held; "
                      f"remove {lock} if nothing is provisioning", file=sys.stderr)
                sys.exit(1)
    os.environ["PTC_LAUNCHER"] = os.path.abspath(__file__)
    py = venv_dir() / "bin" / "python"
    os.execv(str(py), [str(py), "-m", "ptc.mcp"])
```

Also update the module docstring's first line to
`"""Provision ~/.ptc/venvs/<build_id>/ if missing, then exec the ptc MCP adapter from it.`
and keep its second line about the stamp-payload twin contract. Delete the old module-level
`VENV`/`PY`/`STAMP` constants (they are functions now — the build id depends on the source,
which the old constants froze at import).

- [ ] **Step 4: Run the integration file and the packaging pins**

Run: `uv run pytest test/integration/test_provision_upgrade.py test/unit/test_plugin_packaging.py -v`
Expected: PASS. If `test_plugin_packaging.py` pins launcher internals (it asserts the
package sits inside the plugin root and the launcher's hash inputs exist), update only
what names the removed constants — the containment assertions must keep passing untouched.

- [ ] **Step 5: Commit**

```bash
git add bin/ptc-launch test/integration/test_provision_upgrade.py test/unit/test_plugin_packaging.py
git commit -m "f5(ptc): launcher provisions versioned self-contained builds (v0.3 i1)"
```

---

### Task 3: Attach semantics — record the venv, gate on protocol, notice on build drift

**Files:**
- Modify: `src/ptc/kernel.py` (KernelInfo.build_note; `_upgraded_under` → `_venv_gone` + `_protocol_mismatch` + `_build_note`; meta records `venv` + `protocol`)
- Modify: `src/ptc/mcp.py:158-167` (render build_note)
- Modify: `src/ptc/cli.py:189-212` (render build_note, JSON field)
- Test: `test/unit/test_kernel_attach_gates.py` (new), `test/integration/test_kernel_lifecycle.py` (extend)

**Interfaces:**
- Consumes: from Task 1: `venv.build_identity()`, `venv.spawn_venv()`, `venv.venv_python()`, `paths.PTC_PROTOCOL`.
- Produces: `KernelInfo` gains `build_note: str | None = None` (last field, default — every existing construction stays valid). meta.json gains `venv: str` and `protocol: int` at spawn. Later initiatives (2/3) rely on `protocol` and the attach-gate structure.

- [ ] **Step 1: Write the failing unit tests**

Create `test/unit/test_kernel_attach_gates.py`:

```python
"""Attach gates for the immutable-venv scheme (v0.3 i1): a live kernel is recycled ONLY
when its venv is gone or its protocol mismatches; a build difference is a notice."""
import json
from pathlib import Path

import ptc.kernel as kernel
from ptc.discovery import read_meta, write_meta
from ptc.paths import PTC_PROTOCOL, kernel_dir


def _meta(monkeypatch, tmp_path, **fields):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    write_meta("k", **fields)


def test_venv_gone_fires_only_when_recorded_venv_missing(monkeypatch, tmp_path):
    _meta(monkeypatch, tmp_path, venv=str(tmp_path / "venvs" / "dead"))
    assert "no longer exists" in kernel._venv_gone("k")
    live = tmp_path / "venvs" / "alive"
    live.mkdir(parents=True)
    _meta(monkeypatch, tmp_path, venv=str(live))
    assert kernel._venv_gone("k") is None
    _meta(monkeypatch, tmp_path, venv=None)          # no record: dev spawn, no claim
    assert kernel._venv_gone("k") is None


def test_protocol_absent_reads_zero_and_mismatches(monkeypatch, tmp_path):
    _meta(monkeypatch, tmp_path, cwd="/x")           # no protocol field
    note = kernel._protocol_mismatch("k")
    assert "protocol 0" in note and f"current {PTC_PROTOCOL}" in note
    _meta(monkeypatch, tmp_path, protocol=PTC_PROTOCOL)
    assert kernel._protocol_mismatch("k") is None


def test_build_note_names_both_builds(monkeypatch, tmp_path):
    _meta(monkeypatch, tmp_path, build="aaaaaaaaaaaa")
    monkeypatch.setattr(kernel, "build_identity", lambda: "bbbbbbbbbbbb")
    note = kernel._build_note("k")
    assert "aaaaaaaaaaaa" in note and "bbbbbbbbbbbb" in note and "restart()" in note
    monkeypatch.setattr(kernel, "build_identity", lambda: "aaaaaaaaaaaa")
    assert kernel._build_note("k") is None
    monkeypatch.setattr(kernel, "build_identity", lambda: None)
    assert kernel._build_note("k") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_kernel_attach_gates.py -v`
Expected: FAIL with `AttributeError: module 'ptc.kernel' has no attribute '_venv_gone'`.

- [ ] **Step 3: Implement the gates in `src/ptc/kernel.py`**

Imports (top of file): extend the venv import to
`from .venv import build_identity, spawn_venv, venv_python` and the paths import gains
`PTC_PROTOCOL`. Add `build_note` to `KernelInfo`:

```python
@dataclass
class KernelInfo:
    key: str
    pid: int
    connection_file: Path
    spawned: bool
    expired_notice: str | None
    build_note: str | None = None
```

Replace `_upgraded_under` (kernel.py:63-89) with:

```python
def _venv_gone(key: str) -> str | None:
    """Has the venv this kernel launched from VANISHED under it (GC accident, manual rm)?

    Under immutable builds an upgrade no longer touches a standing venv, so the old
    rebuilt-under-us check retired with the rebuild itself; what remains fatal is the
    directory being gone. No recorded venv (a dev spawn, a pre-v0.3 kernel already gated
    by protocol below) claims nothing.
    """
    v = read_meta(key).get("venv")
    if not isinstance(v, str) or Path(v).exists():
        return None
    return (f"replaced because the venv it was running from ({v}) no longer exists — "
            "removed by GC or by hand")


def _protocol_mismatch(key: str) -> str | None:
    """Does this kernel speak our disk/wire contract? Absent reads 0: pre-v0.3 kernels
    recycle exactly once at rollout, with the same notice channel every upgrade used."""
    have = read_meta(key).get("protocol")
    have = have if isinstance(have, int) and not isinstance(have, bool) else 0
    if have == PTC_PROTOCOL:
        return None
    return (f"replaced after a ptc protocol change (kernel protocol {have}, "
            f"current {PTC_PROTOCOL})")


def _build_note(key: str) -> str | None:
    """One header line when an attach crosses builds — known and different on both sides.
    The kernel keeps running old code deliberately; the note is what keeps that a choice."""
    was = read_meta(key).get("build")
    now = build_identity()
    if not was or not now or was == now:
        return None
    return (f"kernel running build {was[:12]} (current {now[:12]}) — restart() to pick "
            "up the new runtime")
```

In `ensure_kernel`, replace the `upgraded` block (lines ~176-192):

```python
        # A live kernel is recycled only when attaching would be a LIE: its venv vanished
        # (it runs deleted code) or it predates our disk/wire contract. A mere build
        # difference attaches fine and says so (`build_note`).
        stale = (_venv_gone(key) or _protocol_mismatch(key)) if attachable else None
        if attachable and stale is None:
            if claude_session_id and not read_meta(key).get("claude_session_id"):
                write_meta(key, claude_session_id=claude_session_id)
            return KernelInfo(key, o.pid, kd / "connection.json", False, None,
                              _build_note(key))
        expired = read_expiry(key) or stale
```

In the spawn path, record where the kernel stands: the `write_meta` call (lines ~258-263)
gains two fields —

```python
            write_meta(key, kernel_key=key,
                       claude_session_id=claude_session_id or read_meta(key).get(
                           "claude_session_id"),
                       cwd=work, depth=cfg.depth, max_depth=cfg.max_depth,
                       idle_hours=cfg.idle_hours, max_concurrency=cfg.max_concurrency,
                       epoch=epoch, build=build,
                       venv=str(spawn_venv()), protocol=PTC_PROTOCOL)
```

(the `build = build_identity()` line before the Popen stays — its comment about reading
beside the interpreter it names still holds, now via the spawn venv's own stamp).

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest test/unit/test_kernel_attach_gates.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Render the note on both surfaces**

`src/ptc/mcp.py` exec handler — after the `expired_notice` block (lines 163-166), add:

```python
    if info.build_note:
        rendered.text = f"[{info.build_note}]\n" + rendered.text
```

`src/ptc/cli.py` exec/wait tail — in the `--json` branch, beside the `d["expired"] = …`
line, add `d["build_note"] = info.build_note if info is not None else None`; in the text
branch, after the expired print, add:

```python
        if info is not None and info.build_note:
            print(f"[{info.build_note}]")
```

- [ ] **Step 6: Extend the kernel lifecycle integration test with the survive-upgrade proof**

Append to `test/integration/test_kernel_lifecycle.py` (it already has real-kernel helpers —
follow its local spawn/exec conventions; the `ptc_home` fixture provides the venv):

```python
def test_kernel_survives_a_build_change_with_notice(ptc_home, monkeypatch):
    """Spec acceptance 1 (unit-level twin of the live check): a new CURRENT build must not
    recycle a standing kernel — same pid, namespace intact, one notice line."""
    import ptc.kernel as kernel
    from ptc.client import Completed, KernelClient
    from ptc.paths import Config
    cfg = Config.from_env()
    # the test venv carries no stamp, so identity is None there; pin a known build for the
    # SPAWN so meta.json records one — the note needs both sides known
    monkeypatch.setattr(kernel, "build_identity", lambda: "aaaaaaaaaaaa")
    info = kernel.ensure_kernel("survive", config=cfg)
    first_pid = info.pid
    out = KernelClient("survive").exec_cell("carried = 41 + 1", timeout_s=60, config=cfg)
    assert isinstance(out, Completed) and out.record.status == "ok"
    # a different CURRENT build: the adapter's own identity moves, the kernel's stays
    monkeypatch.setattr(kernel, "build_identity", lambda: "ffffffffffff")
    again = kernel.ensure_kernel("survive", config=cfg)
    assert again.spawned is False and again.pid == first_pid
    assert again.expired_notice is None
    assert "restart() to pick up the new runtime" in (again.build_note or "")
    out = KernelClient("survive").exec_cell("print(carried)", timeout_s=60, config=cfg)
    assert isinstance(out, Completed) and "42" in out.output
    kernel.kill_kernel("survive")


def test_protocol_zero_kernel_recycles_once_with_notice(ptc_home):
    import ptc.kernel as kernel
    from ptc.discovery import read_meta, write_meta
    from ptc.paths import Config
    cfg = Config.from_env()
    info = kernel.ensure_kernel("proto", config=cfg)
    old_pid = info.pid
    meta = read_meta("proto")
    del_protocol = {k: v for k, v in meta.items() if k != "protocol"}
    (ptc_home / "kernels" / "proto" / "meta.json").write_text(__import__("json").dumps(del_protocol))
    again = kernel.ensure_kernel("proto", config=cfg)
    assert again.spawned is True and again.pid != old_pid
    assert "protocol change" in (again.expired_notice or "")
    assert read_meta("proto").get("protocol") == 1
    kernel.kill_kernel("proto")
```

Note for the executor: the existing tests in that file show the exact exec/spawn idioms —
match them (e.g. if they pass `cwd=` or use a helper, do the same). The meta rewrite goes
through a direct file write because `write_meta` merges and cannot delete a key.

- [ ] **Step 7: Run integration + neighbors**

Run: `uv run pytest test/integration/test_kernel_lifecycle.py test/unit/test_restart_config.py test/unit/test_discovery.py -v`
Expected: PASS. (`restart_kernel` reads `_KERNEL_LIFETIME_FIELDS` from meta — the two new
fields are not in that set and must not disturb it; the discovery tests must not notice
`venv`/`protocol` riding along in meta.json.)

- [ ] **Step 8: Commit**

```bash
git add src/ptc/kernel.py src/ptc/mcp.py src/ptc/cli.py test/unit/test_kernel_attach_gates.py test/integration/test_kernel_lifecycle.py
git commit -m "f5(ptc): attach across builds — venv/protocol recorded, recycle only on venv-gone or protocol drift, notice on build drift (v0.3 i1)"
```

---

### Task 4: Build GC — unreferenced, aged, never mid-spawn

**Files:**
- Modify: `src/ptc/venv.py` (add `gc_builds`)
- Modify: `src/ptc/mcp.py` (call at adapter startup)
- Test: `test/unit/test_venv.py` (GC section)

**Interfaces:**
- Consumes: from Task 1: `venvs_root()`, `runtime_venv()`, `venv_dir()` (legacy candidate); from Task 3: meta.json's `venv` field as the reference set.
- Produces: `venv.gc_builds(*, grace_s: float = 72 * 3600.0) -> list[str]` (paths removed; `[]` on deferral/lock-held). `mcp.py` calls it once at startup, failure-proof.

- [ ] **Step 1: Write the failing GC tests**

Append to `test/unit/test_venv.py`:

```python
import os
import time as _time


def _aged_build(home: Path, name: str, age_s: float = 100 * 3600) -> Path:
    d = home / "venvs" / name
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "python").write_text("#!fake\n")
    old = _time.time() - age_s
    os.utime(d, (old, old))
    return d


def _kernel_row(home: Path, key: str, *, ready: bool, venv_path: str | None,
                alive: bool, monkeypatch) -> None:
    from ptc.discovery import write_meta
    from ptc.ownership import Owner, write_owner
    monkeypatch.setenv("PTC_HOME", str(home))
    kd = home / "kernels" / key
    kd.mkdir(parents=True, exist_ok=True)
    write_owner(key, Owner(99999 if alive else 1, "birth", _time.time(), "n0", "e0"))
    if ready:
        (kd / "ready").write_text("e0")
    if venv_path is not None:
        write_meta(key, venv=venv_path)


def _gc_env(monkeypatch, tmp_path, *, alive_pids=()):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PTC_HOME", str(home))
    import ptc.venv as pv
    monkeypatch.setattr(pv, "runtime_venv", lambda: None)
    from ptc import ownership
    monkeypatch.setattr(ownership, "settled_owner_state",
                        lambda o: o.pid in alive_pids)
    return home


def test_gc_removes_unreferenced_aged_builds(monkeypatch, tmp_path):
    home = _gc_env(monkeypatch, tmp_path)
    dead = _aged_build(home, "aaaaaaaaaaaa")
    removed = venv.gc_builds()
    assert str(dead) in removed and not dead.exists()


def test_gc_keeps_builds_referenced_by_a_live_kernel(monkeypatch, tmp_path):
    home = _gc_env(monkeypatch, tmp_path, alive_pids={99999})
    kept = _aged_build(home, "bbbbbbbbbbbb")
    _kernel_row(home, "k1", ready=True, venv_path=str(kept), alive=True,
                monkeypatch=monkeypatch)
    assert venv.gc_builds() == []
    assert kept.exists()


def test_gc_defers_entirely_while_a_spawn_is_provisional(monkeypatch, tmp_path):
    """owner.json without `ready` = a bootstrap in flight whose build is not recorded
    yet; deleting ANY build on that gap recreates the deleted-venv failure."""
    home = _gc_env(monkeypatch, tmp_path, alive_pids={99999})
    victim = _aged_build(home, "cccccccccccc")
    _kernel_row(home, "mid-spawn", ready=False, venv_path=None, alive=True,
                monkeypatch=monkeypatch)
    assert venv.gc_builds() == []
    assert victim.exists()


def test_gc_respects_grace_symlinks_and_the_lock(monkeypatch, tmp_path):
    home = _gc_env(monkeypatch, tmp_path)
    fresh = _aged_build(home, "dddddddddddd", age_s=60)          # inside grace
    real = _aged_build(home, "eeeeeeeeeeee")
    link = home / "venvs" / "linked"
    link.symlink_to(real)                                         # dev-cache symlink
    legacy = home / "venv"
    (legacy / "bin").mkdir(parents=True)
    old = _time.time() - 100 * 3600
    os.utime(legacy, (old, old))
    removed = venv.gc_builds()
    # precise claims (note: `real` is itself unreferenced+aged and legitimately removed;
    # the symlink-skip protects the LINK from being followed, so test with lexists):
    assert fresh.exists() and str(fresh) not in removed
    assert link.is_symlink() and str(link) not in removed
    assert str(legacy) in removed and not legacy.exists()         # legacy is a candidate
    # lock-held: nothing moves
    again = _aged_build(home, "ffffffffffff")
    (home / "provision.lock").mkdir()
    assert venv.gc_builds() == []
    assert again.exists()
```

(The `real` build referenced only via the symlink IS deletable as itself — the symlink-skip
protects the LINK from being followed; assert only the precise claims listed.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_venv.py -k gc -v`
Expected: FAIL with `AttributeError: module 'ptc.venv' has no attribute 'gc_builds'`.

- [ ] **Step 3: Implement `gc_builds` in `src/ptc/venv.py`**

```python
def gc_builds(*, grace_s: float = 72 * 3600.0) -> list[str]:
    """Delete build venvs nothing needs; never one someone might be climbing into.

    A candidate dies only when it is not the build THIS process runs from, no kernel with
    a live owner records it in meta.json `venv`, and it is older than the grace (which is
    also what protects a NEWER build another session just provisioned — the runtime cannot
    recompute a source payload to name "current"). Deferred entirely while any kernel key
    holds a provisional owner (owner.json without `ready`): that spawn's build is not
    recorded yet. Serialized against provisioning on the same provision.lock; a held lock
    means "not now", never "force it".
    """
    import time
    from .discovery import read_meta
    from .ownership import UnknownOwner, read_owner, settled_owner_state
    from .paths import kernels_root
    referenced: set[str] = set()
    root = kernels_root()
    if root.is_dir():
        for kd in sorted(root.iterdir()):
            if not kd.is_dir():
                continue
            o = read_owner(kd.name)
            if o is None:
                continue
            if not (kd / "ready").exists():
                return []          # provisional spawn mid-bootstrap: defer everything
            try:
                alive = settled_owner_state(o)
            except UnknownOwner:
                alive = True       # unreadable identity pins, never frees
            if alive:
                v = read_meta(kd.name).get("venv")
                if isinstance(v, str):
                    referenced.add(v)
    candidates: list[Path] = []
    vr = venvs_root()
    if vr.is_dir():
        candidates += sorted(vr.iterdir())
    legacy = venv_dir()
    if legacy.exists() or legacy.is_symlink():
        candidates.append(legacy)
    keep = runtime_venv()
    lock = ptc_home() / "provision.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except (FileExistsError, OSError):
        return []                  # provisioning (or another GC) is running: not now
    removed: list[str] = []
    try:
        now = time.time()
        for c in candidates:
            if c.is_symlink() or not c.is_dir():
                continue           # a dev setup symlinks the legacy path at a shared cache
            if keep is not None and c == keep:
                continue
            if str(c) in referenced:
                continue
            try:
                if now - c.stat().st_mtime < grace_s:
                    continue
                shutil.rmtree(c)
                removed.append(str(c))
            except OSError:
                continue
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass
    return removed
```

- [ ] **Step 4: Run the GC tests to verify they pass**

Run: `uv run pytest test/unit/test_venv.py -k gc -v`
Expected: PASS (4 tests). If `_kernel_row`'s `Owner(...)` construction mismatches the real
dataclass field order, read `src/ptc/ownership.py::Owner` and fix the TEST to construct it
correctly (fields as of today: `pid, proc_start_time, spawned_at, nonce, epoch`).

- [ ] **Step 5: Wire GC into adapter startup**

In `src/ptc/mcp.py`, find the server startup (the `main()` / `if __name__` tail that runs
the MCP server) and add, before the server starts serving:

```python
    try:
        from .venv import gc_builds
        gc_builds()
    except Exception:
        pass   # GC must never cost a session its adapter
```

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest test/ -q`
Expected: everything passes (some env-gated skips are normal). Fix regressions before
committing — the likeliest are venv-path expectations in tests not touched by this plan.

- [ ] **Step 7: Commit**

```bash
git add src/ptc/venv.py src/ptc/mcp.py test/unit/test_venv.py
git commit -m "f5(ptc): build GC — unreferenced+aged only, deferred mid-spawn, lock-serialized (v0.3 i1)"
```

---

### Task 5: Final verification — the spec's acceptance, as written

**Files:**
- No new code. Runs commands; records outcomes in the executor report.

The spec's acceptance for initiative 1 (quoted; the integration tests built in Tasks 2-4
are their test-suite twins — this task proves them against the real machinery once more
and runs the full suite):

> 1. A kernel holds a variable; modify a file's content under `src/ptc/` (the identity hashes
>    content, not mtimes — a new build_id) and re-provision; a fresh adapter attaches to the
>    SAME kernel — variable intact, header carries the build-difference notice.
> 2. After `ptc kill` of every kernel referencing the old build, the next provision (or
>    SessionStart) removes that build's directory once past grace (tested with grace 0).
> 3. Provision, then rename the package source directory: kernel spawn + bootstrap still
>    succeed from the venv's own copy, and the adapter still resolves its own build id for
>    the header notice (the `--no-editable` + self-identity proof).
> 4. A spawn whose bootstrap is in flight (owner.json present, `ready` absent) defers GC:
>    no build directory is deleted until the spawn settles (interleaving test, legacy
>    `~/.ptc/venv` included).

- [ ] **Step 1: Full suite**

Run: `uv run pytest test/ -q`
Expected: all pass (env-gated skips OK). Paste the tail (`N passed, M skipped`) into the report.

- [ ] **Step 2: Acceptance 1 + 3 against real uv (one script)**

Run from the repo root:

```bash
python3 - <<'EOF'
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
PKG = Path.cwd()
tmp = Path(tempfile.mkdtemp(prefix="ptc-accept-"))
home = tmp / "home"; os.environ["PTC_HOME"] = str(home)
src = tmp / "cache" / "pkg"
src.mkdir(parents=True)
for n in ("pyproject.toml", "uv.lock"): shutil.copy(PKG / n, src / n)
shutil.copytree(PKG / "src", src / "src"); shutil.copytree(PKG / "bin", src / "bin")
def provision():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    ld = SourceFileLoader(f"l{os.urandom(2).hex()}", str(src / "bin" / "ptc-launch"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
    ld.exec_module(m); 
    if not m.current(): m.provision()
    return m.venv_dir()
v1 = provision()
py1 = v1 / "bin" / "python"
# acceptance 1: kernel holds a variable across an upgrade
code = '''
import os
os.environ["PTC_HOME"] = %r
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.client import KernelClient, Completed
from ptc.paths import Config
cfg = Config.from_env()
ensure_kernel("accept1", config=cfg)
out = KernelClient("accept1").exec_cell("held = 'alive'", timeout_s=60, config=cfg)
assert out.record.status == "ok", out
''' % str(home)
subprocess.run([str(py1), "-c", code], check=True)
(src / "src" / "ptc" / "__init__.py").write_text("VERSION = 'accept-upgrade'\n")
v2 = provision()
assert v2 != v1 and v1.exists(), (v1, v2)
py2 = v2 / "bin" / "python"
code2 = '''
import os
os.environ["PTC_HOME"] = %r
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.client import KernelClient
from ptc.paths import Config
cfg = Config.from_env()
info = ensure_kernel("accept1", config=cfg)
assert info.spawned is False, "must attach, not recycle"
assert info.build_note and "restart()" in info.build_note, info.build_note
out = KernelClient("accept1").exec_cell("print(held)", timeout_s=60, config=cfg)
assert "alive" in out.output, out.output
kill_kernel("accept1")
print("ACCEPT-1 OK:", info.build_note)
''' % str(home)
subprocess.run([str(py2), "-c", code2], check=True)
# acceptance 3: source gone, new-build adapter still works and knows itself
shutil.rmtree(src)
code3 = '''
import os
os.environ["PTC_HOME"] = %r
from ptc.venv import build_identity
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config
bid = build_identity()
assert bid and len(bid) == 12, bid
info = ensure_kernel("accept3", config=Config.from_env())
kill_kernel("accept3")
print("ACCEPT-3 OK: self build", bid)
''' % str(home)
subprocess.run([str(py2), "-c", code3], check=True)
shutil.rmtree(tmp)
print("ACCEPTANCE 1+3 PASSED")
EOF
```

Expected output ends with `ACCEPT-1 OK: kernel running build … (current …) — restart() to
pick up the new runtime`, `ACCEPT-3 OK: self build …`, `ACCEPTANCE 1+3 PASSED`.

- [ ] **Step 3: Acceptance 2 + 4 (GC behavior, grace 0)**

Acceptance 2 and 4 are pinned by the Task 4 unit tests with exact semantics; re-run them
by name as the acceptance record:

Run: `uv run pytest "test/unit/test_venv.py::test_gc_removes_unreferenced_aged_builds" "test/unit/test_venv.py::test_gc_defers_entirely_while_a_spawn_is_provisional" "test/unit/test_venv.py::test_gc_keeps_builds_referenced_by_a_live_kernel" -v`
Expected: 3 passed.

- [ ] **Step 4: Report**

No commit (nothing changed). The executor report records: suite tail, the three acceptance
outputs verbatim, and any deviation as a concern.
