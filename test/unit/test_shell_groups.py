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


def test_killing_a_retired_handle_whose_pgid_was_recycled_signals_nobody(
        tmp_path, monkeypatch):
    """A handle outlives its group. `_retire` drops the row when the group empties, but the
    handle still holds the bare number — and a pgid whose group has ended is free for the OS
    to hand out again. `kill()` signalled it regardless, SIGKILLing whatever same-user work
    inherited the number: the stranger-killing hazard `bgroups.reap` refuses to take,
    reached by the one path that bypassed its rules. It asks the same question now.
    """
    signalled: list = []

    async def flow():
        h = await shell.bash("echo done", background=True)
        await h.wait()
        await asyncio.sleep(0.2)            # let the watcher retire the emptied group
        return h

    h = asyncio.run(flow())
    assert bgroups.read(tmp_path) == [], "the ended group's row was not retired"

    # what a recycled pgid looks like to the only question that can tell: the number is
    # now led by a DIFFERENT process than the one that was registered
    monkeypatch.setattr(bgroups, "start_time_matches", lambda pid, recorded: False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))

    h.kill()

    assert [s for s in signalled if s[1] != 0] == [], \
        f"kill() SIGKILLed a group the handle no longer owns: {signalled}"


# --- r11 finding 2: a retained row's staleness is BOUNDED -----------------------------

def test_a_retained_row_goes_when_its_orphaned_group_finally_empties(tmp_path, monkeypatch):
    """`_retire` keeps a daemonizer's row because its descendants are still in the group —
    and then nothing ever asked again. The watcher awaited the LEADER, so once it exited the
    row sat there naming a group that emptied seconds later, until the next kill/restart/TTL
    reap happened to consume the file. A row that names an emptied group names a NUMBER, and
    a free pgid is one the OS hands out again: that staleness is the raw material every
    recycle hazard downstream is built from. The same watcher stays on the group instead.
    """
    monkeypatch.setattr(shell, "_ORPHAN_POLL_S", 0.05)

    async def flow() -> tuple[int, list]:
        h = await shell.bash("sleep 0.6 >/dev/null 2>&1 & echo $!", background=True)
        pid = int((await h.wait()).stdout.strip())
        await asyncio.sleep(0.2)
        assert bgroups.read(tmp_path), "the row went while its orphaned group still lived"
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and bgroups.read(tmp_path):
            await asyncio.sleep(0.05)
        return pid, bgroups.read(tmp_path)

    pid, rows = asyncio.run(flow())
    try:
        assert _gone(pid), "the fixture's descendant outlived its own sleep"
        assert rows == [], f"the row outlived the orphaned group it was kept for: {rows}"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# --- r11 finding 3: a leader that exits before registration still has a GROUP ----------

def test_a_daemonizer_whose_leader_exits_before_registration_is_still_reapable(
        tmp_path, monkeypatch):
    """`os.getpgid(proc.pid)` raising meant "already exited: nothing to reap later", and
    that second half is false. `start_new_session=True` means setsid already made the pgid
    the pid, so the group id is known by CONSTRUCTION — and a daemonizer's descendants are
    sitting in it. Dropping the registration returned a handle with no pgid at all, so
    neither `h.kill()` nor kill/restart/TTL could ever reach them: a permanent leak, on the
    exact command shape the whole retention rule exists for.

    The row it writes now has no birth stamp to offer (its leader is gone), so it says what
    it does know instead: `leader_exited` plus a pgid the constructor guarantees. That pair
    is what `bgroups` lets past the quarantine — a legacy bare row, which claims neither,
    still does not get through.
    """
    real_getpgid = os.getpgid

    def gone_by_the_time_we_ask(pid):
        if pid == 0:                       # the reaper asking about ITSELF is untouched
            return real_getpgid(0)
        raise ProcessLookupError(pid)

    async def flow() -> int:
        monkeypatch.setattr(os, "getpgid", gone_by_the_time_we_ask)
        try:
            h = await shell.bash("sleep 300 >/dev/null 2>&1 & echo $!", background=True)
            return int((await h.wait()).stdout.strip())
        finally:
            monkeypatch.setattr(os, "getpgid", real_getpgid)

    pid = asyncio.run(flow())
    try:
        rows = bgroups.read(tmp_path)
        assert len(rows) == 1, f"the group was dropped as if it had no members: {rows}"
        assert rows[0]["leader_exited"] is True and rows[0]["leader_start"] is None
        assert bgroups.signalable(rows[0]), "a construction-known pgid stayed quarantined"
        assert not _gone(pid, patience=0.5), "the descendant died before the reap was tested"

        assert bgroups.reap(tmp_path) == [rows[0]["pgid"]]
        assert _gone(pid), f"descendant {pid} had no pgid anything could reap it by"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# --- r8 finding 9 / r12 finding 3: every command's buffers are bounded -----------------

