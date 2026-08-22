"""async shell for the kernel. %%bash magic remains available alongside."""
import asyncio
import os
import signal
import time
from dataclasses import dataclass

from ptc import bgroups
from ptc.ownership import proc_start_time

from . import audit
from .state import STATE

#: Live `bash()` process groups this kernel started, keyed by pgid: every background handle
#: until its group ends, and every foreground command for the duration of its call. Mirrored to
#: `<kernel_dir>/bash-pgids.json` on every change so the host (kill/restart) and the
#: watchdog (TTL exit) can reap groups they never spawned — see ptc/bgroups.py.
_LIVE: dict[int, dict] = {}

#: Exit watchers, retained: a task nobody holds can be collected mid-await, and the entry
#: it was going to remove would then linger until the kernel dies.
_WATCHERS: set = set()

#: How long `BashHandle.kill()` waits for a SIGKILLed group to empty before deciding whether
#: its row can go. `killpg` returning means the signal was POSTED, not that the members are
#: gone; the descendants of an exited shell are orphans reaped by init, so this is a
#: milliseconds-scale wait in practice and a bound for the case where it is not.
_KILL_SETTLE_S = 1.0

#: How often the watcher of a RETAINED (`leader_exited`) row asks whether the orphaned
#: group has finally emptied. Coarse on purpose: nobody awaits the answer — it only bounds
#: how long such a row may name a group that is already gone — and the poll runs for as
#: long as the daemon does, on the kernel's own loop.
_ORPHAN_POLL_S = 5.0


def _persist() -> None:
    # Outside a bootstrapped kernel (a bare `from ptc.runtime import bash` in a test or a
    # script) STATE.kernel_dir is still its placeholder, and there is no kernel for anyone
    # to reap on behalf of — mirroring there would just litter the cwd.
    if str(STATE.kernel_dir) in (".", ""):
        return
    bgroups.write(STATE.kernel_dir, list(_LIVE.values()))


def _register(proc, cmd: str) -> int | None:
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # The leader exited inside this call. That says nothing about its GROUP: `bash()`
        # spawns `start_new_session=True`, so setsid has already made the pgid equal to the
        # pid — the number is known by CONSTRUCTION, not by the read that just failed — and
        # a daemonizing command's descendants are sitting in it right now. Returning None
        # here handed back a handle with `_pgid=None`, which neither `h.kill()` nor
        # kill/restart/TTL can reach: a permanent leak, on precisely the command shape the
        # whole retention rule exists for. So ask the only question that is still
        # answerable — does the GROUP have members — and keep None for the case where it
        # genuinely does not.
        pgid = proc.pid
        if not _group_alive(pgid):
            return None                   # truly empty: nothing to reap later
        # No birth stamp exists to record (its subject is gone), so the row says what it
        # does know instead: the leader IS exited, and the pgid came from the constructor.
        # `bgroups` reads that pair as identity enough to signal under its existence gate;
        # a legacy bare row, which claims neither, stays quarantined (`unverifiable`).
        _LIVE[pgid] = {"pgid": pgid, "pid": proc.pid, "leader_start": None,
                       "cmd": cmd[:200], "started_at": time.time(),
                       "leader_exited": True, "pgid_source": "setsid"}
        _persist()
        return pgid
    # The leader's start time is recorded WITH the pgid: a pgid alone is reusable, and a
    # reaper that trusts a stale row can SIGKILL whatever same-user group inherited the
    # number (ptc/bgroups.py `_recycled`). Same identity pair as ownership.py's.
    #
    # `ps` can fail transiently, and a leader that exits inside this call cannot be read at
    # all — so the read is retried once (the same one-retry the kernel spawn gives its own
    # identity), and a row that still has no identity is written MARKED rather than written
    # bare. Nothing about a bare row tells the reaper it was never verified — it once read
    # as "not recycled", and once its pgid was handed out again the reap killed a stranger.
    # The mark is what keeps such a row droppable but never signalable
    # (`bgroups.unverifiable`), independently of what an identity read says about a pgid
    # nobody recorded.
    # Registration does not fail on it: the command is already running, and a row that can
    # be dropped but not signalled is strictly better than no row at all — `kill()` and
    # `_retire` still find the group through it.
    start = proc_start_time(pgid) or proc_start_time(pgid)
    _LIVE[pgid] = {"pgid": pgid, "pid": proc.pid, "leader_start": start,
                   "cmd": cmd[:200], "started_at": time.time(),
                   **({} if start else {"unverifiable": True})}
    _persist()
    return pgid


