"""The agent namespace: run/spawn/fork/gather/send over pluggable backends.

Lifecycle contract every path here honors (plan review F6):
  * the driver task is RETAINED on the handle — a fire-and-forget task can be garbage
    collected mid-flight, taking its CLI subprocess with it in silence;
  * every call that reaches a backend runs under the shared semaphore, and a caller's
    `timeout` is a wall-clock deadline covering the queue wait too, not just the call;
  * on timeout / cancel: cancel the driver, tear the session down within a SEPARATE
    teardown budget, and settle the handle exactly once. On INTERRUPT the order inverts —
    S1 proved an interrupted turn ends normally, so the drain is awaited, not pre-empted;
  * no path leaves a permit held or a CLI process tree alive.
"""
import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass

from ptc.paths import KEY_MAX, private_write_text, secure_dir

from . import audit
from .state import STATE

#: Teardown gets its own budget, separate from any call deadline. S1 measured a cancelled
#: SDK task taking 0.6-5.7 s to unwind and an interrupt()ed turn 4.0-13.0 s to drain, so
#: "await teardown" must be bounded but generous — never assumed instant.
_TEARDOWN_S = 30.0

#: How long an interrupted turn is given to end the way S1 says it ends: NORMALLY, with a
#: ResultMessage carrying terminal_reason='aborted_streaming'. Drain length is workload-shaped:
#: 4.0-13.0 s in S1's longer turns, and 0.6 s observed live in T28 on a short turn. Within
#: this budget the drain settles the handle through its own completion path; past it the
#: drain is treated as wedged and the driver is cancelled instead.
_INTERRUPT_DRAIN_S = 30.0

_TERMINAL = ("done", "error", "interrupted")

#: Spec: ONE semaphore across agent/llm/web_search, not one pool per caller (two 8-permit
#: pools would let 16 SDK-spawning calls run at once against an 8 cap). Module-level and
#: lazily created from STATE.config so every caller — `_Agent`, `llm()`, and `web.py` —
#: shares the same bound without constructing an `_Agent` just to reach it.
_SHARED_SEM: asyncio.Semaphore | None = None


def shared_semaphore(config: dict | None = None) -> asyncio.Semaphore:
    """The one semaphore shared by every SDK-spawning caller. `config` sizes it on first
    creation (defaults to `STATE.config` for callers with no config of their own — `llm()`,
    `web.py`); `_Agent` passes its own `self._config` explicitly (see `_Agent._semaphore`)
    so it reads its own config rather than reaching past it into global state."""
    global _SHARED_SEM
    if _SHARED_SEM is None:
        cfg = STATE.config if config is None else config
        _SHARED_SEM = asyncio.Semaphore(int(cfg.get("max_concurrency", 8)))
    return _SHARED_SEM


def _reset_semaphore() -> None:
    """Test-only: a semaphore is bound to the `max_concurrency` in effect when it was
    first created, so a test that changes that value needs a fresh one. Production never
    calls this — `max_concurrency` does not change mid-process."""
    global _SHARED_SEM
    _SHARED_SEM = None


async def guarded(sem: asyncio.Semaphore, make_coro, timeout: float | None):
    """One permit, one deadline, released on every exit path. The deadline covers the
    wait for the permit as well, so a queued call cannot outlive its own timeout. Shared
    by every semaphore consumer (`_Agent._guarded`, `llm()`, and `web_search()`) so this
    contract lives in exactly one place.

    Only `None` means "no deadline". `timeout=0` is a deadline that has already passed —
    the call does not get to wait for a permit or a backend — and a negative one is a
    caller error said out loud. A truthiness test read all three as "no deadline", so the
    caller asking for the strictest bound got none at all and a hung backend could run
    forever holding its permit.
    """
    async def _work():
        async with sem:
            return await make_coro()
    if timeout is None:
        return await _work()
    if timeout < 0:
        raise ValueError(
            f"timeout must be a non-negative number of seconds or None, got {timeout!r}: "
            "a deadline in the past is not the same as no deadline")
    return await asyncio.wait_for(_work(), timeout)


