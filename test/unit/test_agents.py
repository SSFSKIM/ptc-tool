"""Agent namespace against a FAKE backend — no SDK, no auth, no network.

The fake is the contract fixture for T21 too (it imports FakeBackend from here), so its
default behavior stays exactly as the plan specified; the failure/hang knobs are opt-in.
"""
import asyncio
import json

import pytest

from ptc.runtime import agents
from ptc.runtime.agents import AgentOpts, AgentResult, _Agent
from ptc.runtime.state import STATE


class FakeSession:
    def __init__(self, task, o, *, sid: str | None = None, hang: bool = False,
                 fail: Exception | None = None, send_hang: bool = False,
                 send_aborts: bool = False,
                 close_hang: bool = False, ignore_interrupt: bool = False,
                 drain_s: float = 0.05):
        #: The real Session leaves this None until a ResultMessage is folded — a fake
        #: that fills it in eagerly is kinder than reality exactly where resume()'s
        #: registry row goes wrong (T20 review I3). A resumed session keeps the id it
        #: resumed, so `sid` (the backend's `resume=`) wins when there is one.
        self.session_id = None
        self._sid = sid or f"fake-{task[:8]}"
        self._task = task
        self.sent: list[str] = []
        self.send_entered: list[str] = []
        self.closed = False
        self.interrupted = False
        self._hang = hang
        self._fail = fail
        self._send_hang = send_hang
        self._send_aborts = send_aborts
        self._close_hang = close_hang
        self._ignore_interrupt = ignore_interrupt
        self._drain_s = drain_s
        self._aborted = asyncio.Event()

    async def wait_result(self):
        if self._fail is not None:
            raise self._fail
        if self._hang:
            # S1: an interrupted turn does not raise — it ends with a NORMAL
            # ResultMessage carrying terminal_reason='aborted_streaming', after a drain
            # measured at 4-13 s live. interrupt() must wait that drain out, so the fake
            # makes it take real (if small) time rather than completing instantly.
            await self._aborted.wait()
            await asyncio.sleep(self._drain_s)
            self.session_id = self._sid
            return AgentResult("(aborted)", self.session_id, None, None, 1, 0)
        await asyncio.sleep(0.05)
        self.session_id = self._sid
        return AgentResult(f"did:{self._task}", self.session_id, None, 0.0, 1, 50)

    async def send(self, msg):
        self.send_entered.append(msg)      # entered — before anything can hang
        if self._send_aborts:
            # A follow-up turn under S1's contract: the interrupt does not raise, the
            # turn ends NORMALLY with whatever partial it had.
            await self._aborted.wait()
            await asyncio.sleep(self._drain_s)
            self.session_id = self._sid
            return AgentResult(f"partial:{msg}", self.session_id, None, None, 1, 0)
        if self._send_hang:
            await asyncio.sleep(3600)
        self.sent.append(msg)
        self.session_id = self._sid
        return AgentResult(f"reply:{msg}", self.session_id, None, 0.0, 1, 10)

    async def interrupt(self):
        """The CLI acknowledges quickly; the DRAIN it triggers is what takes time, and
        it lands in wait_result as a normal aborted result. `ignore_interrupt` models a
        CLI that never honors it — the case the drain budget exists for."""
        self.interrupted = True
        if not self._ignore_interrupt:
            self._aborted.set()
        await asyncio.sleep(0.01)

    async def close(self):
        if self._close_hang:
            await asyncio.sleep(3600)
        self.closed = True

    def messages(self):
        return [{"task": self._task}]


