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
                 send_aborts: bool = False, announce_early: bool = False,
                 close_hang: bool = False, ignore_interrupt: bool = False,
                 drain_s: float = 0.05):
        #: The real Session leaves this None until a ResultMessage is folded — a fake
        #: that fills it in eagerly is kinder than reality exactly where resume()'s
        #: registry row goes wrong (T20 review I3). A resumed session keeps the id it
        #: resumed, so `sid` (the backend's `resume=`) wins when there is one.
        self.session_id = None
        #: whoever wants to hear the id the moment the backend learns it, rather than at
        #: settle — the registry, in production (r11 finding 5)
        self.on_session_id = None
        self._announce_early = announce_early
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

    def _announce(self):
        """What the real backend does on the SDK's init message: the id is known long
        before the turn ends, and whoever is listening hears it then."""
        self.session_id = self._sid
        if self.on_session_id is not None:
            self.on_session_id(self._sid)

    async def wait_result(self):
        if self._announce_early:
            self._announce()
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


def test_list_shows_a_closed_handle_as_not_live(tmp_path):
    """`live` said True for every handle this kernel had ever made. A closed one has no
    session — send() refuses it by design — so the flag invited a call that cannot work,
    on exactly the handles a caller runs list() to sort out. The ROW stays either way:
    its status and its session id are the record of what ran and what may be resumed.
    """
    a = _agent(tmp_path)

    async def flow():
        h = a.spawn("live", name="fresh")
        await asyncio.wait_for(h.result(), 2)
        assert next(e for e in a.list() if e["name"] == "fresh")["live"] is True
        await h.close()                    # a finished handle whose CLI has been reaped
        return h

    h = asyncio.run(flow())
    row = next(e for e in a.list() if e["name"] == "fresh")
    assert row["live"] is False, row
    assert row["status"] == "done" and row["session_id"] == h.session_id


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
    assert other != child and other[:-12] == child[:-12], "same parent, same prefix"


def test_a_child_key_suffix_carries_enough_entropy_to_not_collide(tmp_path):
    """Six hex characters is 24 bits: ~2.9% odds of a collision somewhere in a fleet of a
    thousand concurrent children, and the collision is SILENT — the two children resolve
    to one kernel and share a Python namespace with each other. Twelve puts the same fleet
    at ~1.8e-9, and the parent prefix still fits inside safe_key()'s bound above."""
    from ptc.runtime.agents import child_key

    suffix = child_key("short")[len("short"):]
    assert suffix.startswith("-a") and len(suffix) == 14, suffix
    int(suffix[2:], 16)                    # 12 hex characters — 48 bits


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


def test_close_keeps_the_messages_the_handle_saw(tmp_path):
    """Closing a finished handle reaps its CLI; it must not also erase the turn.

    The session object owns the accumulated messages and close() drops the only reference
    to it, so `h.messages()` came back empty from then on — and for a codex handle that is
    the ONLY transcript view there is, since history() refuses that provider by design.
    """
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.spawn("task-x", name="m1")
        await asyncio.wait_for(h.result(), 2)
        before = h.messages()
        await h.close()
        return before, h.messages()

    before, after = asyncio.run(flow())
    assert before, "the fake session records one message per turn"
    assert after == before, "close() discarded the handle's messages"


def test_resume_refuses_a_second_live_handle_for_the_same_session(tmp_path):
    """Resuming the same session twice overwrote `_handles` and the registry row while
    the first CLI was still live, leaving that process unlistable. It is the same
    collision spawn() refuses, and it gets the same answer — until the first is closed."""
    SID = "sid-dupe-0001"
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h = a.resume(SID)
        await asyncio.wait_for(h.result(), 2)
        with pytest.raises(ValueError, match="already in use"):
            a.resume(SID)
        assert a._handles[h.name] is h
        assert len(b.sessions) == 1, "a refused resume must not open a second CLI session"
        await h.close()
        h2 = a.resume(SID)          # the first CLI is gone: the name is free again
        await asyncio.wait_for(h2.result(), 2)
        return h, h2

    h, h2 = asyncio.run(flow())
    assert h2.name == h.name and a._handles[h.name] is h2


