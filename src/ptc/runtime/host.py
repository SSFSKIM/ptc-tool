"""The agent host: one long-lived headless `claude` per kernel that runs `llm()` calls and
lean subagents inside it, so a fan-out costs coroutines in one process instead of a CLI
process per call (docs/doperpowers/specs/2026-10-20-agent-host-design.md).

The kernel serves the *channel* — HTTP/1.1 on a Unix socket, protocol 1 — and the host
plugin's mod (`host-plugin/hooks/mods/host.tsx`) long-polls it for jobs and posts one result
per job. This module owns the kernel's half: the server, the job table, the host
semaphore, the host's start, its *generations* and their retirement, the crash loop, and
teardown. Which calls take the host is `routes.py`'s.

Three rules carry the design, and every path below keeps them:

  * **One result ends a job.** A job's slot is released when its result arrives — even a
    `late` one, after its caller was settled — or when its generation retires. Never at
    the caller's timeout: abandoned agents still run, and releasing their slots would
    admit replacements past the cap ("Settlement is separate from capacity").
  * **Every transport failure ends in a bounded retirement.** Process exit, a lapsed
    lease (no poll for `LEASE_S`), a lost poll response (a stale `ack`), or a cap filled
    by abandoned jobs retires the generation and fails its in-flight jobs at once. Nothing
    is replayed: an agent may already have run tools.
  * **One deadline per call.** `submit` spends the caller's deadline on the start, the
    permit and the job, and raises `TimeoutError` when it passes, as `guarded` does.

Everything runs on the kernel's own event loop (`STATE.loop`, the loop cells run on), which
ipykernel keeps running between cells: the server, the lease and the watcher live on it.
"""
import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ptc.paths import private_open, secure_dir

from .agents import AgentFailed, AgentOpts, child_ptc_env
from .state import STATE

PROTOCOL = 1

#: The longest the kernel holds a poll, whatever `wait` asks: `$.http.fetch` aborts a
#: request at 30,000 ms (M1, Q7), and 25 s leaves 5 s of margin.
POLL_HOLD_S = 25.0
#: A poll response's encoded JSON stays at or under this many bytes, under the fetch's
#: 4,194,304-byte response cap (Q7). Encoded with `ensure_ascii`, so bytes == characters.
POLL_BUDGET = 4_000_000
#: A job whose own JSON would pass this moves its prompt and system to files.
JOB_SPILL = 3_500_000
#: `$.fs.read`'s cap, in UTF-8 bytes: a field larger than this cannot reach the mod at all.
FIELD_CAP = 4 * 1024 * 1024
#: A generation that sends no poll for this long after its `/hello` has lapsed.
LEASE_S = 60.0
#: This many failures (failed starts, retirements other than `abandoned`) within the
#: window switch `"auto"` to the CLI for the rest of the kernel's life.
CRASH_LIMIT = 3
CRASH_WINDOW_S = 300.0
#: M1's ramp held at every width up to 256: one host, 581 MB, p95 7.45 s.
DEFAULT_CAP = 256
DEFAULT_START_S = 30.0
#: The engine refuses (does not queue) a spawn past its own cap, so its cap sits above the
#: kernel's semaphore and only the semaphore ever binds.
ENGINE_HEADROOM = 8
#: Retirement causes that are not a host failing: a cap of abandoned jobs is the callers'
#: doing, and `closed` is the kernel's.
_NOT_CRASHES = ("abandoned", "closed")
_MAX_BODY = 8 * 1024 * 1024
_TEARDOWN_S = 3.0

#: Variables of the session the kernel inherited that must not reach the host, emptied
#: (the SDK merges `env` over `os.environ`, so a key can be overridden but not removed;
#: the engine reads every one of these as unset when empty). The first three force a model
#: on every subagent, a plugin's spawn included (M1: the session exports
#: `CLAUDE_CODE_SUBAGENT_MODEL=opus`); `CLAUDE_CODE_PLUGIN_DIRS` would load the session's
#: own mods into the host; the rest tie the host to the parent session's identity.
EMPTIED = (
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL", "CLAUDE_CODE_PLUGIN_DIRS",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID",
    "CLAUDE_MODEL", "CLAUDE_EFFORT",
)


class HostUnavailable(RuntimeError):
    """The host cannot take a call: it is off, it did not start, or it crash-looped. Under
    `via="auto"` the call goes to the CLI route instead; under `via="host"` it raises."""


