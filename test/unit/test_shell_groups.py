"""What happens to a `bash()` command's process GROUP when the call ends unusually.

Both cases here are the same hazard from opposite ends: a group whose leader is gone but
whose members are not. A foreground command's group is nobody's to reap — it is its own
session (so the kernel's group kill misses it) and it is not in the registry (so
kill/restart/TTL miss it too) — so cancellation has to kill it on the way out. A
background command's group IS in the registry, and must STAY there while its descendants
live, or the reapers lose the only pgid they could use.
"""
import asyncio
import os
import time

import pytest

from ptc import bgroups
from ptc.runtime import shell
from ptc.runtime.state import STATE


@pytest.fixture(autouse=True)
def _fresh_registry(tmp_path, monkeypatch):
    """`_LIVE` is module state that outlives one kernel's worth of commands in a test
    process; a leftover row would be re-persisted by the next test's `_persist()`. The
    kernel dir moves under tmp_path with it, so `bash()`'s audit line lands there rather
    than in the working directory."""
    monkeypatch.setattr(STATE, "kernel_dir", tmp_path)
    shell._LIVE.clear()
    yield
    shell._LIVE.clear()


def _gone(pid: int, patience: float = 10.0) -> bool:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


async def _wait_for(path, patience: float = 10.0) -> str:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never got its pid")


def test_cancelling_a_foreground_bash_kills_its_process_group(tmp_path):
    """A cell interrupt cancels the coroutine, and CancelledError bypasses the
    timeout-only cleanup: the payload process survived the cell, the kernel, and every
    later kill/restart/TTL, because nothing anywhere held its pgid."""
    pidfile = tmp_path / "child.pid"

    async def flow() -> int:
        task = asyncio.ensure_future(
            shell.bash(f"sleep 300 & echo $! > {pidfile}; wait", timeout=120))
        pid = int(await _wait_for(pidfile))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task                      # and the cancellation still propagates
        await asyncio.sleep(0.2)            # let the transport collect the dead child
        return pid

    pid = asyncio.run(flow())
    assert _gone(pid), f"payload process {pid} outlived the cancelled foreground bash"


def test_a_daemonizing_background_command_keeps_its_registry_row(tmp_path):
    """`sleep 300 >/dev/null 2>&1 &` exits its shell at once and leaves the descendant in
    the SAME group. Unregistering on the LEADER's exit threw away the only pgid anything
    could reap that descendant by, so it outlived the kernel; the row survives until the
    GROUP is empty, and the reaper still honors it with its leader gone."""
    async def flow() -> int:
        h = await shell.bash("sleep 300 >/dev/null 2>&1 & echo $!", background=True)
        r = await h.wait()                  # the shell leader is gone by here
        await asyncio.sleep(0.2)            # ...and so is the watcher that would forget it
        return int(r.stdout.strip())

    pid = asyncio.run(flow())
    try:
        rows = bgroups.read(tmp_path)
        assert len(rows) == 1, f"the daemonizer's group was unregistered: {rows}"
        assert rows[0]["leader_exited"] is True, "the row must say its leader is gone"
        assert not _gone(pid, patience=0.5), "the descendant died on its own"

        assert bgroups.reap(tmp_path) == [rows[0]["pgid"]]
        assert _gone(pid), f"descendant {pid} survived the reap that had its pgid"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_a_background_command_whose_group_really_ended_is_unregistered(tmp_path):
    """The other half of the same rule: an empty group's pgid is free for the OS to hand
    out again, so its row must go — a reap that trusted it would signal a stranger."""
    async def flow():
        h = await shell.bash("echo done", background=True)
        await h.wait()
        await asyncio.sleep(0.2)

    asyncio.run(flow())
    assert bgroups.read(tmp_path) == []