def test_two_sessions_sharing_an_eight_char_prefix_get_distinct_handles(tmp_path):
    """`resumed-<first 8 of the id>` is not unique: two different sessions sharing that
    prefix collided on one name, one row and one `_handles` entry."""
    A, B = "abcd1234-aaaa", "abcd1234-bbbb"
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)

    async def flow():
        h1, h2 = a.resume(A), a.resume(B)
        await asyncio.wait_for(h1.result(), 2)
        await asyncio.wait_for(h2.result(), 2)
        return h1, h2

    h1, h2 = asyncio.run(flow())
    assert h1.name != h2.name
    assert a._handles[h1.name] is h1 and a._handles[h2.name] is h2
    rows = {r["name"]: r for r in _rows(tmp_path)}
    assert rows[h1.name]["session_id"] == A and rows[h2.name]["session_id"] == B
    assert {h1.name, h2.name} <= {e["name"] for e in a.list()}


def test_a_persisted_row_survives_a_resume_that_shares_its_prefix(tmp_path):
    """A restart empties `_handles` and leaves `agents.json` standing on purpose — that
    file IS the list of sessions still resumable. Keyed on live handles alone, the first
    resume after a restart whose id shared eight characters with a persisted row took that
    row's name, and the upsert overwrote its session_id: the only reference to the older
    session was gone, and nothing could ever resume it again."""
    OLD, NEW = "abcd1234-old", "abcd1234-new"
    (tmp_path / "agents.json").write_text(json.dumps(
        [{"name": "resumed-abcd1234", "provider": "claude", "session_id": OLD,
          "status": "done", "created_at": 1.0}]))
    b = FakeBackend()
    a = _agent(tmp_path, backend=b)            # a fresh kernel: no live handles at all
    assert a._handles == {}

    async def flow():
        h = a.resume(NEW)
        await asyncio.wait_for(h.result(), 2)
        return h

    h = asyncio.run(flow())
    assert h.name != "resumed-abcd1234"
    rows = {r["name"]: r for r in _rows(tmp_path)}
    assert rows["resumed-abcd1234"]["session_id"] == OLD, "the persisted row was overwritten"
    assert rows[h.name]["session_id"] == NEW
    assert {OLD, NEW} <= {e["session_id"] for e in a.list()}


def test_resuming_the_id_a_persisted_row_already_names_keeps_its_name(tmp_path):
    """The row is not an obstacle to resuming the session it was kept for: same id, same
    name, one row — which is what `agent.list()` after a restart is supposed to give."""
    SID = "abcd1234-old"
    (tmp_path / "agents.json").write_text(json.dumps(
        [{"name": "resumed-abcd1234", "provider": "claude", "session_id": SID,
          "status": "done", "created_at": 1.0}]))
    a = _agent(tmp_path, backend=FakeBackend())

    async def flow():
        h = a.resume(SID)
        await asyncio.wait_for(h.result(), 2)
        return h

    h = asyncio.run(flow())
    assert h.name == "resumed-abcd1234"
    assert len(_rows(tmp_path)) == 1


# --- r6 finding 7: zero is a deadline, not the absence of one -------------------------

def test_a_zero_timeout_times_out_instead_of_running_unbounded(tmp_path):
    """`if timeout:` read 0 as "no deadline", so `agent(..., timeout=0)`, `llm(timeout=0)`
    and `web_search(timeout=0)` — the caller asking for the strictest bound there is — got
    no bound at all and a hung backend could hold its permit forever. The permit still has
    to come back, so the next call must not hang either."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        with pytest.raises(asyncio.TimeoutError):
            await a.run("slow", timeout=0)
        return await asyncio.wait_for(a.run("after"), 5)

    assert asyncio.run(flow()).text == "after"


def test_a_negative_timeout_is_refused_by_name(tmp_path):
    a = _agent(tmp_path)
    with pytest.raises(ValueError, match="non-negative"):
        asyncio.run(a.run("nope", timeout=-1))


def test_timeout_none_still_means_no_deadline(tmp_path):
    a = _agent(tmp_path)
    assert asyncio.run(a.run("hello", timeout=None)).text == "hello"


# -- r7 findings 1 & 2: closing before the turn starts, and a turn that reports failure --

def test_close_preempts_a_queued_turn_that_never_opened_a_session(tmp_path):
    """close() before the driver assigned `_session` — right after spawn(), or while the
    handle is still queued on the semaphore — returned without touching the driver, which
    then took its permit, opened a CLI and ran a whole billed turn for a handle its owner
    had already closed. It is interrupt()'s no-session branch reached from the other door:
    nothing is draining, so the driver is pre-empted at once and the handle settles."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        sem = a._semaphore()
        await sem.acquire()                    # the only permit: the spawn must queue
        try:
            h = a.spawn("queued", name="c1")
            await asyncio.sleep(0.05)
            assert h._session is None, "the queued turn should not have opened a session"
            await asyncio.wait_for(h.close(), 2)
            assert h.status == "interrupted"
            assert h._driver.done()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(h.result(), 2)
        finally:
            sem.release()
        await asyncio.sleep(0.2)               # a surviving driver would run its turn here
        assert b.sessions == [], "the closed handle still opened (and billed) a session"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "c1")["status"] == "interrupted"