def _unregister(pgid: int | None) -> None:
    if pgid is not None and _LIVE.pop(pgid, None) is not None:
        _persist()


def _group_alive(pgid: int | None) -> bool:
    """True while the process GROUP still has members — not merely its leader.

    `killpg(pgid, 0)` is the only question that asks about the group: ESRCH proves it is
    empty (and the pgid free for the OS to hand out again), success proves it is not.
    """
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # members exist, just not signalable by us
    except OSError:
        return False
    return True


def _retire(pgid: int | None) -> None:
    """The shell leader has exited: drop the row only if the whole GROUP went with it.

    A daemonizing command (`sleep 300 >/dev/null 2>&1 &`) exits its shell immediately and
    leaves the descendant behind in the SAME group, so unregistering on the leader's exit
    threw away the only PGID anything could still reap it by, and the descendant outlived
    the kernel. The row stays, marked `leader_exited`, and that mark is what carries its
    signal-eligibility: an exited leader has no identity left to read, and `bgroups`
    otherwise refuses to signal a group it cannot identify. The exception is sound because
    a pid cannot be recycled while it is still a live group's id — and this row is retained
    only while the group has members — so an absent leader here is the recorded, expected
    state rather than the stranger the identity check exists to spare. Losing
    the leader does not cost a row its signal-eligibility either: it was identified when it
    was REGISTERED, and only a row that never was is quarantined (`bgroups.unverifiable`).
    """
    if not _group_alive(pgid):
        _unregister(pgid)
        return
    row = _LIVE.get(pgid)
    if row is not None and not row.get("leader_exited"):
        row["leader_exited"] = True
        _persist()


async def _watch_orphans(pgid: int | None) -> None:
    """Stay on a RETAINED row's group until it empties, then drop the row.

    `_retire` keeps the row because the leader's exit left descendants behind — that pgid
    is the only handle anything has on them — and then nothing ever asked again: the
    watcher had awaited the LEADER, so the row outlived the group by however long it took
    the next kill/restart/TTL reap to consume the file. A row naming an emptied group names
    a NUMBER, and a free pgid is one the OS hands out again, so unbounded staleness here is
    the raw material of every recycle hazard downstream. `killpg(pgid, 0)` is the same
    question `_group_alive` asks, on a coarse interval, in the same task that was already
    watching — no new machinery, and it ends the moment the row does (a `kill()` that
    unregisters first stops this loop on its next look).
    """
    while _LIVE.get(pgid) is not None:
        if not _group_alive(pgid):
            _unregister(pgid)
            return
        await asyncio.sleep(_ORPHAN_POLL_S)


def _track_orphans(pgid: int | None) -> None:
    """`_watch_orphans` as a retained task, for the paths that are not one already.

    A FOREGROUND daemonizer retains a row exactly like a background one (`bash()`'s
    finally clause) but has no watcher of its own — its call is over. Retained in
    `_WATCHERS` for the same reason every other watcher is: a task nobody holds can be
    collected mid-await, and the row it was going to drop would linger until the kernel dies.
    """
    if _LIVE.get(pgid) is None:
        return
    task = asyncio.ensure_future(_watch_orphans(pgid))
    _WATCHERS.add(task)
    task.add_done_callback(_WATCHERS.discard)


@dataclass
class BashResult:
    code: int | None
    stdout: str
    stderr: str
    timed_out: bool


