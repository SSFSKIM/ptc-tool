"""async shell for the kernel. %%bash magic remains available alongside."""
import asyncio
import os
import signal
from dataclasses import dataclass

from . import audit


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
        return BashResult(self._proc.returncode, self.output(),
                           b"".join(self._err).decode(errors="replace"), False)

    def kill(self) -> None:
        _killpg_or_kill(self._proc)


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