def test_close_leaves_a_finished_handle_alone(tmp_path):
    """The pre-emption is for a driver that never started its turn. A handle that FINISHED
    keeps its terminal status and its settled result through close() — and through a second
    close(), which finds no session and a driver long done."""
    b = FakeBackend()
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("quick", name="c2")
        r = await asyncio.wait_for(h.result(), 2)
        await asyncio.wait_for(h.close(), 2)
        await asyncio.wait_for(h.close(), 2)
        assert h.status == "done" and b.sessions[0].closed
        assert (await asyncio.wait_for(h.result(), 2)) is r
    asyncio.run(flow())
    assert _row(tmp_path, "c2")["status"] == "done"


def test_a_turn_that_reports_failure_settles_the_handle_as_an_error(tmp_path):
    """A ResultMessage with is_error set is a turn that did NOT do its job (rate limit,
    max turns, execution failure). The backend raises it, so the handle goes `error` and
    result() re-raises with the CLI's own text — rather than `done` with the error notice
    standing in for the answer."""
    from ptc.runtime.agents import AgentFailed

    b = FakeBackend(fail=AgentFailed("the claude turn ended in error: usage limit reached"))
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("doomed", name="c3")
        with pytest.raises(AgentFailed, match="usage limit reached"):
            await asyncio.wait_for(h.result(), 2)
        assert h.status == "error"
        assert b.sessions[0].closed, "a failed turn must still release its CLI"
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "c3")["status"] == "error"


# --- r11 finding 5: the row learns the child's id at capture time, not at settle -------

def test_the_registry_learns_a_child_session_id_while_the_turn_is_still_running(tmp_path):
    """`agents.json` is what survives this process; the id has to reach it while there is
    still a process to write it.

    The SDK issues the child's session id on its init message, turns before the stream
    ends, but the row was only ever rewritten at settle. A kernel that died under a running
    turn — or a turn that failed mid-stream — therefore left `status: running` and
    `session_id: null` on disk for a child that had an id all along, so `agent.list()` after
    the restart could see the row and still never resume what it named. The backend
    announces the id as it arrives and the row is upserted then.
    """
    a = _agent(tmp_path, backend=FakeBackend(announce_early=True, hang=True))

    async def flow() -> dict:
        h = a.spawn("alpha", name="one")
        await asyncio.sleep(0.1)              # announced; nowhere near settled
        row = _row(tmp_path, "one")
        await h.interrupt()                   # settle it so it does not outlive the loop
        return row

    row = asyncio.run(flow())
    assert row["status"] == "running", "the turn must still have been in flight"
    assert row["session_id"] == "fake-alpha", \
        "a kernel death here leaves a row that can never name the child it spawned"


# --- r13 finding 6: a row does not outlive its kernel as "running" --------------------

def test_a_running_row_from_a_dead_kernel_is_reconciled_at_startup(tmp_path):
    """`agents.json` outliving the kernel is the POINT of the file — it is what makes a
    session resumable after a restart — but a row is only ever rewritten by the process
    that owns its handle. A kernel killed under a turn (a restart, the TTL watchdog, a
    crash) therefore left `running` on disk forever, and the fresh kernel reported phantom
    running agents with no handle behind any of them. The reconciliation runs where a
    fresh kernel first loads the registry, and keeps the session id: that id is the whole
    reason the row was kept.
    """
    (tmp_path / "agents.json").write_text(json.dumps(
        [{"name": "midflight", "provider": "claude", "session_id": "sess-9",
          "status": "running", "created_at": 1.0, "task_head": "the long job"},
         {"name": "finished", "provider": "claude", "session_id": "sess-8",
          "status": "done", "created_at": 2.0}]))

    a = _agent(tmp_path, backend=FakeBackend())      # a fresh kernel: no live handles

    row = _row(tmp_path, "midflight")
    assert row["status"] == "orphaned", "a phantom running agent survived the kernel"
    assert row["session_id"] == "sess-9", "the only reference to that session was dropped"
    assert row["task_head"] == "the long job"
    assert _row(tmp_path, "finished")["status"] == "done", "a settled row was rewritten"
    assert [e["status"] for e in a.list() if e["name"] == "midflight"] == ["orphaned"]

    # and the row is still resumable, which is what preserving the id was for
    async def flow():
        h = a.resume("sess-9")
        await asyncio.wait_for(h.result(), 2)
        return h

    assert asyncio.run(flow()).session_id == "sess-9"


