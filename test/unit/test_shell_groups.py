"""What happens to a `bash()` command's process GROUP when the call ends unusually.

Every case here is the same hazard from a different side: a group whose leader is gone but
whose members are not. A `bash()` child is its own session by design, so the kernel's own
group kill never reaches it and the per-kernel registry is the only handle anything else
has on it. Foreground commands are registered for the duration of the call (so a kill or
restart landing mid-command still reaps it) and cancellation kills the group on the way
out; a background command's row must STAY while its descendants live, and `kill()` has to
signal the pgid that was RECORDED rather than one recomputed from a leader that has exited.
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


def test_a_foreground_command_is_reapable_while_it_runs(tmp_path):
    """`ptc kill`/`restart`/the TTL watchdog reach a `bash()` child through one channel
    only: the pgid registry. A foreground command was never in it, so a kill landing
    mid-command left the command and its descendants running after the kernel was gone —
    exactly the orphan the background registration exists to prevent."""
    pidfile = tmp_path / "child.pid"

    async def flow() -> tuple[int, list]:
        task = asyncio.ensure_future(
            shell.bash(f"sleep 300 & echo $! > {pidfile}; wait", timeout=120))
        pid = int(await _wait_for(pidfile))
        rows = bgroups.read(tmp_path)        # what a HOST process would find on disk
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        return pid, rows

    pid, rows = asyncio.run(flow())
    try:
        assert len(rows) == 1, f"the in-flight foreground group was not registered: {rows}"
        assert rows[0]["leader_start"], "the row must carry the leader identity, as bg does"
        assert _gone(pid), "the payload outlived the cancelled command"
        assert bgroups.read(tmp_path) == [], "the row outlived the group it named"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_a_registered_foreground_group_is_reaped_by_the_host(tmp_path):
    """The consequence: `bgroups.reap` — what kill/restart/TTL all call — kills the payload
    of a command that is still in flight, without ever having seen its pid."""
    pidfile = tmp_path / "child.pid"
    killed: list = []

    async def flow() -> int:
        task = asyncio.ensure_future(
            shell.bash(f"sleep 300 & echo $! > {pidfile}; wait", timeout=120))
        pid = int(await _wait_for(pidfile))
        killed.extend(bgroups.reap(tmp_path))       # the host's reap, mid-command
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return pid

    pid = asyncio.run(flow())
    try:
        assert killed, "the host reap found no group to signal"
        assert _gone(pid), f"payload {pid} survived the reap a kill/restart would run"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_killing_a_daemonizers_handle_reaps_the_group_its_row_was_kept_for(tmp_path):
    """`kill()` recomputed the group from the leader pid. Once that leader has exited, the
    lookup raises, the `proc.kill()` fallback reaches nobody — and the unconditional
    unregister then threw away the one pgid the descendants could still be reaped by, so
    `h.kill()` on a daemonizer leaked the child permanently."""
    async def flow() -> tuple[int, list]:
        h = await shell.bash("sleep 300 >/dev/null 2>&1 & echo $!", background=True)
        r = await h.wait()                  # the shell leader is gone and reaped by here
        await asyncio.sleep(0.2)
        pid = int(r.stdout.strip())
        assert not _gone(pid, patience=0.5), "the descendant died before kill() was tested"
        h.kill()
        return pid, bgroups.read(tmp_path)

    pid, rows = asyncio.run(flow())
    try:
        assert _gone(pid), f"h.kill() never reached descendant {pid} — its row named it"
        assert rows == [], f"the row outlived the group kill() emptied: {rows}"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_killing_a_live_background_handle_still_ends_it(tmp_path):
    """The other side of the same method: with the leader still alive and still ours to
    reap, kill() must take the group down and the row must not survive the command."""
    async def flow() -> int:
        h = await shell.bash("sleep 300", background=True)
        await asyncio.sleep(0.2)
        pid = h.pid
        h.kill()
        await h.wait()
        await asyncio.sleep(0.2)            # let the watcher retire the emptied group
        return pid

    pid = asyncio.run(flow())
    assert _gone(pid), f"kill() left {pid} running"
    assert bgroups.read(tmp_path) == [], "the row outlived the killed group"


def test_a_background_command_whose_group_really_ended_is_unregistered(tmp_path):
    """The other half of the same rule: an empty group's pgid is free for the OS to hand
    out again, so its row must go — a reap that trusted it would signal a stranger."""
    async def flow():
        h = await shell.bash("echo done", background=True)
        await h.wait()
        await asyncio.sleep(0.2)

    asyncio.run(flow())
    assert bgroups.read(tmp_path) == []
