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


class BashHandle:
    """A backgrounded subprocess. Output accumulates in memory as it streams;
    output() returns stdout captured so far (before or after completion)."""

    def __init__(self, proc, cmd: str):
        self._proc = proc
        self._cmd = cmd
        self._out: list[bytes] = []
        self._err: list[bytes] = []
        self._pump = asyncio.ensure_future(self._drain())

    @property
    def pid(self) -> int:
        return self._proc.pid

    async def _drain(self):
        async def pump(stream, buf):
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                buf.append(chunk)
        await asyncio.gather(pump(self._proc.stdout, self._out),
                              pump(self._proc.stderr, self._err))

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
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass


async def bash(cmd: str, timeout: float = 120.0, cwd=None, env=None,
                background: bool = False):
    audit.append("bash", command=cmd[:200])
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    if background:
        return BashHandle(proc, cmd)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return BashResult(proc.returncode, out.decode(errors="replace"),
                           err.decode(errors="replace"), False)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        out, err = await proc.communicate()
        return BashResult(None, out.decode(errors="replace"),
                           err.decode(errors="replace"), True)