@dataclass
class AgentOpts:
    provider: str = "claude"
    model: str | None = None
    system: str | None = None
    allowed_tools: list | None = None
    permission_mode: str = "bypassPermissions"
    cwd: str | None = None
    max_turns: int | None = None
    effort: str | None = None
    output_schema: dict | None = None
    timeout: float | None = None


@dataclass
class AgentResult:
    text: str
    session_id: str | None
    structured: dict | None
    cost_usd: float | None
    num_turns: int | None
    duration_ms: int


class AgentFailed(RuntimeError):
    """A turn whose own terminal result said it FAILED — a rate limit, a max-turns stop,
    an execution error inside the CLI.

    The CLI reports these in band: the stream ends with an ordinary terminal message
    carrying an error flag and, usually, the error text where a normal turn's answer would
    be. Read as a normal end, that text becomes the answer — `llm()` returned "Claude AI
    usage limit reached" as a classification and a spawned handle went `done` — so the
    failure has to leave the backend as an exception instead.

    The partial `result` rides along where a turn produced one: whatever text arrived
    before the failure is still the caller's to look at, it just is not an answer.
    """

    def __init__(self, message: str, result: AgentResult | None = None):
        super().__init__(message)
        self.result = result


#: The bound safe_key() enforces on every session key. A child key is built to fit INSIDE
#: it — see child_key() — so it is taken from paths rather than restated here: a key that
#: overshoots comes back digested, and a child that needed the digest would no longer
#: carry its own readable parent prefix.
_KEY_MAX = KEY_MAX


def child_key(parent_key: str) -> str:
    """A child session key that is always distinct from its parent's.

    safe_key() truncates to 128 characters, so appending a suffix to a parent key already
    at that bound truncated the suffix straight back off: the child resolved to the
    PARENT's key, attached to the parent's kernel and inherited its depth-0 bootstrap,
    losing session isolation and the recursion brake in one step. The parent portion is
    hash-shortened when it has to be, so the suffix always survives, and the shortening is
    deterministic — the same parent always contributes the same prefix.

    Twelve hex characters, not six. Six is 24 bits, which collides somewhere in a fleet of
    a thousand concurrent children about 2.9% of the time — and the collision is silent
    and total: two unrelated children resolve to ONE key, attach to one kernel and share a
    Python namespace. 48 bits takes the same fleet to ~1.8e-9, and the parent portion still
    fits (a shortened parent needs `_KEY_MAX - len(suffix)` characters, digest included).
    """
    suffix = f"-a{uuid.uuid4().hex[:12]}"
    room = _KEY_MAX - len(suffix)
    if len(parent_key) > room:
        digest = hashlib.sha256(parent_key.encode()).hexdigest()[:12]
        parent_key = f"{parent_key[:room - len(digest) - 1]}-{digest}"
    return parent_key + suffix


def child_ptc_env(o: AgentOpts) -> dict:
    """PTC's own variables for a child agent process — the same four for every backend.

    Two rules here are load-bearing and must not drift between backends: PTC_SESSION is
    always overridden to a fresh child key (a child inheriting the parent's key would
    attach to the parent's kernel), and PTC_DEPTH is the parent's depth + 1, which is what
    makes the depth brake bite one level down.
    """
    parent_key = STATE.config.get("key") or "root"
    return {
        "PTC_SESSION": child_key(parent_key),
        "PTC_DEPTH": str(int(STATE.config.get("depth", 0)) + 1),
        "PTC_MAX_DEPTH": str(STATE.config.get("max_depth", 1)),
        "PTC_CWD": o.cwd or STATE.config.get("cwd") or os.getcwd(),
    }


#: Teardown steps still draining past their budget. Retained so a step that outlives the
#: caller's patience cannot be garbage collected half-done (F6).
_DRAINING: set = set()


async def _swallow(aw) -> None:
    try:
        await aw
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown never raises
        pass


async def _bounded(aw, budget: float | None = None) -> None:
    """Await one teardown step within the teardown budget; never raise out of teardown.

    Past the budget we stop WAITING but let the step finish in the background: cancelling
    a disconnect half-way is how a CLI process tree survives its client.
    """
    task = asyncio.ensure_future(_swallow(aw))
    _DRAINING.add(task)
    task.add_done_callback(_DRAINING.discard)
    try:
        # _swallow means the task itself never raises, so the budget is the only way out
        # besides success. A CancelledError from OUR caller is deliberately NOT caught:
        # swallowing it would make a wedged teardown look like a clean one to whoever is
        # bounding us from outside.
        await asyncio.wait_for(asyncio.shield(task), budget or _TEARDOWN_S)
    except TimeoutError:
        pass


