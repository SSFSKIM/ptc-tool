"""A kernel confirmed DEAD still has a process group, and its children are still in it.

The kernel is spawned `start_new_session=True`, so its pgid equals its pid and every
ordinary child it makes — the Claude SDK's `claude` CLI, a `codex app-server`, a plain
`subprocess` — lands in that group. The live path has always taken the group
(`kill_process_tree`); the CONFIRMED-DEAD path took nothing at all, so an OOM-killed or
crashed kernel left its children running (and billing) while a replacement spawned beside
them. `bgroups.reap` does not cover them: it only knows the background `bash()` groups,
which are in sessions of their own precisely so the kernel's group kill misses them.

Signalling a dead record's group takes the same existence gate `bgroups` applies to a
`leader_exited` row, and these tests pin both of its answers.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

from ptc import bgroups, kernel
from ptc.ownership import Owner, UnknownOwner, write_owner
from ptc.paths import kernel_dir, secure_dir

#: A bare `ps -o lstart=` string no live process on this machine can be wearing. Recorded
#: rather than a birth stamp on purpose: a record that names no source is compared against
#: the two legacy readings and answers a definite MISMATCH, where a `btime=` record read on
#: a machine whose libproc call failed would answer "unknown" and raise `UnknownOwner`.
_ANCIENT = "Thu Jan  1 00:00:00 2000"


def _record(key: str, pid: int) -> None:
    secure_dir(kernel_dir(key))
    write_owner(key, Owner(pid, _ANCIENT, time.time(), "n1", "e1"))


def _dead_pid() -> int:
    """A pid nothing is wearing: a child run to completion and reaped."""
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    if bgroups._pid_exists(p.pid):
        pytest.skip("the pid was handed out again before the test could use it")
    return p.pid


@pytest.fixture
def killpg(monkeypatch):
    """Every group SIGKILL attempted, and none of them delivered."""
    sent: list = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    return sent


def test_kill_reaps_the_group_of_a_kernel_that_is_already_gone(monkeypatch, tmp_path,
                                                              killpg):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    pid = _dead_pid()
    _record("dk1", pid)

    assert kernel.kill_kernel("dk1") is False, "nothing was signalled, so nothing was killed"

    assert killpg == [(pid, signal.SIGKILL)], \
        f"the dead kernel's orphaned children were left running: {killpg}"


def test_clean_stale_reaps_the_group_before_it_spawns_the_replacement(monkeypatch,
                                                                     tmp_path, killpg):
    """`ensure_kernel`'s cleanup is the other door to the same room: it deletes the dead
    record and spawns a fresh kernel, and until the group goes with it the replacement runs
    beside the old kernel's still-live agent backends."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    pid = _dead_pid()
    kd = secure_dir(kernel_dir("dk2"))
    owner = Owner(pid, _ANCIENT, time.time(), "n1", "e1")

    kernel._clean_stale(kd, "dk2", owner, False)

    assert killpg == [(pid, signal.SIGKILL)], \
        f"the replacement was spawned beside the dead kernel's children: {killpg}"


def test_a_recycled_pid_is_never_signalled(monkeypatch, tmp_path, killpg):
    """The record is stale because the identity MISMATCHED, and a mismatch has two shapes:
    the process is gone, or its number was handed out again. Only the second leaves a
    process wearing that pid — and its group is a stranger's, so existence is the whole
    gate, exactly as it is for a `leader_exited` bash row."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _record("dk3", os.getpid())          # alive, and its birth identity cannot match

    assert kernel.kill_kernel("dk3") is False

    assert killpg == [], f"a recycled pid's group — this test runner's own — was killed: {killpg}"


def test_an_unreadable_identity_still_signals_nothing(monkeypatch, tmp_path, killpg):
    """`UnknownOwner` is neither answer, and it is the one path that must stay untouched:
    a live kernel whose `ps` read failed twice is not a dead kernel with an orphaned
    group."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _record("dk4", os.getpid())
    monkeypatch.setattr("ptc.ownership.owner_state", lambda o: None)

    with pytest.raises(UnknownOwner):
        kernel.kill_kernel("dk4")

    assert killpg == [], f"an unidentifiable owner's group was killed: {killpg}"