class FakeBackend:
    def __init__(self, **session_kw):
        self.concurrent = 0
        self.max_seen = 0
        self.sessions: list[FakeSession] = []
        self.open_error: Exception | None = None
        self.run_error: Exception | None = None
        self._session_kw = session_kw

    async def run_once(self, task, o, *, resume=None, fork=False):
        self.concurrent += 1
        self.max_seen = max(self.max_seen, self.concurrent)
        try:
            if self.run_error is not None:
                raise self.run_error
            await asyncio.sleep(0.1)
            tag = "forked:" if fork else ""
            return AgentResult(f"{tag}{task}", resume or "fresh", None, 0.0, 1, 100)
        finally:
            self.concurrent -= 1

    async def open_session(self, task, o, *, resume=None):
        if self.open_error is not None:
            raise self.open_error
        s = FakeSession(task or "resumed", o, sid=resume, **self._session_kw)
        self.sessions.append(s)
        return s


def _agent(tmp_path, backend=None, **cfg):
    STATE.kernel_dir = tmp_path
    conf = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 2}
    conf.update(cfg)
    STATE.config = conf
    b = backend or FakeBackend()
    return _Agent(conf, {"claude": b, "codex": FakeBackend()})


def _rows(tmp_path) -> list:
    return json.loads((tmp_path / "agents.json").read_text())


def _row(tmp_path, name: str) -> dict:
    matching = [r for r in _rows(tmp_path) if r["name"] == name]
    assert len(matching) == 1, f"expected exactly one {name!r} row, got {matching}"
    return matching[0]


# -- plan-specified contract --------------------------------------------------------

def test_run_and_depth_guard(tmp_path):
    a = _agent(tmp_path)
    r = asyncio.run(a.run("hello"))
    assert r.text == "hello"
    a2 = _agent(tmp_path, depth=1)
    with pytest.raises(RuntimeError, match="agent depth limit reached"):
        asyncio.run(a2.run("nope"))


def test_semaphore_bounds_concurrency(tmp_path):
    a = _agent(tmp_path, max_concurrency=2)
    fb = a._backends["claude"]

    async def burst():
        await asyncio.gather(*(a.run(f"t{i}") for i in range(6)))
    asyncio.run(burst())
    assert fb.max_seen <= 2


def test_spawn_gather_registry_send(tmp_path):
    a = _agent(tmp_path)

    async def flow():
        h1 = a.spawn("alpha", name="one")
        h2 = a.spawn("beta")
        assert h1.status == "running"
        r1, r2 = await a.gather(h1, h2)
        assert r1.text == "did:alpha" and h1.status == "done"
        follow = await h1.send("more")
        assert follow.text == "reply:more"
        return h1
    h = asyncio.run(flow())
    rows = _rows(tmp_path)
    assert any(e["name"] == "one" and e["status"] == "done" for e in rows)
    listed = a.list()
    assert any(e["name"] == "one" for e in listed)
    assert h.session_id == "fake-alpha"


def test_handle_history_needs_a_session_id(tmp_path):
    a = _agent(tmp_path)

    async def flow():
        h = a.spawn("alpha", name="one")
        # Nothing has awaited the driver yet, so open_session hasn't run and no session
        # id is bound — history() must refuse legibly rather than delegate with None.
        with pytest.raises(RuntimeError, match="has no session id yet"):
            h.history()
        await a.gather(h)   # let the driver settle so it doesn't outlive the loop
    asyncio.run(flow())


def test_handle_history_delegates_to_transcript_by_session_id(tmp_path, monkeypatch):
    # T25's history() locates a transcript by session id (with a glob fallback when the
    # cwd guess misses); AgentHandle.history() (spec: "returns the same type for
    # children") is a thin delegate — this proves the wiring, not transcript.py's logic
    # (that's test_transcript.py's job).
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    proj = tmp_path / ".claude" / "projects" / "somewhere"
    proj.mkdir(parents=True)
    rows = [{"type": "user", "message": {"role": "user", "content": "hi from child"}}]
    (proj / "fake-alpha.jsonl").write_text(_json.dumps(rows[0]))

    a = _agent(tmp_path)

    async def flow():
        h = a.spawn("alpha", name="one")
        await a.gather(h)
        return h
    h = asyncio.run(flow())
    assert h.session_id == "fake-alpha"

    t = h.history()
    assert t.path == proj / "fake-alpha.jsonl"
    assert t.user() == ["hi from child"]


