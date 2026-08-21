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
    def __init__(self, task, o, *, hang: bool = False, fail: Exception | None = None,
                 send_hang: bool = False, close_hang: bool = False):
        self.session_id = f"fake-{task[:8]}"
        self._task = task
        self.sent: list[str] = []
        self.closed = False
        self.interrupted = False
        self._hang = hang
        self._fail = fail
        self._send_hang = send_hang
        self._close_hang = close_hang
        self._aborted = asyncio.Event()

    async def wait_result(self):
        if self._fail is not None:
            raise self._fail
        if self._hang:
            # S1: an interrupted turn does not raise — it ends with a normal ResultMessage
            # carrying terminal_reason='aborted_streaming'. A driver that is still running
            # when the session is interrupted would therefore settle the handle "done".
            await self._aborted.wait()
            return AgentResult("(aborted)", self.session_id, None, None, 1, 0)
        await asyncio.sleep(0.05)
        return AgentResult(f"did:{self._task}", self.session_id, None, 0.0, 1, 50)

    async def send(self, msg):
        if self._send_hang:
            await asyncio.sleep(3600)
        self.sent.append(msg)
        return AgentResult(f"reply:{msg}", self.session_id, None, 0.0, 1, 10)

    async def interrupt(self):
        self.interrupted = True
        self._aborted.set()
        await asyncio.sleep(0.01)          # S1: a real drain is slow, never instant

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
        s = FakeSession(task or "resumed", o, **self._session_kw)
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


def test_interrupt_settles_once_and_frees_permit(tmp_path):
    b = FakeBackend(hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("forever", name="i1")
        await asyncio.sleep(0.05)                 # let the driver open the session
        assert isinstance(h._driver, asyncio.Task), "driver task must be retained"
        await asyncio.wait_for(h.interrupt(), 5)
        assert h.status == "interrupted"
        assert b.sessions[0].interrupted and b.sessions[0].closed
        assert h._driver.done()
        with pytest.raises(RuntimeError, match="interrupted"):
            await asyncio.wait_for(h.result(), 2)
        # the interrupted turn's permit is back
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"
    assert _row(tmp_path, "i1")["status"] == "interrupted"


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
    b = FakeBackend(send_hang=True)
    a = _agent(tmp_path, backend=b, max_concurrency=1)

    async def flow():
        h = a.spawn("s", name="s1", timeout=0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(h.send("hi"), 2)
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "after"


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