_CHATTY = "yes hello | head -n 1000000"          # 6 000 000 bytes, whole lines
_CHATTY_BYTES = len("hello\n") * 1_000_000


def test_a_chatty_background_command_keeps_a_bounded_buffer(tmp_path):
    """A background handle lives as long as the KERNEL, and `_pump` appended every chunk
    to a list for the process's whole lifetime: `yes`, or a long-running server writing
    logs, grew the kernel until it was OOM-killed. Head and tail are kept, the middle goes,
    and the elision is rendered into the output so nobody reads it as a complete log.
    """
    async def flow():
        h = await shell.bash(_CHATTY, background=True)
        return await h.wait(), h

    r, h = asyncio.run(flow())

    assert len(h._out._head) + len(h._out._tail) <= shell._OUTPUT_BUFFER_BYTES, \
        "the handle retained more than its bound"
    assert "bytes elided" in r.stdout, "output was truncated with no notice that it was"
    assert r.stdout.startswith("hello\n"), "the opening of the stream was lost"
    assert r.stdout.endswith("hello\n"), "the end of the stream was lost"
    assert len(r.stdout) < _CHATTY_BYTES


def test_a_chatty_foreground_command_keeps_a_bounded_buffer(tmp_path, monkeypatch):
    """This asserted the opposite until r12: "a foreground command's buffers live exactly
    as long as its call, so the timeout already bounds them". It does not bound them at
    all — `yes` writes gigabytes in seconds and OOM-kills the persistent kernel long
    before 120 s elapse, taking every session on that key with it. The bound is the same
    one a handle gets, and the buffer OBJECT is the same, so both result shapes carry
    head, elision notice and tail and read alike.

    The buffers themselves are what is asserted: they are local to the `bash()` call, so a
    recording subclass is what reaches them — measuring the returned text alone would pass
    just as well against an unbounded list that happened to be truncated on the way out.
    """
    made: list = []

    class _Recording(shell._Bounded):
        def __init__(self, limit):
            super().__init__(limit)
            made.append(self)

    monkeypatch.setattr(shell, "_Bounded", _Recording)
    r = asyncio.run(shell.bash(_CHATTY, timeout=120))

    assert made, "the foreground path never used the bounded buffer"
    assert all(len(b._head) + len(b._tail) <= shell._OUTPUT_BUFFER_BYTES for b in made), \
        [(len(b._head), len(b._tail)) for b in made]
    assert "bytes elided" in r.stdout, "output was truncated with no notice that it was"
    assert r.stdout.startswith("hello\n"), "the opening of the stream was lost"
    assert r.stdout.endswith("hello\n"), "the end of the stream was lost"
    assert len(r.stdout) < _CHATTY_BYTES


# --- r12 finding 2: an unregistered handle never re-derives a group --------------------

def _no_group_calls(monkeypatch) -> list:
    """Record every group-level call `kill()` makes. On the unregistered branch there must
    be none at all: the only safe question left is about the child itself."""
    calls: list = []
    real_getpgid, real_killpg = os.getpgid, os.killpg
    monkeypatch.setattr(os, "getpgid", lambda pid: calls.append(("getpgid", pid))
                        or real_getpgid(pid))
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig))
                        or real_killpg(pgid, sig))
    return calls


def test_killing_an_unregistered_handle_never_re_derives_a_group(tmp_path, monkeypatch):
    """`_pgid is None` means registration already PROVED the group empty. The leader has
    since been reaped, so its pid is free for the OS to hand out again — and `killpg(
    getpgid(pid), SIGKILL)` on it resolves a STRANGER's group and kills that instead. A
    handle in this state owns nothing anybody can still signal, and asks nothing.
    """
    async def flow():
        monkeypatch.setattr(shell, "_register", lambda proc, cmd: None)
        h = await shell.bash("echo done", background=True)
        await h.wait()
        return h

    h = asyncio.run(flow())
    assert h._pgid is None and h.poll() is not None
    calls = _no_group_calls(monkeypatch)

    h.kill()

    assert calls == [], f"kill() went looking for a group on a recycled pid: {calls}"


def test_killing_an_unregistered_handle_still_ends_its_own_child(tmp_path, monkeypatch):
    """The other half: while the child is UN-REAPED its pid cannot be recycled — the
    transport holds the zombie — so signalling it directly reaches this handle's own
    process and nobody else's. That is what makes the direct kill safe where the group
    lookup is not, and dropping the group lookup must not turn kill() into a no-op."""
    async def flow():
        monkeypatch.setattr(shell, "_register", lambda proc, cmd: None)
        h = await shell.bash("sleep 300", background=True)
        await asyncio.sleep(0.1)
        return h

    h = asyncio.run(flow())
    assert h._pgid is None and h.poll() is None
    calls = _no_group_calls(monkeypatch)

    h.kill()

    assert calls == [], f"kill() went looking for a group on a live child: {calls}"
    assert _gone(h.pid), "the handle's own child survived its kill()"