def test_handle_history_refuses_a_codex_handle_instead_of_guessing(tmp_path):
    """A codex handle's session_id is a THREAD id, and its turns live in a codex rollout
    under $CODEX_HOME/sessions — a different store in a different format. Searching
    Claude's JSONL for it either fails or, worse, glob-matches an unrelated transcript and
    answers with somebody else's conversation. v1 reads the Claude store only, and says so.
    """
    a = _agent(tmp_path)

    async def flow():
        h = a.spawn("alpha", name="one", provider="codex")
        await a.gather(h)
        return h
    h = asyncio.run(flow())

    assert h.session_id, "the handle did bind a session id — this is not the id-less case"
    with pytest.raises(NotImplementedError, match="codex rollout"):
        h.history()


def test_spawn_name_collision_and_timeout(tmp_path):
    a = _agent(tmp_path)

    async def flow():
        a.spawn("x", name="dup")
        with pytest.raises(ValueError, match="name already in use"):
            a.spawn("y", name="dup")
        with pytest.raises(asyncio.TimeoutError):
            await a.run("slow", timeout=0.01)
    asyncio.run(flow())


# -- lifecycle contract: no leaked permit, no zombie session, settle exactly once ----

def test_run_error_releases_permit(tmp_path):
    """A failed call must not strand its permit: with a bound of 1, the next call
    would hang forever if the first leaked."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        b.run_error = RuntimeError("CLI died")
        with pytest.raises(RuntimeError, match="CLI died"):
            await a.run("first")
        b.run_error = None
        return await asyncio.wait_for(a.run("second"), 2)
    assert asyncio.run(flow()).text == "second"


def test_run_timeout_releases_permit(tmp_path):
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        with pytest.raises(asyncio.TimeoutError):
            await a.run("slow", timeout=0.01)
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"


def test_spawn_timeout_settles_error_and_tears_down(tmp_path):
    b = FakeBackend(hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("hangs", name="h1", timeout=0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(h.result(), 2)
        assert h.status == "error"
        assert b.sessions[0].closed, "timed-out session left open (CLI process tree leak)"
        # the permit came back
        b2 = a._backends["claude"]
        assert b2 is b
        r = await asyncio.wait_for(a.run("after"), 2)
        return h, r
    h, r = asyncio.run(flow())
    assert r.text == "after"
    assert _row(tmp_path, "h1")["status"] == "error"


def test_spawn_open_failure_settles_error(tmp_path):
    """CLI death at connect (S1: ProcessError in ~1.5s) surfaces through result()."""
    b = FakeBackend()
    b.open_error = RuntimeError("ProcessError: claude exited with code 1")
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("doomed", name="d1")
        with pytest.raises(RuntimeError, match="ProcessError"):
            await asyncio.wait_for(h.result(), 2)
        assert h.status == "error"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "d1")["status"] == "error"


def test_interrupt_settles_via_the_drains_normal_completion(tmp_path):
    """The S1 contract: an interrupted turn does NOT raise — it ends with a normal
    ResultMessage (terminal_reason='aborted_streaming') after a 4-13 s drain. So
    interrupt() signals the session and lets the driver's ordinary completion path settle
    the handle with that partial result; pre-empting the drain would throw it away."""
    b = FakeBackend(hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("forever", name="i1")
        await asyncio.sleep(0.05)                 # let the driver open the session
        assert isinstance(h._driver, asyncio.Task), "driver task must be retained"
        await asyncio.wait_for(h.interrupt(), 5)
        assert h.status == "interrupted"
        assert b.sessions[0].interrupted and b.sessions[0].closed
        assert h._driver.done() and not h._driver.cancelled(), \
            "the drain was pre-empted instead of awaited to its normal end"
        r = await asyncio.wait_for(h.result(), 2)
        assert r.text == "(aborted)", "the aborted turn's own result must reach the caller"
        assert (await asyncio.wait_for(h.result(), 2)) is r     # settled exactly once
        # the interrupted turn's permit is back
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    row = _row(tmp_path, "i1")
    assert row["status"] == "interrupted" and row["session_id"] == "fake-forever"


def test_interrupt_cancels_the_driver_when_the_drain_outlives_its_budget(tmp_path,
                                                                        monkeypatch):
    """A CLI that never honors the interrupt must not wedge the cell: past the drain
    budget the driver is cancelled, and the handle still settles once with its permit
    released — the fallback, not the normal path."""
    monkeypatch.setattr(agents, "_INTERRUPT_DRAIN_S", 0.05)
    b = FakeBackend(hang=True, ignore_interrupt=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("never-aborts", name="i2")
        await asyncio.sleep(0.05)
        await asyncio.wait_for(h.interrupt(), 5)   # would hang forever if unbounded
        assert h.status == "interrupted"
        assert h._driver.done() and b.sessions[0].closed
        with pytest.raises(RuntimeError, match="was interrupted"):
            await asyncio.wait_for(h.result(), 2)
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "i2")["status"] == "interrupted"


def test_interrupt_preempts_a_queued_turn_that_has_no_session(tmp_path):
    """A handle still waiting for a permit has no session, so nothing was ever asked to
    abort: there is no drain to await. interrupt() must pre-empt the driver rather than
    sit out the drain budget — otherwise the queued turn later acquires its permit and
    RUNS TO COMPLETION (a real CLI turn, really billed) and is then merely relabelled."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        sem = a._semaphore()
        await sem.acquire()                    # the only permit: the spawn must queue
        try:
            h = a.spawn("queued", name="n1")
            await asyncio.sleep(0.05)
            assert h._session is None, "the queued turn should not have opened a session"
            # The bound is the regression guard: with nothing to drain, waiting the drain
            # budget out is the defect.
            await asyncio.wait_for(h.interrupt(), 2)
            assert h.status == "interrupted"
            assert h._driver.done()
            with pytest.raises(RuntimeError, match="was interrupted"):
                await asyncio.wait_for(h.result(), 2)
        finally:
            sem.release()
        await asyncio.sleep(0.2)               # a surviving driver would run its turn here
        assert b.sessions == [], "the interrupted turn still opened (and billed) a session"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "n1")["status"] == "interrupted"