def _killpg_or_kill(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _reap_foreground(proc, readers) -> None:
    """Kill a foreground command's group and settle its readers, raising nothing.

    This runs with an exception already in flight (usually CancelledError), which the
    caller re-raises — so a second CancelledError arriving here must be swallowed rather
    than replace it. The output is discarded on this path, so the pumps are cancelled
    rather than drained; awaiting them is what guarantees no reader task is left running
    against a dead pipe.
    """
    _killpg_or_kill(proc)
    readers.cancel()
    try:
        await readers
    except BaseException:
        pass


async def _pump(stream, buf) -> None:
    while chunk := await stream.read(65536):
        buf.append(chunk)


#: Per stream, how much of a BACKGROUND command's output a handle keeps. A foreground
#: command's buffers live exactly as long as its call, so its whole output is bounded by
#: the timeout; a background handle's live as long as the KERNEL, and `yes` or a chatty
#: server grows them until the kernel is OOM-killed. Generous enough that no realistic
#: command notices — `read()`ing a produced file is the answer for anything larger.
_BG_BUFFER_BYTES = 4_000_000


class _Bounded:
    """A byte buffer that keeps the head and the tail of a stream and elides the middle.

    Both ends matter and for different reasons: a command's opening lines say what it
    decided to do (and carry the failure that made it noisy), its last lines say where it
    got to. What goes is the repetitive middle, and it goes visibly — the elision is
    rendered into the output rather than silently closing the gap, so nobody reads a
    truncated log as a complete one.
    """

    def __init__(self, limit: int):
        self._head_max = limit // 2
        self._tail_max = limit - self._head_max
        self._head = bytearray()
        self._tail = bytearray()
        self._elided = 0

    def append(self, chunk: bytes) -> None:
        room = self._head_max - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
            if not chunk:
                return
        self._tail += chunk
        excess = len(self._tail) - self._tail_max
        if excess > 0:
            del self._tail[:excess]
            self._elided += excess

    def value(self) -> bytes:
        if not self._elided:
            return bytes(self._head + self._tail)
        return (bytes(self._head)
                + f"\n[... {self._elided} bytes elided: a background handle keeps the "
                  f"first and last {self._head_max} bytes of each stream ...]\n".encode()
                + bytes(self._tail))


class BashHandle:
    """A backgrounded subprocess. Output accumulates in memory as it streams;
    output() returns stdout captured so far (before or after completion).
    Its stdin is /dev/null (the kernel spawns background children detached,
    stdin=DEVNULL) — a command that tries to read stdin gets immediate EOF
    rather than blocking.

    A handle lives as long as the kernel, so what it keeps is BOUNDED: past
    `_BG_BUFFER_BYTES` per stream the middle is elided and the notice saying so is part
    of the output. Redirect to a file and `read()` it when a command's whole stream
    matters."""

    def __init__(self, proc, cmd: str):
        self._proc = proc
        self._cmd = cmd
        self._out = _Bounded(_BG_BUFFER_BYTES)
        self._err = _Bounded(_BG_BUFFER_BYTES)
        self._pump = asyncio.gather(_pump(proc.stdout, self._out),
                                     _pump(proc.stderr, self._err))
        # A background group survives the kernel unless somebody records it: it is its own
        # session, so no group kill on the kernel reaches it.
        self._pgid = _register(proc, cmd)
        # The registry row ITSELF, not a copy of it: `_retire` will take it out of `_LIVE`
        # while this handle lives on holding the bare number — and a bare number is not an
        # identity (`kill`) — but it also MARKS it `leader_exited` in place, which is the
        # one fact that keeps a daemonizer's orphaned group signalable once its leader is
        # unreadable (`bgroups._recycled`). A snapshot taken at registration never sees that
        # mark, so `kill()` read the group as unidentifiable and left the descendants
        # running. Holding the object retains the row past its removal just as well.
        self._row = _LIVE.get(self._pgid) or {}
        watcher = asyncio.ensure_future(self._forget_when_done())
        _WATCHERS.add(watcher)
        watcher.add_done_callback(_WATCHERS.discard)

    async def _forget_when_done(self) -> None:
        """Drop the registry entry the moment the GROUP ends, so a reap never signals a
        pgid the OS has since handed to somebody else — and never before, or a daemonized
        descendant loses the only handle anything has on it (`_retire`)."""
        try:
            await self._proc.wait()
        finally:
            _retire(self._pgid)
        # `_retire` may have KEPT the row — a daemonizer's descendants outlive its shell —
        # and this task is the only thing that will ever look at that group again. Awaited
        # rather than spawned: staying here is what makes the retention bounded (r11).
        await _watch_orphans(self._pgid)

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> int | None:
        return self._proc.returncode

    def output(self) -> str:
        """stdout captured so far — with the middle elided, and a notice saying by how
        much, once this stream has produced more than `_BG_BUFFER_BYTES`."""
        return self._out.value().decode(errors="replace")

    async def wait(self) -> BashResult:
        await self._proc.wait()
        await self._pump
        _retire(self._pgid)         # the watcher would too; this makes it observable to
                                    # whoever awaited, without a scheduling race
        return BashResult(self._proc.returncode, self.output(),
                           self._err.value().decode(errors="replace"), False)

    def kill(self) -> None:
        """SIGKILL the RECORDED group, and retire its row only once that group is empty.

        Recomputing the group from the leader's pid is wrong for exactly the case `_retire`
        exists for. A daemonizing command's shell has already exited leaving descendants in
        the group, so `os.getpgid(<reaped pid>)` raises, the `proc.kill()` fallback reaches
        nobody — and the unconditional unregister then threw away the one pgid anything
        could still have reaped those descendants by. `h.kill()` in that state leaked the
        child permanently.

        Verifying the recorded group is the other half, and it is bgroups' rule rather than
        a second one. Once `_retire` has dropped the row the handle still holds the number,
        and a pgid whose group has ended is free for the OS to hand out again: signalling it
        then SIGKILLs unrelated same-user work — precisely the hazard `bgroups.reap` refuses
        to take, reached by a path that used to bypass its checks. A handle whose recorded
        identity no longer holds signals no group at all; if its own leader is somehow still
        alive it is reached by pid, which can only ever be the process this handle spawned.
        """
        if self._pgid is None:              # never registered: the leader was already gone
            _killpg_or_kill(self._proc)
            return
        if not bgroups.signalable(self._row):
            if self._proc.returncode is None:
                try:
                    self._proc.kill()
                except (OSError, ProcessLookupError):
                    pass
            _unregister(self._pgid)
            return
        try:
            os.killpg(self._pgid, signal.SIGKILL)
        except OSError:
            pass                            # already empty (ESRCH) or not ours (EPERM)
        if self._proc.returncode is None:
            # The leader is still ours to reap, so the only member `_group_alive` could see
            # right now is its own zombie. `_forget_when_done` is awaiting that wait() and
            # retires the row the moment the group really ends.
            _retire(self._pgid)
            return
        # The leader was reaped already, so nothing else will ever look at this row again:
        # this call owns it. Give the signalled group a bounded moment to empty first.
        deadline = time.monotonic() + _KILL_SETTLE_S
        while _group_alive(self._pgid) and time.monotonic() < deadline:
            time.sleep(0.01)
        _retire(self._pgid)


async def bash(cmd: str, timeout: float = 120.0, cwd=None, env=None,
                background: bool = False):
    """Run `cmd` in a shell. Returns a BashResult, or (if background=True) a
    BashHandle for a detached process whose output can be polled/awaited.

    env, if given, is MERGED over the parent's environment (os.environ),
    not a wholesale replacement as with plain subprocess/asyncio.create_subprocess_*
    — pass env={"FOO": "bar"} to add/override FOO while keeping PATH etc.

    On timeout, output already produced before the kill is preserved in the
    returned BashResult (code=None, timed_out=True) rather than dropped.
    """
    audit.append("bash", command=cmd[:200])
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    if background:
        return BashHandle(proc, cmd)

    # A foreground command is its own session too, so the kernel's own group kill misses it
    # — and while it was absent from the registry, `ptc kill`/`restart` and the TTL watchdog
    # missed it as well: bgroups.reap is the only channel to a HOST that never saw this pid.
    # A kill landing mid-`bash()` therefore left the command and its descendants running
    # after the kernel was gone. The row lives exactly as long as the call.
    pgid = _register(proc, cmd)
    out: list[bytes] = []
    err: list[bytes] = []
    readers = asyncio.gather(_pump(proc.stdout, out), _pump(proc.stderr, err))
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        await readers
        return BashResult(proc.returncode, b"".join(out).decode(errors="replace"),
                           b"".join(err).decode(errors="replace"), False)
    except asyncio.TimeoutError:
        _killpg_or_kill(proc)
        await readers
        return BashResult(None, b"".join(out).decode(errors="replace"),
                           b"".join(err).decode(errors="replace"), True)
    except BaseException:
        # Cancellation — a cell interrupt, a task torn down — or any other failure. It
        # bypasses the timeout handler above, so kill the group here, settle the readers,
        # and let the original propagate.
        await _reap_foreground(proc, readers)
        raise
    finally:
        # Only the normal path has waited on the leader, and only there can `_group_alive`
        # answer honestly: an ended group drops its row, while a foreground daemonizer's
        # surviving descendants keep it (the same pgid the reapers need, `_retire`). Both
        # kill paths above SIGKILLed the whole group, so there is nothing left to keep a row
        # for — and the leader is an unreaped zombie there, which is still a group member
        # and would make `_group_alive` answer "alive" for good.
        if proc.returncode is not None:
            _retire(pgid)
            # A foreground daemonizer's row is retained on the same terms as a background
            # one, and this call is over — so the group it names gets a watcher of its own
            # rather than waiting for the next reap to notice it is empty.
            _track_orphans(pgid)
        else:
            _unregister(pgid)