class AgentHandle:
    def __init__(self, owner: "_Agent", name: str, provider: str, timeout: float | None):
        self._owner = owner
        self.name = name
        self.provider = provider
        self._timeout = timeout
        self._session = None
        self._driver: asyncio.Task | None = None    # retained — never discarded (F6)
        self._status = "running"
        #: interrupt() is in charge from here on: the driver must not claim the
        #: "interrupted" transition out from under it if the drain has to be cancelled.
        self._interrupting = False
        #: last session id the backend reported, kept across close() so a post-teardown
        #: registry row still names the session the caller can resume.
        self._session_id_seen: str | None = None
        #: the turn's messages, kept across close() for the same reason — see close().
        self._messages_seen: list = []
        #: One turn at a time on one CLI session: two follow-up sends interleaved on the
        #: same stream would each drain part of the other's messages.
        self._turn_lock = asyncio.Lock()
        self._result_fut: asyncio.Future = asyncio.get_running_loop().create_future()

    @property
    def session_id(self):
        return getattr(self._session, "session_id", None) or self._session_id_seen

    @property
    def status(self) -> str:
        return self._status

    async def result(self) -> AgentResult:
        # shield: a cancelled cell must not cancel the shared settlement future.
        return await asyncio.shield(self._result_fut)

    async def send(self, msg: str) -> AgentResult:
        """A follow-up turn, run under the SAME running/interrupt lifecycle as the first.

        The turn is retained as this handle's driver and the status flips to 'running' for
        its duration, so a concurrent interrupt() takes the live branch: it signals the
        backend, waits the drain out, and the send returns the interrupted partial (S1: an
        aborted turn ends NORMALLY). Left at 'done' the handle looked terminal — interrupt()
        closed the session without ever signalling it, and the send settled as an error.
        Settlement stays exactly-once: `_result_fut` belongs to the first turn and is
        already done, so a send never re-settles it.
        """
        async with self._turn_lock:
            if self._status == "running":
                await self.result()
            # Bind the session BEFORE the lambda: _guarded evaluates it after the semaphore
            # wait, and a close()/interrupt() landing in that window would otherwise turn
            # the intended refusal into an AttributeError on None (M2).
            session = self._session
            if session is None:
                raise RuntimeError(f"agent {self.name!r} has no live session to send to")
            self._status = "running"
            self._owner._registry_update(self, "running")
            turn = asyncio.ensure_future(
                self._owner._guarded(lambda: session.send(msg), self._timeout))
            self._driver = turn      # retained AND reachable by interrupt() (F6)
            try:
                r = await turn
            except asyncio.CancelledError:
                await self._poison("interrupted")
                raise
            except BaseException:                 # timeout, backend error, anything
                await self._poison("error")
                raise
            # An interrupt that landed mid-drain owns the terminal transition; this turn
            # returning its partial does not undo it.
            if not self._interrupting and self._status not in _TERMINAL:
                self._status = "done"
                self._owner._registry_update(self, self._status)
            return r

    async def _poison(self, status: str) -> None:
        """A send() that did not return leaves the session unusable, whatever the reason.

        Releasing the permit is not enough: the child's turn may still be running and its
        stream is half-drained, so a later send() on the same handle would talk over a
        live turn on a process this handle no longer understands. Mark the handle terminal
        and close the session — the next send() then refuses with "no live session" (F5).
        The status is set unconditionally: the handle this bites is normally 'done' (a
        finished turn kept open as a send target), which a terminal-status guard would
        preserve as if nothing had gone wrong.
        """
        self._status = status
        self._owner._registry_update(self, status)
        await self.close()

    def messages(self) -> list:
        return self._session.messages() if self._session else list(self._messages_seen)

    def history(self):
        """The child's own transcript (T25's `Transcript`, same type `history()` returns).

        Claude handles only — see the provider check below for why a codex handle is
        refused rather than answered.

        The child writes its own JSONL under a cwd this handle never tracked (only
        `AgentOpts.cwd`, which is not retained past spawn), so nothing is passed as
        `cwd=` here. That does NOT mean resolution is by session id alone: `history()`
        still reads the KERNEL's `meta.json` first and tries the direct path under that
        cwd, which hits whenever the child ran in the kernel's own directory (the
        default). `transcript._resolve_path`'s glob is the fallback that covers a child
        given a different `cwd=` at spawn.
        """
        if self.provider != "claude":
            # A codex handle's session_id is a THREAD id whose turns live in a codex
            # rollout ($CODEX_HOME/sessions/**.jsonl), a different store in a different
            # format. Searching Claude's JSONL for it either fails or, worse, glob-matches
            # an unrelated transcript. v1 reads the Claude store only; say so rather than
            # answer with somebody else's conversation.
            raise NotImplementedError(
                f"history() reads Claude session transcripts; agent {self.name!r} runs on "
                f"provider {self.provider!r}, whose turns are recorded as a codex rollout "
                "under $CODEX_HOME/sessions — a store this build does not read. Use "
                "messages() for the turns this handle saw.")
        if not self.session_id:
            raise RuntimeError(f"agent {self.name!r} has no session id yet")
        from .transcript import history as _history
        return _history(session=self.session_id)

    async def interrupt(self) -> None:
        """Interrupt the turn, let its drain END NORMALLY, settle exactly once (F6).

        Order follows the S1 contract: an interrupted turn does NOT raise — it ends with a
        normal ResultMessage carrying terminal_reason='aborted_streaming' after a 4-13 s
        drain. So the session is interrupted FIRST and the driver is left running to
        collect that result through its ordinary completion path; the caller then gets the
        partial turn back from result() instead of an exception. Only if the drain
        outlives _INTERRUPT_DRAIN_S is it treated as wedged and the driver cancelled — the
        fallback, not the norm. Either way the handle settles once and ends 'interrupted'.

        A handle with NO session is the other shape: still queued on the semaphore, or
        inside open_session/connect. Nothing was ever asked to abort, so there is no drain
        to await — the driver is pre-empted at once, or the queued turn would later take
        its permit and run a whole (billed) turn to completion under an 'interrupted' label.
        """
        if self._status in _TERMINAL:
            # Already settled (it may have finished on its own): keep the terminal status
            # and the terminal registry row, but still release the CLI.
            await self.close()
            return
        if self._interrupting:
            return          # a first interrupt owns the drain; do not re-signal under it
        self._interrupting = True
        try:
            driver = self._driver
            session = self._session
            if session is not None:
                await _bounded(session.interrupt())
                if driver is not None and not driver.done():
                    await _bounded(asyncio.gather(driver, return_exceptions=True),
                                   _INTERRUPT_DRAIN_S)
            if driver is not None and not driver.done():
                # Either there was no session to signal (nothing is draining) or the drain
                # outlived its budget (it is wedged). Both mean: stop waiting for a normal
                # end and take the turn down the hard way.
                driver.cancel()
                await _bounded(asyncio.gather(driver, return_exceptions=True))
            # The drain may have settled the handle with its aborted result; if it did not
            # (cancelled, or no driver at all) the interrupt itself is the terminal event.
            self._status = "interrupted"
            if not self._result_fut.done():
                self._settle_exception(RuntimeError(f"agent {self.name!r} was interrupted"))
            await self.close()
            self._owner._registry_update(self, "interrupted")
        except BaseException:
            # This interrupt never finished (the caller cancelled it, most likely). Clear
            # the latch so a retry re-enters: left set, every later interrupt() returns at
            # it instantly, stranding a live driver on a handle that never settles.
            self._interrupting = False
            raise

    async def close(self) -> None:
        """Release this handle's CLI, whichever side of open_session() it is on.

        With a session, closing it is the whole job. WITHOUT one the handle is not idle —
        it is a driver still queued on the semaphore, or inside open_session/connect, about
        to spawn a CLI and run a whole billed turn for a handle whose owner has already
        closed it. Nothing was ever asked to stop, so the driver is pre-empted at once;
        this is interrupt()'s no-session branch, reached from the other door.

        The driver's own cancel handler settles the handle (status, registry row and
        result), which keeps settlement exactly-once whichever of the two got there first;
        the belt below covers a cancel that outlives the teardown budget, where waiting
        longer would just hang the caller on an unsettled future.
        """
        session, self._session = self._session, None
        if session is not None:
            self._session_id_seen = getattr(session, "session_id", None) or self._session_id_seen
            # The session owns the accumulated messages and this assignment drops the last
            # reference to it, so a caller that closes a completed handle to reap its CLI
            # found messages() empty from then on. For a codex handle that is the only
            # transcript view there is — history() refuses that provider by design.
            self._messages_seen = list(session.messages())
            await _bounded(session.close())
            return
        driver = self._driver
        # `is not current_task()`: the driver calls close() itself on its own error path
        # (spawn's `except Exception`), and cancelling yourself there aborts the settlement
        # you are on your way to making.
        if driver is None or driver.done() or driver is asyncio.current_task():
            return
        driver.cancel()
        await _bounded(asyncio.gather(driver, return_exceptions=True))
        if self._status not in _TERMINAL:
            self._status = "interrupted"
            self._owner._registry_update(self, "interrupted")
        if not self._result_fut.done():
            self._settle_exception(
                RuntimeError(f"agent {self.name!r} was closed before its turn started"))

    # -- settlement ---------------------------------------------------------
    def _settle_result(self, r: AgentResult) -> None:
        if not self._result_fut.done():
            self._result_fut.set_result(r)

    def _settle_exception(self, exc: BaseException) -> None:
        if self._result_fut.done():
            return
        self._result_fut.set_exception(exc)
        # Mark retrieved: a handle nobody awaits would otherwise dump
        # "Future exception was never retrieved" into the cell log at GC time.
        self._result_fut.exception()