def test_interrupt_latch_resets_so_a_cancelled_interrupt_can_be_retried(tmp_path):
    """A cancelled interrupt() must not strand the handle. The latch that keeps a second
    concurrent interrupt from re-signalling under the first one's drain has to clear when
    the first never finishes — otherwise every later interrupt() returns at the latch and
    the driver stays alive with the handle unsettled, recoverable only via close()."""
    b = FakeBackend(hang=True, drain_s=0.5)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("forever", name="n2")
        await asyncio.sleep(0.05)
        first = asyncio.ensure_future(h.interrupt())
        await asyncio.sleep(0.1)               # first is inside the drain wait
        assert not first.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert h.status == "running", "the cancelled interrupt left the handle unsettled"
        # The retry must actually re-enter and finish the job.
        await asyncio.wait_for(h.interrupt(), 5)
        assert h.status == "interrupted"
        assert h._driver.done() and b.sessions[0].closed
        assert (await asyncio.wait_for(h.result(), 2)).text == "(aborted)"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "n2")["status"] == "interrupted"


def test_interrupt_does_not_overwrite_a_finished_turn(tmp_path):
    """S1: an interrupted turn ends with a normal ResultMessage, so a late interrupt can
    race a completed drain. A terminal handle stays terminal — status and registry alike."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("quick", name="q1")
        r = await asyncio.wait_for(h.result(), 2)
        await asyncio.wait_for(h.interrupt(), 5)
        assert h.status == "done"
        assert b.sessions[0].closed, "late interrupt should still tear the session down"
        assert (await asyncio.wait_for(h.result(), 2)) is r
    asyncio.run(flow())
    assert _row(tmp_path, "q1")["status"] == "done"


def test_teardown_is_bounded_when_close_hangs(tmp_path, monkeypatch):
    """A wedged disconnect must not hang the cell: teardown has its own budget."""
    monkeypatch.setattr(agents, "_TEARDOWN_S", 0.05)
    b = FakeBackend(hang=True, close_hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("wedged", name="w1")
        await asyncio.sleep(0.05)
        await asyncio.wait_for(h.interrupt(), 3)   # would hang forever if unbounded
        assert h.status == "interrupted"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"


def test_send_timeout_releases_permit(tmp_path):
    """send() is not exempt from the caller's deadline. The spawn's own timeout must be
    comfortably longer than its turn, or the DRIVER's deadline wins the race and send()
    is never entered at all — which is what this test used to prove (T20 review I2)."""
    b = FakeBackend(send_hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("s", name="s1", timeout=0.3)       # turn takes 0.05s; send hangs
        await asyncio.wait_for(h.result(), 2)
        assert h.status == "done"

        async def _send():
            with pytest.raises(asyncio.TimeoutError):
                await h.send("hi")                     # send's OWN deadline must fire
        # The hang guard raises a DIFFERENT failure than the one under test: if send()
        # never times out, the cancel lands inside pytest.raises as CancelledError, the
        # block re-raises it, and this wait_for fails the test instead of passing it.
        await asyncio.wait_for(_send(), 3)
        assert b.sessions[0].send_entered == ["hi"], "send() was never entered"
        assert b.sessions[0].sent == [], "the hung send should not have completed"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"


def test_send_timeout_poisons_the_session(tmp_path):
    """A send() that timed out leaves a turn possibly still running and a half-drained
    stream: the handle must go terminal and the session must close, or the next send()
    reuses that process and talks over a live (billed) turn."""
    b = FakeBackend(send_hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("s", name="s1", timeout=0.3)
        await asyncio.wait_for(h.result(), 2)
        assert h.status == "done"

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(h.send("hi"), 3)

        assert h.status == "error", "a failed send left the handle looking healthy"
        assert b.sessions[0].closed, "the session survived a send that never returned"
        assert _row(tmp_path, "s1")["status"] == "error"
        with pytest.raises(RuntimeError, match="no live session"):
            await asyncio.wait_for(h.send("again"), 2)
    asyncio.run(flow())


def test_send_after_interrupt_is_refused(tmp_path):
    b = FakeBackend(hang=True)
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.spawn("forever", name="x1")
        await asyncio.sleep(0.05)
        await asyncio.wait_for(h.interrupt(), 5)
        with pytest.raises(RuntimeError, match="no live session"):
            await asyncio.wait_for(h.send("hi"), 2)
    asyncio.run(flow())


def test_unknown_provider_and_bad_option(tmp_path):
    a = _agent(tmp_path)
    with pytest.raises(ValueError, match="unknown provider"):
        asyncio.run(a.run("x", provider="gemini"))
    with pytest.raises(TypeError, match="unsupported agent options"):
        asyncio.run(a.run("x", temperature=0.5))


def test_fork_records_parent_session_id(tmp_path, monkeypatch):
    """S2: the child's JSONL carries no back-pointer, so the registry row is the only
    place the parent link survives."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    from ptc.discovery import write_meta
    a = _agent(tmp_path)
    write_meta("k", claude_session_id="parent-sid-1")
    r = asyncio.run(a.fork("recall"))
    assert r.text == "forked:recall"
    row = next(e for e in _rows(tmp_path) if e["name"].startswith("fork-"))
    assert row["parent_session_id"] == "parent-sid-1"
    assert row["session_id"] == "parent-sid-1"      # fake echoes resume as the child sid
    assert row["status"] == "done"