def test_reconciliation_is_a_startup_step_and_not_a_sweep(tmp_path):
    """It runs once, where a fresh kernel first loads the registry, and only over what is
    already on disk: a handle spawned afterwards writes `running` and keeps it, which is
    the status a caller needs to be able to trust while a turn really is in flight."""
    (tmp_path / "agents.json").write_text(json.dumps(
        [{"name": "ghost", "provider": "claude", "session_id": "s0", "status": "running",
          "created_at": 1.0}]))
    a = _agent(tmp_path, backend=FakeBackend(hang=True))
    assert _row(tmp_path, "ghost")["status"] == "orphaned"

    async def flow():
        h = a.spawn("slow", name="live-one")
        await asyncio.sleep(0.05)
        assert _row(tmp_path, "live-one")["status"] == "running"
        assert _row(tmp_path, "ghost")["status"] == "orphaned"
        await h.interrupt()
    asyncio.run(flow())


# --- r14 finding 5: a generated handle name must not land on a persisted row -----------

class _CollidingUUID:
    """`uuid4()` that draws the same value once before diverging — a 24-bit collision made
    real rather than waited for."""

    def __init__(self, *hexes):
        self._hexes = list(hexes)

    def __call__(self):
        return type("U", (), {"hex": self._hexes.pop(0)})()


def test_a_generated_name_never_overwrites_a_persisted_rows_session(tmp_path,
                                                                     monkeypatch):
    """`_claim_name` looks only at `_handles`, which a restart empties — while
    `agents.json` survives on purpose, because that file is what makes an old session
    resumable at all. A generated name landing on a persisted row therefore made
    `_registry_write` UPSERT that row and overwrite its `session_id`, deleting the only
    reference to the session it was being kept for. (r12 widened child KERNEL keys; this
    is the handle NAME, a different site with the same 24-bit arithmetic behind it.)

    Nobody asked for this name, so the collision is regenerated rather than raised: an
    explicit name is the caller's choice and keeps its own refusal.
    """
    a = _agent(tmp_path)
    # Both widths of the same draw are already spoken for: the six-hex prefix a narrow
    # generator would have produced, and the twelve-hex name a wide one lands on.
    (tmp_path / "agents.json").write_text(json.dumps(
        [{"name": "agent-aaaaaa", "provider": "claude", "session_id": "precious",
          "status": "done", "created_at": 1},
         {"name": "agent-aaaaaaaaaaaa", "provider": "claude", "session_id": "also-precious",
          "status": "done", "created_at": 2}]))
    monkeypatch.setattr(agents.uuid, "uuid4",
                        _CollidingUUID("aaaaaaaaaaaa0000", "bbbbbbbbbbbb0000"))

    async def flow():
        h = a.spawn("alpha")
        await asyncio.wait_for(h.result(), 5)
        return h

    h = asyncio.run(flow())

    assert _row(tmp_path, "agent-aaaaaa")["session_id"] == "precious"
    assert _row(tmp_path, "agent-aaaaaaaaaaaa")["session_id"] == "also-precious"
    assert _row(tmp_path, "agent-aaaaaaaaaaaa")["status"] == "done"
    assert h.name == "agent-bbbbbbbbbbbb", "the collision was taken rather than redrawn"


def test_generated_suffixes_are_wide_enough_to_make_the_draw_unremarkable(tmp_path):
    """Six hex characters is 24 bits — a birthday collision inside a few thousand handles
    on one long-lived kernel. Twelve is the width `child_key` already uses for the same
    reason, and every generated name here is drawn at it."""
    a = _agent(tmp_path)
    for prefix in ("agent", "fork", "resumed-sess"):
        name = a._generated_name(prefix)
        suffix = name[len(prefix) + 1:]
        assert len(suffix) == 12 and int(suffix, 16) >= 0, name