class _Agent:
    def __init__(self, config: dict, backends: dict):
        self._config = config
        self._backends = backends
        self._handles: dict[str, AgentHandle] = {}

    # -- plumbing -----------------------------------------------------------
    #: The pool itself lives at module level (`shared_semaphore`) — one pool across
    #: agent/llm/web_search, per spec. Reads self._config, not STATE.config directly: in
    #: production and every test today `_config is STATE.config` by construction (see
    #: `_make_agent`), so the pool is sized the same either way, but this keeps `_Agent`
    #: honoring its own constructor argument rather than silently reaching past it.
    def _semaphore(self) -> asyncio.Semaphore:
        return shared_semaphore(self._config)

    async def _guarded(self, make_coro, timeout: float | None):
        return await guarded(self._semaphore(), make_coro, timeout)

    def _check_depth(self) -> None:
        d, m = int(self._config.get("depth", 0)), int(self._config.get("max_depth", 1))
        if d >= m:
            raise RuntimeError(
                f"agent depth limit reached (PTC_DEPTH={d}, PTC_MAX_DEPTH={m}); "
                "raise PTC_MAX_DEPTH to allow grandchildren")

    def _opts(self, kw: dict) -> AgentOpts:
        provider = kw.pop("provider", "claude")
        if provider not in self._backends:
            raise ValueError(f"unknown provider {provider!r}; use one of {sorted(self._backends)}")
        bad = set(kw) - set(AgentOpts.__dataclass_fields__)
        if bad:
            raise TypeError(f"unsupported agent options: {sorted(bad)}")
        return AgentOpts(provider=provider, **kw)

    def _claim_name(self, name: str) -> None:
        """Refuse a handle name whose previous holder is still live.

        "done" is not free: a finished handle deliberately keeps its CLI session open as a
        send() target, so overwriting the entry and the registry row would strand that
        process with no owner and no cleanup path. Shared by spawn() and resume() — a
        resumed session is a live CLI exactly like a spawned one.
        """
        prior = self._handles.get(name)
        if prior is not None and (prior.status == "running" or prior._session is not None):
            raise ValueError(
                f"agent name already in use: {name!r} (status={prior.status}, its session "
                "is still live) — await that handle's close() before reusing the name")

    def _resumed_name(self, session_id: str) -> str:
        """`resumed-<first 8 of the session id>`, lengthened when that prefix is already
        taken by a DIFFERENT session.

        Two ids sharing eight characters are not the same session, and letting them share
        one handle name overwrote `_handles` and the registry row of a session whose CLI
        was still live — unlistable and unmanageable from then on. A name held by THIS
        session is the other case, a genuine double-resume, and `_claim_name` decides it.

        The registry is consulted alongside the live handles because it OUTLIVES them: a
        restart empties `_handles` while `agents.json` survives on purpose, and that is the
        whole point of the file — `agent.list()` after a restart is what makes an old
        session resumable at all. Keyed on live handles alone, the first resume after a
        restart reused a persisted row's name and `_registry_write` overwrote its
        `session_id`, deleting the only reference to the session it was kept for.
        """
        rows = {r.get("name"): r.get("session_id") for r in self._registry_load()
                if isinstance(r, dict)}
        for n in (8, 16, len(session_id)):
            name = f"resumed-{session_id[:n]}"
            prior = self._handles.get(name)
            if prior is not None:
                if prior.session_id == session_id:
                    return name
                continue
            if name not in rows or rows[name] == session_id:
                return name
        return f"resumed-{session_id}-{uuid.uuid4().hex[:6]}"

    def _registry_path(self):
        return STATE.kernel_dir / "agents.json"

    def _registry_load(self) -> list:
        try:
            rows = json.loads(self._registry_path().read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return rows if isinstance(rows, list) else []

    def _registry_write(self, name: str, provider: str, session_id, status: str,
                        task_head: str = "", **extra) -> None:
        """Upsert one row. Bookkeeping never breaks a run: an unwritable registry costs
        visibility (agent.list across restarts), not the result the caller is awaiting."""
        rows = self._registry_load()
        row = next((r for r in rows if r.get("name") == name), None)
        if row is None:
            row = {"name": name, "created_at": time.time()}
            rows.append(row)
        row.update({"provider": provider, "session_id": session_id, "status": status,
                    "last_turn_at": time.time(), **extra})
        if task_head:
            row["task_head"] = task_head[:120]
        try:
            path = self._registry_path()
            secure_dir(path.parent)
            private_write_text(path, json.dumps(rows), tmp=path.with_suffix(".tmp"))
        except OSError:
            pass

    def _registry_update(self, h: AgentHandle, status: str, task_head: str = "", **extra) -> None:
        self._registry_write(h.name, h.provider, h.session_id, status, task_head, **extra)

    # -- public API ---------------------------------------------------------
    async def run(self, task: str, **kw) -> AgentResult:
        self._check_depth()
        o = self._opts(kw)
        audit.append("agent", name=f"run-{o.provider}", task=task[:120], provider=o.provider)
        return await self._guarded(
            lambda: self._backends[o.provider].run_once(task, o), o.timeout)

    def spawn(self, task: str, *, name: str | None = None, **kw) -> AgentHandle:
        self._check_depth()
        o = self._opts(kw)
        name = name or f"agent-{uuid.uuid4().hex[:6]}"
        self._claim_name(name)
        h = AgentHandle(self, name, o.provider, o.timeout)
        self._handles[name] = h
        audit.append("agent", name=name, task=task[:120], provider=o.provider)
        self._registry_update(h, "running", task_head=task)

        async def drive():
            try:
                async def _turn():
                    h._session = await self._backends[o.provider].open_session(task, o)
                    # The row is what OUTLIVES this process, so it has to learn the child's
                    # id while there is still a process to write it. The SDK issues that id
                    # on its init message, turns before the stream ends; rewritten only at
                    # settle, a kernel that died under a running turn left `running` and a
                    # null session_id on disk for a child that had an id all along — visible
                    # to `agent.list()` after the restart and resumable by nobody.
                    h._session.on_session_id = lambda _sid: self._registry_update(
                        h, "running", task_head=task)
                    self._registry_update(h, "running", task_head=task)
                    return await h._session.wait_result()
                r = await self._guarded(_turn, o.timeout)
                h._status = "done"
                self._registry_update(h, "done")
                h._settle_result(r)
            except asyncio.CancelledError:
                # interrupt() owns the interrupted transition (status + registry + close);
                # a cancel from anywhere else still leaves an honest terminal state.
                if h._status not in _TERMINAL and not h._interrupting:
                    h._status = "interrupted"
                    self._registry_update(h, "interrupted")
                    h._settle_exception(RuntimeError(f"agent {h.name!r} was cancelled"))
                raise
            except Exception as e:  # noqa: BLE001 — surfaced through result()
                h._status = "error"
                self._registry_update(h, "error")
                await h.close()            # tear down the CLI process tree (F6)
                h._settle_exception(e)
        h._driver = asyncio.ensure_future(drive())
        return h

    async def fork(self, task: str, **kw) -> AgentResult:
        self._check_depth()
        o = self._opts(kw)
        if o.provider != "claude":
            raise NotImplementedError("fork is claude-only; use provider='claude'")
        from ptc.discovery import read_meta
        key = self._config.get("key")
        if not key:
            raise RuntimeError("this kernel has no session key in its config — "
                               "fork needs one to look up meta.json")
        sid = read_meta(key).get("claude_session_id")
        if not sid:
            raise RuntimeError("no claude_session_id known for this kernel "
                               "(alias-keyed session) — fork unavailable")
        audit.append("agent", name="fork", task=task[:120], provider="claude")
        r = await self._guarded(
            lambda: self._backends["claude"].run_once(task, o, resume=sid, fork=True),
            o.timeout)
        # S2: a forked child's own transcript carries no back-pointer to its parent, so
        # this row is the only place the relation survives on disk.
        self._registry_write(f"fork-{uuid.uuid4().hex[:6]}", "claude", r.session_id,
                             "done", task_head=task, parent_session_id=sid)
        return r

    async def gather(self, *handles: AgentHandle) -> list:
        return list(await asyncio.gather(*(h.result() for h in handles)))

    def list(self) -> list:
        """The registry, with this kernel's own handles laid over it.

        `live` is a claim about the CLI BEHIND the handle, not about the row: close() and
        the error teardown both null the session out, and send() refuses a handle without
        one — so a flat True invited a call that cannot work, on exactly the handles a
        caller runs list() to sort out. A driver that has not opened its session yet is
        live all the same; it is on its way to one. The row itself survives either way —
        its status and its session id are the record of what ran and what may be resumed.
        """
        rows = {r["name"]: dict(r) for r in self._registry_load() if r.get("name")}
        for h in self._handles.values():
            rows.setdefault(h.name, {"name": h.name})
            rows[h.name].update({"provider": h.provider, "session_id": h.session_id,
                                 "status": h.status,
                                 "live": h._session is not None or h.status == "running"})
        return sorted(rows.values(), key=lambda r: r.get("created_at", 0))

    def resume(self, session_id: str, **kw) -> AgentHandle:
        # resume opens a real CLI session and can drive unbounded turns through send(),
        # so it is subject to the same depth brake as run/spawn/fork.
        self._check_depth()
        o = self._opts(kw)
        name = self._resumed_name(session_id)
        self._claim_name(name)
        h = AgentHandle(self, name, o.provider, o.timeout)
        # The resumed id IS this handle's session id from the start: a resumed session
        # keeps its id, and nothing folds a ResultMessage until the first send(), so
        # without this the registry row (the point of resume) would carry null.
        h._session_id_seen = session_id
        self._handles[h.name] = h
        audit.append("agent", name=h.name, task=f"resume {session_id}"[:120],
                     provider=o.provider)

        async def open_():
            try:
                async def _open():
                    h._session = await self._backends[o.provider].open_session(
                        None, o, resume=session_id)
                    return h._session
                await self._guarded(_open, o.timeout)
                h._status = "done"       # nothing running; the session is a send() target
                h._settle_result(
                    AgentResult("(resumed — use send())", session_id, None, None, None, 0))
                self._registry_update(h, "done")
            except asyncio.CancelledError:
                if h._status not in _TERMINAL and not h._interrupting:
                    h._status = "interrupted"
                    self._registry_update(h, "interrupted")
                    h._settle_exception(RuntimeError(f"agent {h.name!r} was cancelled"))
                raise
            except Exception as e:  # noqa: BLE001 — surfaced through result()
                h._status = "error"
                self._registry_update(h, "error")
                await h.close()
                h._settle_exception(e)
        h._driver = asyncio.ensure_future(open_())     # retained, like spawn's (F6)
        return h
