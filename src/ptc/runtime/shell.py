"""async shell for the kernel. %%bash magic remains available alongside."""
import asyncio
import os
import signal
import time
from dataclasses import dataclass

from ptc import bgroups

from . import audit
from .state import STATE

#: Live background groups this kernel started, keyed by pgid. Mirrored to
#: `<kernel_dir>/bash-pgids.json` on every change so the host (kill/restart) and the
#: watchdog (TTL exit) can reap groups they never spawned — see ptc/bgroups.py.
_LIVE: dict[int, dict] = {}

#: Exit watchers, retained: a task nobody holds can be collected mid-await, and the entry
#: it was going to remove would then linger until the kernel dies.
_WATCHERS: set = set()


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
    _LIVE[pgid] = {"pgid": pgid, "pid": proc.pid, "cmd": cmd[:200], "started_at": time.time()}
    _persist()
    return pgid


def _unregister(pgid: int | None) -> None:
    if pgid is not None and _LIVE.pop(pgid, None) is not None:
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
        """Drop the registry entry the moment the group ends, so a reap never signals a
        pgid the OS has since handed to somebody else."""
        try:
            await self._proc.wait()
        finally:
            _unregister(self._pgid)

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
        _unregister(self._pgid)     # the watcher would too; this makes it observable to
                                    # whoever awaited, without a scheduling race
        return BashResult(self._proc.returncode, self.output(),
                           b"".join(self._err).decode(errors="replace"), False)

    def kill(self) -> None:
        _killpg_or_kill(self._proc)
        _unregister(self._pgid)


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