def test_resume_records_the_real_session_id_and_audits(tmp_path):
    """The registry row is the whole point of resume — `agent.list()` after a restart is
    what makes a session resumable at all — so it must carry the id, not null. The real
    Session has no id until a turn folds, so resume() carries the one it was given."""
    SID = "sid-abcdef-1234"
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.resume(SID)
        r = await asyncio.wait_for(h.result(), 2)
        assert h.status == "done" and r.session_id == SID
        assert h.session_id == SID
        follow = await asyncio.wait_for(h.send("more"), 2)   # a resumed session is a target
        assert follow.text == "reply:more"
        return h
    h = asyncio.run(flow())
    assert h.name == "resumed-sid-abcd"
    row = _row(tmp_path, h.name)
    assert row["session_id"] == SID and row["status"] == "done"
    assert row["provider"] == "claude"
    assert next(e for e in a.list() if e["name"] == h.name)["session_id"] == SID
    entries = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "agent" and e["name"] == h.name and e["provider"] == "claude"
               and SID in e["task"] for e in entries), entries


def test_resume_refuses_at_max_depth(tmp_path):
    """resume opens a real CLI session and can drive unbounded turns through send(), so
    the depth brake applies to it exactly as it does to run/spawn/fork — and it must fire
    before any session is opened."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, depth=1)

    async def flow():
        with pytest.raises(RuntimeError, match="agent depth limit reached"):
            a.resume("sid-deep")
        await asyncio.sleep(0.05)      # a handle made anyway would open its session here
    asyncio.run(flow())
    assert b.sessions == [] and not (tmp_path / "agents.json").exists()


def test_list_merges_live_handles_over_registry(tmp_path):
    a = _agent(tmp_path)
    (tmp_path / "agents.json").write_text(json.dumps(
        [{"name": "ghost", "provider": "claude", "session_id": "s0", "status": "done",
          "created_at": 1.0}]))

    async def flow():
        h = a.spawn("live", name="fresh")
        await asyncio.wait_for(h.result(), 2)
    asyncio.run(flow())
    names = {e["name"] for e in a.list()}
    assert {"ghost", "fresh"} <= names
    assert next(e for e in a.list() if e["name"] == "fresh")["live"] is True


def _secretish(name: str, value: str) -> bool:
    """The spec's redaction predicate (Trust model + T18 Decision Log), restated here as
    the assertion this test makes about the explicitly forwarded child env."""
    return (any(w in name.upper() for w in ("KEY", "TOKEN", "BEARER", "SECRET"))
            or value.startswith("sk-ant-"))


def test_child_env_is_ptc_only_and_carries_no_secrets(monkeypatch, tmp_path):
    """Decision Log (T18 review): the kernel inherits the adapter's environment verbatim,
    including an sk-ant- OAuth bearer. What PTC forwards EXPLICITLY must be PTC's own
    variables — never a merged copy of os.environ (the shape bash() uses, one seam over)."""
    from ptc.runtime import claude_backend
    monkeypatch.setenv("CLAUDE_ROUTINE_BEARER", "sk-ant-oat01-not-a-real-token")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-secret")
    STATE.config = {"key": "parent-key", "depth": 0, "max_depth": 1}
    env = claude_backend._child_env(AgentOpts(cwd=str(tmp_path)))
    assert set(env) == {"PTC_SESSION", "PTC_DEPTH", "PTC_MAX_DEPTH", "PTC_CWD"}
    assert env["PTC_SESSION"] != "parent-key" and env["PTC_SESSION"].startswith("parent-key-a")
    assert env["PTC_DEPTH"] == "1" and env["PTC_MAX_DEPTH"] == "1"
    for name, value in env.items():
        assert not _secretish(name, value), name


# -- r2 review: child keys, follow-up-turn lifecycle, name reuse ---------------------

def test_child_key_survives_a_parent_key_at_the_length_bound(tmp_path):
    """safe_key() truncates at 128 characters. Appending the child suffix to a parent key
    already at the bound truncated the suffix straight back off, so the child resolved to
    the PARENT's key: it attached to the parent's kernel and inherited its depth-0
    bootstrap, losing session isolation and the recursion brake together."""
    from ptc.paths import kernel_dir, safe_key
    from ptc.runtime.agents import child_ptc_env

    parent = "p" * 128
    _agent(tmp_path, key=parent)           # sets STATE.config, which child_ptc_env reads
    child = child_ptc_env(AgentOpts())["PTC_SESSION"]

    assert safe_key(child) == child, "the child key must survive sanitization unchanged"
    assert child != parent
    assert kernel_dir(child) != kernel_dir(parent), "the child attached to the parent kernel"
    assert child_ptc_env(AgentOpts())["PTC_DEPTH"] == "1"

    # short parents keep the readable form, and the shortening is per-parent deterministic
    _agent(tmp_path, key="short")
    assert child_ptc_env(AgentOpts())["PTC_SESSION"].startswith("short-a")
    _agent(tmp_path, key=parent)
    other = child_ptc_env(AgentOpts())["PTC_SESSION"]
    assert other != child and other[:-8] == child[:-8], "same parent, same prefix"


def test_interrupt_during_a_follow_up_send_reaches_the_backend(tmp_path):
    """A follow-up send is an ACTIVE turn. While it was left looking 'done', interrupt()
    took the terminal branch: it closed the session without ever signalling the backend,
    and the send settled as an error instead of returning the interrupted partial."""
    b = FakeBackend(send_aborts=True)
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.spawn("alpha", name="fu1")
        await asyncio.wait_for(h.result(), 5)
        assert h.status == "done"

        send = asyncio.ensure_future(h.send("more"))
        await asyncio.sleep(0.1)
        assert h.status == "running", "the follow-up turn is not tracked as active"
        assert _row(tmp_path, "fu1")["status"] == "running"

        await asyncio.wait_for(h.interrupt(), 5)
        assert b.sessions[0].interrupted, "the interrupt never reached the backend"
        assert (await asyncio.wait_for(send, 5)).text == "partial:more"
        assert h.status == "interrupted"
        assert b.sessions[0].closed
        # the turn's permit came back
        assert (await asyncio.wait_for(a.run("after"), 5)).text == "after"

    asyncio.run(flow())
    assert _row(tmp_path, "fu1")["status"] == "interrupted"


def test_a_completed_send_returns_the_handle_to_done(tmp_path):
    """The running/interrupt lifecycle is borrowed for the turn, not kept: an ordinary
    send leaves the handle exactly as it found it, still a live send target."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.spawn("alpha", name="fu2")
        await asyncio.wait_for(h.result(), 5)
        assert (await asyncio.wait_for(h.send("more"), 5)).text == "reply:more"
        assert h.status == "done"
        assert (await asyncio.wait_for(h.send("again"), 5)).text == "reply:again"

    asyncio.run(flow())
    assert _row(tmp_path, "fu2")["status"] == "done"


