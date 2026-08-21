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
import json
import os
import time
import uuid
from dataclasses import dataclass

from . import audit
from .state import STATE

#: Teardown gets its own budget, separate from any call deadline. S1 measured a cancelled
#: SDK task taking 0.6-5.7 s to unwind and an interrupt()ed turn 4.0-13.0 s to drain, so
#: "await teardown" must be bounded but generous — never assumed instant.
_TEARDOWN_S = 30.0

#: How long an interrupted turn is given to end the way S1 says it ends: NORMALLY, with a
#: ResultMessage carrying terminal_reason='aborted_streaming' (4.0-13.0 s measured). Within
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
    contract lives in exactly one place."""
    async def _work():
        async with sem:
            return await make_coro()
    if timeout:
        return await asyncio.wait_for(_work(), timeout)
    return await _work()


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


def child_ptc_env(o: AgentOpts) -> dict:
    """PTC's own variables for a child agent process — the same four for every backend.

    Two rules here are load-bearing and must not drift between backends: PTC_SESSION is
    always overridden to a fresh child key (a child inheriting the parent's key would
    attach to the parent's kernel), and PTC_DEPTH is the parent's depth + 1, which is what
    makes the depth brake bite one level down.
    """
    parent_key = STATE.config.get("key") or "root"
    return {
        "PTC_SESSION": f"{parent_key}-a{uuid.uuid4().hex[:6]}",
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
        if self._status == "running":
            await self.result()
        # Bind the session BEFORE the lambda: _guarded evaluates it after the semaphore
        # wait, and a close()/interrupt() landing in that window would otherwise turn the
        # intended refusal into an AttributeError on None (M2).
        session = self._session
        if session is None:
            raise RuntimeError(f"agent {self.name!r} has no live session to send to")
        r = await self._owner._guarded(lambda: session.send(msg), self._timeout)
        self._owner._registry_update(self, self._status)
        return r

    def messages(self) -> list:
        return self._session.messages() if self._session else []

    def history(self):
        """The child's own transcript (T25's `Transcript`, same type `history()` returns).

        The child writes its own JSONL under a cwd this handle never tracked (only
        `AgentOpts.cwd`, which is not retained past spawn), so nothing is passed as
        `cwd=` here. That does NOT mean resolution is by session id alone: `history()`
        still reads the KERNEL's `meta.json` first and tries the direct path under that
        cwd, which hits whenever the child ran in the kernel's own directory (the
        default). `transcript._resolve_path`'s glob is the fallback that covers a child
        given a different `cwd=` at spawn.
        """
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
        session, self._session = self._session, None
        if session is not None:
            self._session_id_seen = getattr(session, "session_id", None) or self._session_id_seen
            await _bounded(session.close())

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
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(path)
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
        if name in self._handles and self._handles[name].status == "running":
            raise ValueError(f"agent name already in use: {name!r}")
        h = AgentHandle(self, name, o.provider, o.timeout)
        self._handles[name] = h
        audit.append("agent", name=name, task=task[:120], provider=o.provider)
        self._registry_update(h, "running", task_head=task)

        async def drive():
            try:
                async def _turn():
                    h._session = await self._backends[o.provider].open_session(task, o)
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
        rows = {r["name"]: dict(r) for r in self._registry_load() if r.get("name")}
        for h in self._handles.values():
            rows.setdefault(h.name, {"name": h.name})
            rows[h.name].update({"provider": h.provider, "session_id": h.session_id,
                                 "status": h.status, "live": True})
        return sorted(rows.values(), key=lambda r: r.get("created_at", 0))

    def resume(self, session_id: str, **kw) -> AgentHandle:
        # resume opens a real CLI session and can drive unbounded turns through send(),
        # so it is subject to the same depth brake as run/spawn/fork.
        self._check_depth()
        o = self._opts(kw)
        h = AgentHandle(self, f"resumed-{session_id[:8]}", o.provider, o.timeout)
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
