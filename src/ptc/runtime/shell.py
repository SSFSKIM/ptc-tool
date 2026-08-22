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
    except OSError:                       # already exited: nothing to reap later
        return None
    # The leader's start time is recorded WITH the pgid: a pgid alone is reusable, and a
    # reaper that trusts a stale row can SIGKILL whatever same-user group inherited the
    # number (ptc/bgroups.py `_recycled`). Same identity pair as ownership.py's.
    #
    # `ps` can fail transiently, and a leader that exits inside this call cannot be read at
    # all — so the read is retried once (the same one-retry the kernel spawn gives its own
    # identity), and a row that still has no identity is written MARKED rather than written
    # bare. A bare row is indistinguishable from a verified one to the reaper: it reads as
    # "not recycled", and once its pgid is handed out again the reap kills a stranger. The
    # mark is what keeps such a row droppable but never signalable (`bgroups.unverifiable`).
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
    the kernel. The row stays, marked `leader_exited` — the shape bgroups._recycled is
    written to accept, since a pid cannot be recycled while it is still a live group's id,
    so an absent leader is never the identity-mismatch case that suppresses a reap. Losing
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


async def _pump(stream, buf: list[bytes]) -> None:
    while chunk := await stream.read(65536):
        buf.append(chunk)


class BashHandle:
    """A backgrounded subprocess. Output accumulates in memory as it streams;
    output() returns stdout captured so far (before or after completion).
    Its stdin is /dev/null (the kernel spawns background children detached,
    stdin=DEVNULL) — a command that tries to read stdin gets immediate EOF
    rather than blocking."""

    def __init__(self, proc, cmd: str):
        self._proc = proc
        self._cmd = cmd
        self._out: list[bytes] = []
        self._err: list[bytes] = []
        self._pump = asyncio.gather(_pump(proc.stdout, self._out),
                                     _pump(proc.stderr, self._err))
        # A background group survives the kernel unless somebody records it: it is its own
        # session, so no group kill on the kernel reaches it.
        self._pgid = _register(proc, cmd)
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

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> int | None:
        return self._proc.returncode

    def output(self) -> str:
        return b"".join(self._out).decode(errors="replace")

    async def wait(self) -> BashResult:
        await self._proc.wait()
        await self._pump
        _retire(self._pgid)         # the watcher would too; this makes it observable to
                                    # whoever awaited, without a scheduling race
        return BashResult(self._proc.returncode, self.output(),
                           b"".join(self._err).decode(errors="replace"), False)

    def kill(self) -> None:
        """SIGKILL the RECORDED group, and retire its row only once that group is empty.

        Recomputing the group from the leader's pid is wrong for exactly the case `_retire`
        exists for. A daemonizing command's shell has already exited leaving descendants in
        the group, so `os.getpgid(<reaped pid>)` raises, the `proc.kill()` fallback reaches
        nobody — and the unconditional unregister then threw away the one pgid anything
        could still have reaped those descendants by. `h.kill()` in that state leaked the
        child permanently.
        """
        if self._pgid is None:              # never registered: the leader was already gone
            _killpg_or_kill(self._proc)
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
        else:
            _unregister(pgid)