def _warn(msg: str) -> None:
    """One line in the current cell's output (the kernel's stderr is tee'd into it)."""
    print(msg, file=sys.stderr, flush=True)


def _left(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


async def _until(deadline: float | None, make):
    """Await `make()` within the deadline; `TimeoutError` once it has passed. `make` is a
    factory so that nothing is started for a budget that is already spent."""
    left = _left(deadline)
    if left is None:
        return await make()
    if left <= 0:
        raise TimeoutError
    return await asyncio.wait_for(make(), left)


def _exit_cause(rc: int | None) -> str:
    if rc is None:
        return "its SDK stream ended while its process still ran"
    if rc < 0:
        try:
            return f"killed by signal {-rc} ({signal.Signals(-rc).name})"
        except ValueError:
            return f"killed by signal {-rc}"
    return f"exit code {rc}"


class _Job:
    __slots__ = ("abandoned", "delivered", "encoded", "files", "future", "gen", "id",
                 "released")

    def __init__(self, jid: str, gen: "_Generation", encoded: str, files: list, future):
        self.id = jid
        self.gen = gen
        self.encoded = encoded
        self.files = files
        self.future = future
        self.delivered = False      # carried by a poll response
        self.abandoned = False      # its caller was settled while it still ran
        self.released = False       # its slot went back


class _Generation:
    """One started host: its process, its socket, its jobs, and its poll state."""

    def __init__(self, n: int, published: Path, socket: str):
        loop = asyncio.get_running_loop()
        self.n = n
        self.published = published
        self.socket = socket
        self.hello = loop.create_future()
        self.jobs: dict[str, _Job] = {}
        self.queue: deque[str] = deque()
        self.cancels: list[str] = []
        self.seq = 0                 # the last non-empty response's seq
        self.last_poll = time.monotonic()
        self.retired: str | None = None
        self.proc = None
        self.server = None
        self.session_id: str | None = None
        self.cwd: str | None = None
        self.engine: str | None = None
        self.tasks: set = set()
        self._changed = loop.create_future()
        self._held = None            # the newest held poll's supersede future

    @property
    def ready(self) -> bool:
        h = self.hello
        return h.done() and not h.cancelled() and h.exception() is None

    def notify(self) -> None:
        """Wake every held poll: a job, a cancel or a retirement is there to be seen."""
        if not self._changed.done():
            self._changed.set_result(None)
        self._changed = asyncio.get_running_loop().create_future()


class AgentHost:
    def __init__(self, *, kernel_dir, cwd: str, plugin_dir, cap: int = DEFAULT_CAP,
                 start_s: float = DEFAULT_START_S, enabled: bool = True, launch=None,
                 lease_s: float = LEASE_S):
        if cap < 1:
            raise ValueError(f"PTC_HOST_MAX_CONCURRENCY must be >= 1, got {cap}")
        self._dir = Path(kernel_dir) / "host"
        self._in = self._dir / "in"
        self._out = self._dir / "out"
        self._cwd = cwd
        self._plugin_dir = Path(plugin_dir) if plugin_dir is not None else None
        self.cap = cap
        self._start_s = start_s
        self._off = not enabled
        self._launch = launch or _launch_sdk
        self._lease_s = lease_s
        self._sem = asyncio.Semaphore(cap)
        self._held = 0
        self._gen: _Generation | None = None
        self._last: _Generation | None = None
        self._starting: asyncio.Task | None = None
        self._n = 0
        self._failures: deque[float] = deque()
        self._looped: str | None = None
        self._closed = False
        self._causes: list[str] = []
        self._tasks: set = set()

    # ---- state ------------------------------------------------------------------------

    def enabled(self) -> bool:
        """Whether `"auto"` may send a call here: not off, not crash-looped, not closed,
        and a host plugin to load."""
        return self._why_off() is None

    def _why_off(self) -> str | None:
        if self._off:
            return "the agent host is off (PTC_HOST=off)"
        if self._closed:
            return "the agent host is closed: this kernel is shutting down"
        if self._plugin_dir is None:
            return ("the agent host's plugin cannot be found: PTC_LAUNCHER is not set, "
                    "as it is in every ptc kernel")
        if self._looped is not None:
            return (f"the agent host failed {CRASH_LIMIT} times within "
                    f"{CRASH_WINDOW_S / 60:.0f} minutes (last: {self._looped}) and is off "
                    "for the rest of this kernel's life; PTC_HOST=off silences it")
        return None

    @property
    def session_id(self) -> str | None:
        g = self._gen or self._last
        return g.session_id if g else None

    @property
    def host_cwd(self) -> str | None:
        g = self._gen or self._last
        return g.cwd if g else None

    @property
    def pid(self) -> int | None:
        g = self._gen
        return getattr(g.proc, "pid", None) if g else None

    def held_slots(self) -> int:
        return self._held

    def retired_causes(self) -> list[str]:
        return list(self._causes)

    # ---- start --------------------------------------------------------------------------

    async def ensure_started(self) -> None:
        """Return once a generation has said hello; raise `HostUnavailable` if none can.
        Callers that arrive during a start wait on that same start."""
        why = self._why_off()
        if why is not None:
            raise HostUnavailable(why)
        g = self._gen
        if g is not None and g.retired is None:
            return
        if self._starting is None or self._starting.done():
            self._starting = asyncio.ensure_future(self._start())
            # A start nobody is left awaiting still fails loudly enough (its warning);
            # retrieving here keeps "exception was never retrieved" out of the cell.
            self._starting.add_done_callback(
                lambda t: t.cancelled() or t.exception())
        await asyncio.shield(self._starting)

    async def _start(self) -> None:
        self._n += 1
        gen = None
        try:
            for d in (self._dir, self._in, self._out):
                secure_dir(d)
            gen = await self._serve(self._n)
            gen.proc = await self._launch(self._env(gen), self._cwd, self._plugin_dir)
            self._spawn(gen, self._watch(gen))
            try:
                await asyncio.wait_for(asyncio.shield(gen.hello), self._start_s)
            except TimeoutError:
                raise HostUnavailable(f"no hello within {self._start_s:g} s") from None
        except asyncio.CancelledError:
            if gen is not None:
                self._retire(gen, "closed")
            raise
        except BaseException as e:      # noqa: BLE001 — every start failure is the same fact
            cause = str(e) if isinstance(e, HostUnavailable) else f"{type(e).__name__}: {e}"
            if gen is not None:
                self._retire(gen, cause)
            _warn(f"ptc: the agent host did not start ({cause}); calls under via='auto' "
                  "run on the CLI route instead (PTC_HOST=off skips the host)")
            self._count_failure(cause)
            raise HostUnavailable(f"the agent host did not start: {cause}") from None
        gen.last_poll = time.monotonic()
        self._spawn(gen, self._lease(gen))
        self._gen = self._last = gen

    def _env(self, gen: _Generation) -> dict:
        """The host's environment over the kernel's: M1's launch recipe."""
        env = child_ptc_env(AgentOpts(cwd=self._cwd))
        env.update({
            "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1",
            # The short real path, not the published symlink: `socketPath` is a sun_path.
            "PTC_HOST_SOCKET": gen.socket,
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(self.cap + ENGINE_HEADROOM),
            # A lifetime cap on spawns; the kernel's environment may carry 300.
            "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": str(10**9),
            "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
            **{k: "" for k in EMPTIED},
        })
        return env

    def _spawn(self, gen: _Generation | None, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        bucket = gen.tasks if gen is not None else self._tasks
        bucket.add(t)
        t.add_done_callback(bucket.discard)
        return t

    async def _watch(self, gen: _Generation) -> None:
        """Host death is visible only through the SDK (M1, Q8): its stream ends."""
        try:
            cause = await gen.proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:          # noqa: BLE001
            cause = f"its process could not be watched: {e}"
        self._retire(gen, cause)

    async def _lease(self, gen: _Generation) -> None:
        step = min(5.0, self._lease_s / 4)
        while gen.retired is None:
            await asyncio.sleep(step)
            if time.monotonic() - gen.last_poll > self._lease_s:
                self._retire(gen, "lease")

    # ---- retirement -----------------------------------------------------------------------

    def _count_failure(self, cause: str) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > CRASH_WINDOW_S:
            self._failures.popleft()
        if len(self._failures) >= CRASH_LIMIT and self._looped is None:
            self._looped = cause
            _warn(f"ptc: the agent host failed {CRASH_LIMIT} times within "
                  f"{CRASH_WINDOW_S / 60:.0f} minutes (last: {cause}); calls under "
                  "via='auto' run on the CLI route for the rest of this kernel's life. "
                  "Set PTC_HOST=off to skip the host from the start.")

    def _retire(self, gen: _Generation, cause: str) -> None:
        """Fail every in-flight job of `gen` at once and take its process down. Idempotent:
        the first cause wins. Counts toward the crash loop when the generation had said
        hello (a start that failed is counted by `_start`) and the cause is a failure."""
        if gen.retired is not None:
            return
        gen.retired = cause
        if self._gen is gen:
            self._gen = None
        crash = gen.ready and cause not in _NOT_CRASHES
        if not gen.hello.done():
            gen.hello.set_exception(HostUnavailable(cause))
            gen.hello.exception()
        err = f"agent host retired: {cause}"
        for j in list(gen.jobs.values()):
            if not j.future.done():
                j.future.set_exception(AgentFailed(err))
                j.future.exception()
            self._release(j)
        gen.queue.clear()
        gen.cancels.clear()
        gen.notify()
        self._causes.append(cause)
        if crash:
            self._count_failure(cause)
        self._spawn(None, self._teardown(gen))

    async def _teardown(self, gen: _Generation) -> None:
        for t in list(gen.tasks):
            if t is not asyncio.current_task():
                t.cancel()
        if gen.server is not None:
            gen.server.close()
        for p in (gen.published, Path(gen.socket)):
            try:
                p.unlink()
            except OSError:
                pass
        if gen.proc is not None:
            try:
                await asyncio.wait_for(gen.proc.close(), _TEARDOWN_S + 2)
            except BaseException:       # noqa: BLE001 — teardown never raises
                pass

    async def close(self) -> None:
        """Kernel exit, idle reap and restart: fail what is in flight, stop the host."""
        self._closed = True
        if self._starting is not None and not self._starting.done():
            self._starting.cancel()
            await asyncio.gather(self._starting, return_exceptions=True)
        if self._gen is not None:
            self._retire(self._gen, "closed")
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=_TEARDOWN_S + 3)

    # ---- jobs ---------------------------------------------------------------------------

    async def submit(self, job: dict, deadline: float | None) -> dict:
        """Run one job on the host and return its Result (`ok: true`).

        `deadline` is in `time.monotonic()`, `None` for none; it covers the start, the
        permit and the job, and passing it raises `TimeoutError` (the caller is settled at
        once; the job is cancelled in the host and keeps its slot until it ends there). A
        result that says it failed raises `AgentFailed`, as does the generation retiring
        under the job. A field the mod could not read is refused with `ValueError`.
        """
        job = dict(job)
        jid = f"j-{uuid.uuid4().hex}"
        job["id"] = jid
        _check_fields(job)
        while True:
            await _until(deadline, self.ensure_started)
            await _until(deadline, self._sem.acquire)
            gen = self._gen
            if gen is not None and gen.retired is None:
                break
            # The generation retired while this call waited for a slot: the job never
            # reached it, so it is simply the next call, and starts the next generation.
            self._sem.release()
        left = _left(deadline)
        try:
            if left is not None and left <= 0:
                raise TimeoutError
            job["timeoutMs"] = None if left is None else max(1, int(left * 1000))
            encoded, files = self._encode(job)
        except BaseException:
            self._sem.release()
            raise
        j = _Job(jid, gen, encoded, files, asyncio.get_running_loop().create_future())
        self._held += 1
        gen.jobs[jid] = j
        gen.queue.append(jid)
        gen.notify()
        try:
            res = await _until(deadline, lambda: asyncio.shield(j.future))
        except TimeoutError:
            if j.future.done() and not j.future.cancelled() and j.future.exception() is None:
                return _outcome(j.future.result())    # it landed as the deadline passed
            self._abandon(j)
            raise
        except asyncio.CancelledError:
            self._abandon(j)
            raise
        return _outcome(res)

    def _encode(self, job: dict) -> tuple[str, list[Path]]:
        """The job's JSON as it rides a poll response, spilling its prompt and system to
        files when that JSON would pass `JOB_SPILL`. Sized encoded, never raw: JSON
        escaping more than doubles a newline-heavy text."""
        job.setdefault("promptFile", None)
        if "system" in job:
            job.setdefault("systemFile", None)
        enc = json.dumps(job)
        if len(enc) <= JOB_SPILL:
            return enc, []
        files = []
        for field in ("prompt", "system"):
            text = job.get(field)
            if isinstance(text, str):
                p = self._in / f"{job['id']}.{field}.txt"
                with private_open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                files.append(p)
                job[field] = None
                job[f"{field}File"] = str(p)
        enc = json.dumps(job)
        if len(enc) > JOB_SPILL:
            _unlink(files)
            raise ValueError(f"this host job is {len(enc):,} characters of JSON even with "
                             "its prompt in a file (its agent type's system prompt is the "
                             "rest); run it with via='cli'")
        return enc, files

    def _release(self, j: _Job) -> None:
        """Give a job's slot back. Idempotent: its result and its generation's retirement
        may both reach here, and only the first counts."""
        if j.released:
            return
        j.released = True
        j.gen.jobs.pop(j.id, None)
        _unlink(j.files)
        self._held -= 1
        self._sem.release()

    def _abandon(self, j: _Job) -> None:
        """The caller was settled (deadline, cancel) while the job had not ended."""
        if j.future.done():
            return
        j.future.cancel()
        if j.released:
            return
        gen = j.gen
        if not j.delivered:
            # It never reached the host, so nothing runs there and no result will come.
            try:
                gen.queue.remove(j.id)
            except ValueError:
                pass
            self._release(j)
            return
        j.abandoned = True
        gen.cancels.append(j.id)
        gen.notify()
        if sum(1 for x in gen.jobs.values() if x.abandoned) >= self.cap:
            self._retire(gen, "abandoned")

    # ---- the channel ------------------------------------------------------------------------

    async def _serve(self, n: int) -> _Generation:
        """Bind generation `n`'s socket. The real socket sits at a short path in the temp
        dir and `<kernel_dir>/host/g<n>.sock` links to it (`runtime/peek.py`'s answer to the
        104-byte sun_path cap); a fresh path per generation means a retired generation's
        mod can reach nothing of the next."""
        published = self._dir / f"g{n}.sock"
        real = Path(tempfile.gettempdir()) / (
            "ptc-host-" + hashlib.sha256(str(published).encode()).hexdigest()[:12] + ".sock")
        for p in (real, published):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        gen = _Generation(n, published, str(real))
        gen.server = await asyncio.start_unix_server(
            lambda r, w: self._connection(gen, r, w), path=str(real))
        os.chmod(real, 0o600)
        published.symlink_to(real)
        return gen

    async def _connection(self, gen, reader, writer) -> None:
        try:
            while True:
                req = await _read_request(reader)
                if req is None:
                    break
                method, target, headers, body = req
                status, payload = await self._route(gen, method, target, body)
                data = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
                keep = headers.get("connection", "").lower() != "close"
                writer.write(
                    f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                    f"Connection: {'keep-alive' if keep else 'close'}\r\n\r\n".encode() + data)
                await writer.drain()
                if not keep:
                    break
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    async def _route(self, gen: _Generation, method: str, target: str, body: bytes):
        if gen.retired is not None:
            return 410, {"ok": False, "reason": "retired"}
        url = urlsplit(target)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if method == "POST" and url.path == "/hello":
            return self._on_hello(gen, body)
        if method == "GET" and url.path == "/jobs":
            return await self._on_poll(gen, _int(q.get("wait")), _int(q.get("ack")))
        if method == "POST" and url.path.startswith("/results/"):
            return self._on_result(gen, unquote(url.path[len("/results/"):]), body)
        return 404, {"ok": False, "reason": "no such route"}

    def _on_hello(self, gen: _Generation, body: bytes):
        try:
            hello = json.loads(body)
            protocol = hello.get("protocol")
        except (ValueError, AttributeError):
            return 400, {"ok": False, "reason": "malformed hello"}
        if protocol != PROTOCOL:
            if not gen.hello.done():
                gen.hello.set_exception(HostUnavailable(
                    f"protocol mismatch: the host plugin speaks protocol {protocol!r}, "
                    f"this kernel {PROTOCOL}"))
            return 409, {"ok": False, "reason": "protocol"}
        gen.engine = hello.get("engine")
        gen.session_id = hello.get("session")
        gen.cwd = hello.get("cwd")
        gen.last_poll = time.monotonic()
        if not gen.hello.done():
            gen.hello.set_result(hello)
        return 200, {"ok": True, "outDir": str(self._out)}

    async def _on_poll(self, gen: _Generation, wait_ms: int, ack: int):
        gen.last_poll = time.monotonic()
        if ack < gen.seq:
            # The response numbered `gen.seq` never reached the mod. Its jobs may or may
            # not run, and redelivering them would need deduplication in the mod to rule
            # out running an agent twice: retire instead.
            self._retire(gen, "delivery")
            return 410, {"ok": False, "reason": "retired"}
        if gen._held is not None and not gen._held.done():
            gen._held.set_result(None)            # a newer poll: the old one is dead
        mine = asyncio.get_running_loop().create_future()
        gen._held = mine
        end = gen.last_poll + min(max(wait_ms, 0) / 1000, POLL_HOLD_S)
        while not (gen.queue or gen.cancels) and gen.retired is None and not mine.done():
            left = end - time.monotonic()
            if left <= 0:
                break
            await asyncio.wait([gen._changed, mine], timeout=left,
                               return_when=asyncio.FIRST_COMPLETED)
        if gen.retired is not None:
            return 410, {"ok": False, "reason": "retired"}
        if mine.done():                           # superseded: nobody reads this one
            return 200, {"seq": gen.seq, "jobs": [], "cancel": []}
        gen._held = None
        return 200, self._response(gen)

    def _response(self, gen: _Generation) -> str:
        """Every queued cancel and as many queued jobs as fit `POLL_BUDGET`, as the exact
        JSON text sent (assembled from each job's cached encoding, so its size is known)."""
        cancels, gen.cancels = gen.cancels, []
        tail = json.dumps(cancels)
        size = len('{"seq": , "jobs": [], "cancel": }') + 20 + len(tail)
        parts: list[str] = []
        while gen.queue:
            j = gen.jobs.get(gen.queue[0])
            if j is None or j.released or j.delivered:
                gen.queue.popleft()
                continue
            add = len(j.encoded) + (2 if parts else 0)
            if parts and size + add > POLL_BUDGET:
                break
            gen.queue.popleft()
            parts.append(j.encoded)
            j.delivered = True
            size += add
        if parts or cancels:
            gen.seq += 1
        return f'{{"seq": {gen.seq}, "jobs": [{", ".join(parts)}], "cancel": {tail}}}'

    def _on_result(self, gen: _Generation, jid: str, body: bytes):
        j = gen.jobs.get(jid)
        if j is None or j.released:
            return 404, {"ok": False, "reason": "unknown job"}
        try:
            res = json.loads(body)
            if not isinstance(res, dict):
                raise ValueError("not an object")
            if res.get("textFile"):
                expected = self._out / f"{jid}.txt"
                try:
                    if Path(res["textFile"]) != expected:
                        raise ValueError(f"textFile {res['textFile']!r} is not {expected}")
                    res["text"] = expected.read_text(encoding="utf-8")
                    res["textFile"] = None
                finally:
                    _unlink([expected])
        except (ValueError, OSError, TypeError) as e:
            res = {"ok": False, "reason": "error",
                   "detail": f"the host's result could not be read: {e}"}
        late = j.future.done()
        if not late:
            j.future.set_result(res)
        self._release(j)
        return 200, ({"ok": True, "late": True} if late else {"ok": True})


def _outcome(res: dict) -> dict:
    if res.get("ok"):
        return res
    reason = res.get("reason") or "error"
    detail = res.get("detail") or ""
    if reason == "timeout":
        raise TimeoutError(f"the host job reached its deadline: {detail}".rstrip(": "))
    status = res.get("status")
    head = f"{reason}, status {status}" if status is not None else reason
    raise AgentFailed(f"agent host job failed ({head})" + (f": {detail}" if detail else ""))


def _check_fields(job: dict) -> None:
    for name, text in (("prompt", job.get("prompt")), ("system", job.get("system")),
                       ("agent type's system prompt", (job.get("type") or {}).get("prompt"))):
        if isinstance(text, str) and len(text) > FIELD_CAP // 4:
            n = len(text.encode("utf-8"))
            if n > FIELD_CAP:
                raise ValueError(f"this call's {name} is {n:,} bytes in UTF-8, over the "
                                 "agent host's 4 MiB read cap; run it with via='cli'")


def _unlink(paths) -> None:
    for p in paths:
        try:
            Path(p).unlink()
        except OSError:
            pass


def _int(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 409: "Conflict", 410: "Gone",
            413: "Payload Too Large"}


async def _read_request(reader: asyncio.StreamReader):
    """One HTTP/1.1 request: (method, target, headers, body), or None at EOF."""
    line = await reader.readline()
    if not line:
        return None
    method, target, _ = line.decode("latin-1").split(" ", 2)
    headers: dict[str, str] = {}
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks, total = [], 0
        while True:
            n = int((await reader.readline()).strip().split(b";")[0], 16)
            if n == 0:
                await reader.readline()
                break
            total += n
            if total > _MAX_BODY:
                raise ValueError("request body too large")
            chunks.append(await reader.readexactly(n))
            await reader.readline()
        body = b"".join(chunks)
    else:
        n = int(headers.get("content-length", "0"))
        if n > _MAX_BODY:
            raise ValueError("request body too large")
        body = await reader.readexactly(n)
    return method, target, headers, body


# ---- the real launch ----------------------------------------------------------------------


class _SdkHost:
    """A host started through `ClaudeSDKClient`: the SDK's bundled CLI, the version the CLI
    route runs. The SDK keeps the CLI's process on its transport and exposes no pid or
    exit status, so both are read from there."""

    def __init__(self, client):
        self._client = client
        self._process = client._transport._process
        self.pid = self._process.pid

    async def wait(self) -> str:
        try:
            async for _ in self._client.receive_messages():
                pass
        except Exception:                # noqa: BLE001 — the exit is the fact, not its form
            pass
        try:
            rc = await asyncio.wait_for(self._process.wait(), 5)
        except TimeoutError:
            rc = None
        return _exit_cause(rc)

    async def close(self) -> None:
        if self._process.returncode is None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            await asyncio.wait_for(self._client.disconnect(), _TEARDOWN_S)
        except BaseException:            # noqa: BLE001 — teardown never raises
            pass
        if self._process.returncode is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass


async def _launch_sdk(env: dict, cwd: str, plugin_dir: Path) -> _SdkHost:
    """M1's launch recipe: `connect()` with no first query, no user settings, no MCP
    servers, the main loop's default tools (a spawn goes through its `Agent` tool and a
    cancel through `TaskStop`, and only with the default set was a type's tool list seen
    to hold exactly), the host plugin through `plugins` (`--plugin-dir`)."""
    if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        raise HostUnavailable(f"no host plugin at {plugin_dir}")
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    client = ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd=cwd, setting_sources=[], permission_mode="bypassPermissions", env=env,
        plugins=[{"type": "local", "path": str(plugin_dir)}]))
    try:
        await client.connect()
        return _SdkHost(client)
    except BaseException:
        try:
            await asyncio.wait_for(client.disconnect(), _TEARDOWN_S)
        except BaseException:            # noqa: BLE001 — teardown never masks the fault
            pass
        raise


