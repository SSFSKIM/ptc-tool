import json
import subprocess
import time
from pathlib import Path

import pytest
from ptc import venv


def _write_src_tree(root: Path, extra: str = "") -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (root / "uv.lock").write_text("version = 1\n")
    pkg = root / "src" / "ptc"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("VERSION = 1\n" + extra)
    (pkg / "mod.py").write_text("def f():\n    return 1\n")


def _fake_run_factory(calls):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        # simulate uv creating the python binary on `uv venv`
        if cmd[1] == "venv":
            target = Path(cmd[2])
            p = target / "bin"
            if p.exists() and "--clear" not in cmd:
                # uv 0.11: "A virtual environment already exists at: ..." (exit 2).
                # A retry over a half-provisioned directory hits this, so the fake must
                # refuse exactly as uv does.
                raise subprocess.CalledProcessError(2, cmd)
            p.mkdir(parents=True, exist_ok=True)
            (p / "python").write_text("#!fake\n")
        class R: returncode = 0
        return R()
    return fake_run


def _must_not_provision(cmd, **kw):
    raise AssertionError("must not provision")


def _project(monkeypatch, tmp_path):
    """An isolated PTC_HOME plus a source tree to compute a build identity from."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    root = tmp_path / "pkg"
    root.mkdir(parents=True, exist_ok=True)
    _write_src_tree(root)
    monkeypatch.setattr(venv, "PKG_ROOT", root)
    return root


# -- identity: what a build IS, independent of where it sits ----------------------------


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
    after_src = venv.build_id()
    (root / "uv.lock").write_text("version = 1\n# a dependency was pinned down\n")
    assert venv.build_id() != after_src, "a lock-only change is a new build"


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


# -- provisioning: one immutable directory per build ------------------------------------


def test_provisions_into_versioned_dir(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path)
    calls: list = []
    py = venv.ensure_venv(run=_fake_run_factory(calls))
    bid = venv.build_id()
    assert py == venv.build_venv_dir(bid) / "bin" / "python"
    sync = next(c for c in calls if c[1] == "sync")
    # the checked-in lock, not a fresh resolution; and copied in rather than linked back
    # at the source tree, so the build outlives the directory it was made from
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


def test_spawn_venv_prefers_a_standing_provisioned_build(monkeypatch, tmp_path):
    """A checkout CLI run is not provisioned itself, but once its `ptc setup` has built the
    current source's venv, kernels must spawn from THAT — nothing fills the legacy shared
    directory any more, so without this rung the CLI-standalone flow has no venv at all."""
    from ptc.paths import venv_dir as legacy_dir
    _project(monkeypatch, tmp_path)
    monkeypatch.setattr(venv, "runtime_venv", lambda: None)
    assert venv.spawn_venv() == legacy_dir()          # nothing provisioned yet
    venv.ensure_venv(run=_fake_run_factory([]))
    assert venv.spawn_venv() == venv.build_venv_dir(venv.build_id())


def test_ensure_venv_without_source_raises_runtime_error(monkeypatch, tmp_path):
    """A --no-editable runtime has no pyproject/lock beside it and so cannot compute a
    payload. Saying so beats provisioning something under a guessed identity."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(venv, "PKG_ROOT", tmp_path / "not-there")
    with pytest.raises(RuntimeError, match="launcher"):
        venv.ensure_venv(run=_must_not_provision)


def test_waits_for_contended_lock_then_returns_without_provisioning(monkeypatch, tmp_path):
    """The lock dir already exists (another process is provisioning it). On
    our first poll it finishes: lock gone, venv python + a current stamp in
    place. We must pick that up and return without ever calling run."""
    _project(monkeypatch, tmp_path)
    lock = tmp_path / "home" / "provision.lock"
    lock.mkdir(parents=True)

    sleep_calls: list = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        lock.rmdir()
        vd = venv.build_venv_dir(venv.build_id())
        p = vd / "bin"
        p.mkdir(parents=True, exist_ok=True)
        (p / "python").write_text("#!fake\n")
        (vd / ".ptc-version").write_text(json.dumps(venv.stamp_payload()))

    monkeypatch.setattr(time, "sleep", fake_sleep)

    py = venv.ensure_venv(run=_must_not_provision)
    assert py == venv.build_venv_dir(venv.build_id()) / "bin" / "python"
    assert sleep_calls == [0.5]  # broke out of the poll loop on the first check after sleeping


def test_raises_when_lock_contention_exceeds_budget(monkeypatch, tmp_path):
    """ensure_venv has no time.time()/time.monotonic() call: the 10-minute budget
    is a fixed 1200-iteration poll loop (1200 * 0.5s sleep), not a wall-clock
    check. A lock that never clears must exhaust every iteration and raise."""
    _project(monkeypatch, tmp_path)
    lock = tmp_path / "home" / "provision.lock"
    lock.mkdir(parents=True)

    sleep_calls: list = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(RuntimeError, match="lock"):
        venv.ensure_venv(run=_must_not_provision)
    assert len(sleep_calls) == 1200


def test_waiter_provisions_its_own_build_after_holder_releases(monkeypatch, tmp_path):
    """The lock holder was provisioning a DIFFERENT build; when it releases, the waiter
    must take the lock and provision its own rather than raise (rollout overlap)."""
    _project(monkeypatch, tmp_path)
    lock = tmp_path / "home" / "provision.lock"
    lock.mkdir(parents=True)
    calls: list = []
    monkeypatch.setattr(time, "sleep", lambda s: lock.rmdir())
    py = venv.ensure_venv(run=_fake_run_factory(calls))
    assert any(c[1] == "venv" for c in calls), "waiter must provision its own build"
    assert py == venv.build_venv_dir(venv.build_id()) / "bin" / "python"


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
