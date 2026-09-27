"""gc_kernel_dirs: key directories go once their kernel is gone for good, and never before.

Owners are real processes: a running `sleep` for a live one (its own birth identity), a
reaped child for a dead one — so the liveness path under test is the one production runs.
Never this test process: conftest's reaper kill_kernel()s every key a test leaves behind.
"""
import json
import os
import subprocess
import time

import pytest

from ptc import kernel
from ptc.lock import key_lock
from ptc.ownership import Owner, UnknownOwner, proc_start_time, write_owner

DAY = 24 * 3600.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "ptc-home"
    (h / "kernels").mkdir(parents=True)
    monkeypatch.setenv("PTC_HOME", str(h))
    return h


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


@pytest.fixture
def sleeper():
    p = subprocess.Popen(["sleep", "300"], start_new_session=True)
    yield p
    p.kill()
    p.wait()


def _key(home, key: str, *, owner=None, age_s: float = 30 * DAY,
         agents: list | None = None):
    """A key directory as expiry/kill leave it (marker, meta, a cell log), aged by
    `age_s`. owner: None (absent), a live Popen, or "dead" (a reaped child)."""
    kd = home / "kernels" / key
    (kd / "cells").mkdir(parents=True)
    (kd / "cells" / "1.log").write_text("x")
    (kd / "meta.json").write_text("{}")
    (kd / "expired.marker").write_text("expired after 24.00 h idle")
    if isinstance(owner, subprocess.Popen):
        write_owner(key, Owner(owner.pid, proc_start_time(owner.pid), 0.0, "n", "1"))
    elif owner == "dead":
        write_owner(key, Owner(_dead_pid(), "btime=1.000000", 0.0, "n", "1"))
    if agents is not None:
        (kd / "agents.json").write_text(json.dumps(agents))
    old = time.time() - age_s
    for dirpath, _dirs, files in os.walk(kd):
        for name in files:
            os.utime(os.path.join(dirpath, name), (old, old))
    return kd


def test_removes_an_aged_key_whose_owner_is_absent_or_dead(home):
    gone = _key(home, "expired")
    dead = _key(home, "crashed", owner="dead")
    assert sorted(kernel.gc_kernel_dirs()) == ["crashed", "expired"]
    assert not gone.exists() and not dead.exists()


def test_never_removes_a_live_kernels_directory(home, sleeper):
    live = _key(home, "live", owner=sleeper)
    assert kernel.gc_kernel_dirs() == []
    assert live.exists()


def test_an_unreadable_owner_keeps_the_directory(home, monkeypatch):
    kd = _key(home, "unknown", owner="dead")

    def unknown(o):
        raise UnknownOwner("unreadable")
    monkeypatch.setattr(kernel, "settled_owner_state", unknown)
    assert kernel.gc_kernel_dirs() == []
    assert kd.exists()


def test_an_owner_file_that_cannot_be_read_or_decoded_is_not_an_absent_owner(home):
    """read_owner answers None for a bad file as well as a missing one; only the missing
    one means nobody owns the key."""
    garbled = _key(home, "garbled")
    (garbled / "owner.json").write_text("{not json")
    locked = _key(home, "unreadable")
    (locked / "owner.json").write_text("{}")
    os.chmod(locked / "owner.json", 0)
    dangling = _key(home, "dangling")
    (dangling / "owner.json").symlink_to(home / "nowhere")
    try:
        assert kernel.gc_kernel_dirs() == []
        assert garbled.exists() and locked.exists() and dangling.exists()
    finally:
        os.chmod(locked / "owner.json", 0o600)


def test_grace_is_a_week_for_main_keys_and_a_day_for_subagent_keys(home):
    main_recent = _key(home, "s1", age_s=3 * DAY)
    sub_recent = _key(home, "s1--sub-agent_a", age_s=3 * 3600)
    sub_old = _key(home, "s1--sub-agent_b", age_s=3 * DAY)
    assert kernel.gc_kernel_dirs() == ["s1--sub-agent_b"]
    assert main_recent.exists() and sub_recent.exists() and not sub_old.exists()


def test_any_recent_file_restarts_the_grace(home):
    kd = _key(home, "touched")
    (kd / "cells" / "1.log").write_text("fresh")        # mtime now
    assert kernel.gc_kernel_dirs() == []
    assert kd.exists()


def test_a_main_keys_agent_registry_keeps_it_but_a_subagent_keys_does_not(home):
    """agents.json is how a --resume gets back to child sessions after the kernel died;
    a finished subagent's key has nobody left to come back through it."""
    main = _key(home, "withagents", agents=[{"name": "a", "session_id": "s"}])
    empty = _key(home, "emptyreg", agents=[])
    sub = _key(home, "withagents--sub-agent_c", agents=[{"name": "a"}])
    assert sorted(kernel.gc_kernel_dirs()) == ["emptyreg", "withagents--sub-agent_c"]
    assert main.exists() and not empty.exists() and not sub.exists()


def test_a_held_key_lock_means_not_now(home):
    kd = _key(home, "busy")
    with key_lock("busy"):
        assert kernel.gc_kernel_dirs() == []
    assert kd.exists()
    assert kernel.gc_kernel_dirs() == ["busy"]


def test_symlinks_and_stray_files_are_left_alone(home, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "f").write_text("x")
    old = time.time() - 30 * DAY
    os.utime(target / "f", (old, old))
    (home / "kernels" / "link").symlink_to(target)
    (home / "kernels" / "stray.txt").write_text("x")
    assert kernel.gc_kernel_dirs() == []
    assert (target / "f").exists() and (home / "kernels" / "stray.txt").exists()


def test_no_kernels_root_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "never-created"))
    assert kernel.gc_kernel_dirs() == []