# ---- the kernel's one host ------------------------------------------------------------------

_HOST: AgentHost | None = None


def _number(name: str, cast, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def get_host() -> AgentHost:
    """The kernel's one host, configured from its environment on first use:
    `PTC_HOST` (`auto` | `off`), `PTC_HOST_MAX_CONCURRENCY` (256), `PTC_HOST_START_S` (30).
    The plugin is `dirname(dirname($PTC_LAUNCHER))/host-plugin`, beside the ptc plugin's
    own `bin/`."""
    global _HOST
    if _HOST is None:
        launcher = os.environ.get("PTC_LAUNCHER")
        _HOST = AgentHost(
            kernel_dir=STATE.kernel_dir,
            cwd=STATE.config.get("cwd") or os.getcwd(),
            plugin_dir=(Path(os.path.dirname(os.path.dirname(launcher))) / "host-plugin"
                        if launcher else None),
            cap=_number("PTC_HOST_MAX_CONCURRENCY", int, DEFAULT_CAP),
            start_s=_number("PTC_HOST_START_S", float, DEFAULT_START_S),
            enabled=os.environ.get("PTC_HOST", "auto").strip().lower()
            not in ("off", "0", "false", "no"))
    return _HOST


def current_host() -> AgentHost | None:
    """The host if one was ever asked for — teardown closes it without creating one."""
    return _HOST


def _reset_host() -> None:
    """Test-only: the next `get_host()` builds a fresh host from the environment."""
    global _HOST
    _HOST = None