def test_concurrent_sends_are_serialized_on_one_session(tmp_path):
    """Two turns interleaved on one CLI session would each drain part of the other's
    stream; the handle runs them one at a time instead."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.spawn("alpha", name="fu3")
        await asyncio.wait_for(h.result(), 5)
        out = await asyncio.wait_for(asyncio.gather(h.send("one"), h.send("two")), 5)
        assert {r.text for r in out} == {"reply:one", "reply:two"}
        assert b.sessions[0].sent == ["one", "two"], "the sends overlapped"

    asyncio.run(flow())


def test_spawn_refuses_a_name_whose_session_is_still_live(tmp_path):
    """A finished handle deliberately keeps its CLI session open as a send() target.
    Reusing the name overwrote the handle and the registry row and left that process
    running with no owner and no cleanup path."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.spawn("alpha", name="dup")
        await asyncio.wait_for(h.result(), 5)
        assert h.status == "done"

        with pytest.raises(ValueError, match="close"):
            a.spawn("beta", name="dup")
        assert len(b.sessions) == 1, "the refused spawn still opened a session"

        await h.close()
        h2 = a.spawn("beta", name="dup")       # closed: the name is free again
        assert (await asyncio.wait_for(h2.result(), 5)).text == "did:beta"

    asyncio.run(flow())
