"""`via=` resolution, the option-compatibility table, deadlines across routes, and the host
route's structured output — `llm()` and `agent.*` against a fake host (`host_fakes.py`)
and a fake CLI backend. No `claude` process, no model call.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from host_fakes import FakeLauncher

from ptc.runtime import _llm as llm_mod
from ptc.runtime import host as host_mod
from ptc.runtime import routes
from ptc.runtime.agents import AgentFailed, AgentResult, _Agent
from ptc.runtime.host import AgentHost, HostUnavailable
from ptc.runtime.profiles import LLM_SYSTEM, READER_SYSTEM, Profile
from ptc.runtime.state import STATE


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "routes", "depth": 0, "max_depth": 1, "max_concurrency": 8,
                    "cwd": str(tmp_path)}
    return tmp_path


def install_host(kernel, monkeypatch, launcher=None, **kw) -> tuple[AgentHost, FakeLauncher]:
    launcher = launcher or FakeLauncher()
    kw.setdefault("start_s", 5.0)
    h = AgentHost(kernel_dir=kernel, cwd=str(kernel), plugin_dir=kernel / "plugin",
                  launch=launcher, **kw)
    monkeypatch.setattr(host_mod, "_HOST", h)
    return h, launcher


class CliBackend:
    """The CLI route, faked: records each call and answers from the call's own cwd."""

    def __init__(self):
        self.calls: list = []

    async def run_once(self, task, o, *, resume=None, fork=False, bare_llm=False):
        self.calls.append((task, o, bare_llm))
        text = f"cli:{task}"
        if o.cwd and (Path(o.cwd) / "same.txt").exists():
            text = (Path(o.cwd) / "same.txt").read_text()
        structured = {"answer": 42} if o.output_schema else None
        return AgentResult(text, "cli-session", structured, 0.01, 1, 5)


@pytest.fixture
def cli(monkeypatch):
    b = CliBackend()
    monkeypatch.setattr(llm_mod, "_run_once", b.run_once)
    return b


def agent(kernel, cli) -> _Agent:
    return _Agent(STATE.config, {"claude": cli, "codex": cli})


# ---- llm() --------------------------------------------------------------------------------


def test_llm_takes_the_host_under_auto(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            assert await llm_mod.llm("hi") == "echo:hi"
            [job] = launcher.mod.jobs
            assert job["kind"] == "complete" and job["model"] == "haiku"
            # None: the most a host completion takes, as the CLI route takes its own cap
            assert job["system"] == LLM_SYSTEM and job["maxTokens"] is None
            assert job["effort"] is None and cli.calls == []
        finally:
            await h.close()
    asyncio.run(main())
    rec = [json.loads(x) for x in (kernel / "audit.jsonl").read_text().splitlines()]
    assert rec[-1]["kind"] == "llm" and rec[-1]["via"] == "host"


def test_llm_carries_its_options_to_the_host(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            await llm_mod.llm("hi", model="sonnet", system="be brief", max_tokens=99,
                              effort="low")
            [job] = launcher.mod.jobs
            assert (job["model"], job["system"], job["maxTokens"], job["effort"]) == \
                ("sonnet", "be brief", 99, "low")
        finally:
            await h.close()
    asyncio.run(main())


def test_llm_via_cli_and_with_the_host_off_takes_the_cli(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            assert await llm_mod.llm("a", via="cli") == "cli:a"
            h._off = True
            assert await llm_mod.llm("b") == "cli:b"
            with pytest.raises(HostUnavailable, match="PTC_HOST=off"):
                await llm_mod.llm("c", via="host")
            assert launcher.procs == []
        finally:
            await h.close()
    asyncio.run(main())
    assert [c[0] for c in cli.calls] == ["a", "b"]


def test_llm_with_claude_codes_system_prompt_is_cli_only(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            p = Profile("claude_code", [])
            assert await llm_mod.llm("x", profile=p) == "cli:x"
            with pytest.raises(ValueError, match="via='host'.*claude_code"):
                await llm_mod.llm("x", profile=p, via="host")
            assert launcher.procs == []                   # the host was never started
        finally:
            await h.close()
    asyncio.run(main())


def test_an_unknown_via_is_refused(kernel, monkeypatch, cli):
    with pytest.raises(ValueError, match="via"):
        asyncio.run(llm_mod.llm("x", via="sideways"))


def test_max_tokens_is_checked(kernel, cli):
    for bad in (0, -3, 1.5, True):
        with pytest.raises(ValueError, match="max_tokens"):
            asyncio.run(llm_mod.llm("x", max_tokens=bad))


def test_an_explicit_max_tokens_caps_the_reply_on_both_routes(kernel, monkeypatch, cli):
    """The CLI route takes it as the child's `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, the host as
    the completion's `maxTokens`; a cap the host cannot take sends the call to the CLI."""
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            await llm_mod.llm("a", max_tokens=500)
            assert launcher.mod.jobs[-1]["maxTokens"] == 500
            await llm_mod.llm("b", max_tokens=500, via="cli")
            await llm_mod.llm("c", max_tokens=100_000)               # past 64,000: CLI
            with pytest.raises(ValueError, match="via='host'.*64,000"):
                await llm_mod.llm("d", max_tokens=100_000, via="host")
            await llm_mod.llm("e", via="cli")
        finally:
            await h.close()
    asyncio.run(main())
    assert [(c[0], c[1].max_output_tokens) for c in cli.calls] == \
        [("b", 500), ("c", 100_000), ("e", None)]


def test_a_reply_cut_at_its_cap_fails_naming_the_cap(kernel, monkeypatch, cli):
    """The CLI route ends a cut reply with an API error naming the cap; the mod reports a
    cut as `max-tokens` in the same words, and the partial reply rides the failure."""
    async def cut(mod, job):
        return {"ok": False, "reason": "max-tokens", "text": "The sea is",
                "detail": "API Error: Claude's response exceeded the 40 output token maximum."}

    async def main():
        h, _ = install_host(kernel, monkeypatch, launcher=FakeLauncher(handler=cut))
        try:
            with pytest.raises(AgentFailed, match="exceeded the 40 output token maximum") as ei:
                await llm_mod.llm("x", max_tokens=40)
            assert ei.value.result.text == "The sea is" and ei.value.result.via == "host"
        finally:
            await h.close()
    asyncio.run(main())


def test_a_host_that_does_not_start_sends_auto_to_the_cli_with_the_remainder(
        kernel, monkeypatch, cli):
    seen = []
    real = llm_mod.guarded

    async def spy(sem, make, timeout):
        seen.append(timeout)
        return await real(sem, make, timeout)
    monkeypatch.setattr(llm_mod, "guarded", spy)

    class SlowFail:
        async def __call__(self, env, cwd, plugin_dir):
            await asyncio.sleep(1.0)
            raise RuntimeError("the engine refused the plugin")

    async def main():
        h, _ = install_host(kernel, monkeypatch, launcher=SlowFail())
        try:
            assert await llm_mod.llm("x", timeout=3.0) == "cli:x"
            with pytest.raises(HostUnavailable, match="did not start"):
                await llm_mod.llm("y", via="host", timeout=3.0)
        finally:
            await h.close()
    asyncio.run(main())
    assert len(seen) == 1 and 1.5 < seen[0] < 2.1, seen


def test_a_fallback_reached_with_its_budget_spent_raises_timeout(kernel, monkeypatch, cli):
    class BlockingFail:
        async def __call__(self, env, cwd, plugin_dir):
            time.sleep(0.35)  # noqa: ASYNC251 — past the deadline without yielding, on purpose
            raise RuntimeError("no")

    async def main():
        h, _ = install_host(kernel, monkeypatch, launcher=BlockingFail())
        try:
            with pytest.raises(TimeoutError):
                await llm_mod.llm("x", timeout=0.3)
        finally:
            await h.close()
    asyncio.run(main())
    assert cli.calls == []
    assert routes.remaining(time.monotonic() - 5) == 0.0 and routes.remaining(None) is None


def test_timeout_zero_is_a_timeout_on_both_routes_and_negative_is_refused(
        kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            with pytest.raises(TimeoutError):
                await llm_mod.llm("x", timeout=0)
            with pytest.raises(TimeoutError):
                await llm_mod.llm("x", timeout=0, via="cli")
            await h.ensure_started()
            with pytest.raises(TimeoutError):
                await llm_mod.llm("x", timeout=0, via="host")
            for via in ("auto", "host", "cli"):
                with pytest.raises(ValueError, match="non-negative"):
                    await llm_mod.llm("x", timeout=-1, via=via)
            assert launcher.mod.jobs == [] and cli.calls == []
        finally:
            await h.close()
    asyncio.run(main())


# ---- llm(json_schema=...) on the host ------------------------------------------------------

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
          "additionalProperties": False}


def replies(*texts):
    queue = list(texts)

    async def handler(mod, job):
        return {"ok": True, "text": queue.pop(0)}
    return handler


def test_schema_calls_take_the_host_and_parse_a_fenced_reply(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(
            kernel, monkeypatch, launcher=FakeLauncher(handler=replies('```json\n{"n": 3}\n```')))
        try:
            assert await llm_mod.llm("three", json_schema=SCHEMA) == {"n": 3}
            [job] = launcher.mod.jobs
            assert job["system"].startswith(LLM_SYSTEM)
            assert ("Reply with one JSON value that validates against this JSON Schema, "
                    "and nothing else:\n") in job["system"]
            assert json.dumps(SCHEMA) in job["system"]
        finally:
            await h.close()
    asyncio.run(main())
    assert cli.calls == []


def test_a_reply_that_does_not_validate_is_retried_once_with_the_error(
        kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(
            kernel, monkeypatch, launcher=FakeLauncher(handler=replies('{"n": "x"}', '{"n": 4}')))
        try:
            assert await llm_mod.llm("four", json_schema=SCHEMA) == {"n": 4}
            first, second = launcher.mod.jobs
            assert first["prompt"] == "four"
            assert second["prompt"].startswith("four") and "'x' is not of type 'integer'" \
                in second["prompt"] and '{"n": "x"}' in second["prompt"]
        finally:
            await h.close()
    asyncio.run(main())


def test_a_second_bad_reply_raises_agent_failed_carrying_it(kernel, monkeypatch, cli):
    async def main():
        h, _ = install_host(kernel, monkeypatch,
                            launcher=FakeLauncher(handler=replies("nope", "still not json")))
        try:
            with pytest.raises(AgentFailed, match="did not validate") as ei:
                await llm_mod.llm("x", json_schema=SCHEMA)
            assert ei.value.result.text == "still not json"
        finally:
            await h.close()
    asyncio.run(main())


def test_an_invalid_schema_is_a_caller_error(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            with pytest.raises(ValueError, match="JSON Schema"):
                await llm_mod.llm("x", json_schema={"type": "no-such-type"})
            assert launcher.procs == [] or launcher.mod.jobs == []
        finally:
            await h.close()
    asyncio.run(main())


# ---- agent.run / agent.spawn ---------------------------------------------------------------


def test_a_reader_run_takes_the_host(kernel, monkeypatch, cli):
    async def answer(mod, job):
        return {"ok": True, "text": "12", "agentId": "abc123", "usage": {"input_tokens": 9}}

    async def main():
        h, launcher = install_host(kernel, monkeypatch, launcher=FakeLauncher(handler=answer))
        try:
            r = await agent(kernel, cli).run("count", profile="reader", model="haiku")
            [job] = launcher.mod.jobs
            assert job["kind"] == "agent" and job["prompt"] == "count"
            assert job["type"] == {"prompt": READER_SYSTEM, "tools": ["Read", "Grep", "Glob"],
                                   "model": "haiku", "effort": None,
                                   "permissionMode": "bypassPermissions", "maxTurns": 50}
            assert job["description"] == "ptc agent"
            assert (r.text, r.via, r.agent_id, r.session_id) == ("12", "host", "abc123",
                                                                 "host-session")
            assert r.cost_usd is None and r.num_turns is None and r.structured is None
            [row] = [x for x in agent(kernel, cli).list() if x.get("provider") == "claude-host"]
            assert row["status"] == "done" and row["agent_id"] == "abc123"
            assert row["session_id"] == "host-session"
            assert row["transcript"].endswith(
                "/-fake-cwd/host-session/subagents/agent-abc123.jsonl")
        finally:
            await h.close()
    asyncio.run(main())
    assert cli.calls == []


def test_max_turns_and_effort_ride_the_type(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            await agent(kernel, cli).run("t", profile="reader", max_turns=3, effort="high")
            t = launcher.mod.jobs[0]["type"]
            assert (t["maxTurns"], t["effort"], t["model"]) == (3, "high", None)
        finally:
            await h.close()
    asyncio.run(main())


def test_memory_rides_the_types_prompt(kernel, monkeypatch, cli):
    (kernel / "CLAUDE.md").write_text("Project rule: be careful.")

    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            await agent(kernel, cli).run(
                "t", profile=Profile(READER_SYSTEM, ["Read"], memory="project"))
            prompt = launcher.mod.jobs[0]["type"]["prompt"]
            assert prompt.startswith(READER_SYSTEM) and "Project rule: be careful." in prompt
        finally:
            await h.close()
    asyncio.run(main())


@pytest.mark.parametrize(("kw", "why"), [
    ({}, "tools='default'"),                                     # agent.run's default: full
    ({"profile": "worker"}, "claude_code"),
    ({"profile": "reader", "tools": ["Read", "Agent"]}, "'Agent'"),
    ({"profile": "reader", "tools": ["Read", "Task"]}, "'Task'"),
    ({"profile": "reader", "tools": ["mcp__ptc__exec"]}, "'mcp__ptc__exec'"),
    ({"profile": Profile(READER_SYSTEM, ["Read"], settings="user")}, "settings='user'"),
    ({"profile": "reader", "output_schema": {"type": "object"}}, "output_schema"),
    ({"profile": "reader", "permission_mode": "acceptEdits"}, "permission_mode='acceptEdits'"),
    ({"profile": "reader", "provider": "codex"}, "provider='codex'"),
])
def test_calls_only_the_cli_can_serve(kernel, monkeypatch, cli, kw, why):
    """Under auto they take the CLI; under via='host' they raise naming the reason."""
    if kw.get("provider") == "codex":
        kw = {"provider": "codex"}

    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            a = agent(kernel, cli)
            r = await a.run("t", **kw)
            assert r.via == "cli"
            with pytest.raises(ValueError, match=f"via='host'.*{why}"):
                await a.run("t", via="host", **kw)
            assert launcher.procs == []
        finally:
            await h.close()
    asyncio.run(main())
    assert len(cli.calls) == 1


def test_output_schema_under_auto_runs_on_the_cli_with_structured(kernel, monkeypatch, cli):
    async def main():
        h, _ = install_host(kernel, monkeypatch)
        try:
            r = await agent(kernel, cli).run("t", profile="reader",
                                             output_schema={"type": "object"})
            assert r.structured == {"answer": 42} and r.via == "cli"
        finally:
            await h.close()
    asyncio.run(main())


def test_readers_in_two_directories_each_answer_from_their_own(kernel, monkeypatch, cli):
    """A spawn's `cwd` does not move a host subagent (M1, Q9), so a `cwd` other than the
    kernel's goes to the CLI route, where each child runs where it was told."""
    a_dir, b_dir = kernel / "a", kernel / "b"
    for d, text in ((a_dir, "from a"), (b_dir, "from b")):
        d.mkdir()
        (d / "same.txt").write_text(text)

    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            ag = agent(kernel, cli)
            ra, rb = await asyncio.gather(
                ag.run("read same.txt", profile="reader", cwd=str(a_dir)),
                ag.run("read same.txt", profile="reader", cwd=str(b_dir)))
            assert (ra.text, rb.text) == ("from a", "from b")
            assert ra.via == rb.via == "cli"
            with pytest.raises(ValueError, match="via='host'.*cwd="):
                await ag.run("x", profile="reader", cwd=str(a_dir), via="host")
            # the kernel's own directory is no reason to leave the host
            r = await ag.run("here", profile="reader", cwd=str(kernel))
            assert r.via == "host" and [j["prompt"] for j in launcher.mod.jobs] == ["here"]
        finally:
            await h.close()
    asyncio.run(main())


def test_spawn_under_auto_always_takes_the_cli(kernel, monkeypatch, cli):
    assert routes.choose("auto", None, spawn=True) == "cli"
    assert routes.choose("cli", None) == "cli"
    with pytest.raises(ValueError, match="via='host'.*because"):
        routes.choose("host", "because")


def test_a_host_spawn_settles_its_handle_and_takes_no_follow_ups(kernel, monkeypatch, cli):
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            ag = agent(kernel, cli)
            handle = ag.spawn("job", profile="reader", via="host")
            r = await handle.result()
            assert r.via == "host" and handle.status == "done"
            assert [j["kind"] for j in launcher.mod.jobs] == ["agent"]
            assert handle.provider == "claude-host"
            with pytest.raises(RuntimeError, match="follow-up"):
                await handle.send("more")
            [row] = [x for x in ag.list() if x["name"] == handle.name]
            assert row["provider"] == "claude-host" and row["status"] == "done"
        finally:
            await h.close()
    asyncio.run(main())


def test_interrupting_a_host_spawn_cancels_its_job(kernel, monkeypatch, cli):
    from host_fakes import hold

    async def main():
        h, launcher = install_host(kernel, monkeypatch, launcher=FakeLauncher(handler=hold))
        try:
            handle = agent(kernel, cli).spawn("job", profile="reader", via="host")
            while not (launcher.procs and launcher.mod.jobs):
                await asyncio.sleep(0.01)
            await handle.interrupt()
            assert handle.status == "interrupted"
            jid = launcher.mod.jobs[0]["id"]
            while jid not in launcher.mod.cancelled:
                await asyncio.sleep(0.01)
        finally:
            await h.close()
    asyncio.run(main())


def test_a_host_run_failure_surfaces_and_marks_its_row(kernel, monkeypatch, cli):
    async def fail(mod, job):
        return {"ok": False, "reason": "aborted", "detail": "stopped", "agentId": "z"}

    async def main():
        h, _ = install_host(kernel, monkeypatch, launcher=FakeLauncher(handler=fail))
        try:
            ag = agent(kernel, cli)
            with pytest.raises(AgentFailed, match="aborted") as ei:
                await ag.run("t", profile="reader")
            assert ei.value.result.agent_id == "z" and ei.value.result.via == "host"
            assert ei.value.result.session_id == "host-session"
            [row] = [x for x in ag.list() if x.get("provider") == "claude-host"]
            assert row["status"] == "error" and row["agent_id"] == "z"
            assert row["transcript"].endswith("/-fake-cwd/host-session/subagents/agent-z.jsonl")
        finally:
            await h.close()
    asyncio.run(main())


def test_a_run_timeout_on_the_host_is_a_timeout(kernel, monkeypatch, cli):
    from host_fakes import hold

    async def main():
        h, _ = install_host(kernel, monkeypatch, launcher=FakeLauncher(handler=hold))
        try:
            with pytest.raises(TimeoutError):
                await agent(kernel, cli).run("t", profile="reader", timeout=0.3)
            with pytest.raises(ValueError, match="non-negative"):
                await agent(kernel, cli).run("t", profile="reader", timeout=-2)
        finally:
            await h.close()
    asyncio.run(main())


def test_a_call_is_audited_once_with_the_route_it_ran_on(kernel, monkeypatch, cli):
    """A host whose generation dies while the call waits for a slot, and cannot start
    again, sends the call to the CLI: the record says cli, once."""
    async def main():
        h, launcher = install_host(kernel, monkeypatch)
        try:
            await h.ensure_started()
            real = h.submit

            async def gone(job, deadline, **kw):
                h._looped = "test"                    # the host is off from here on
                raise HostUnavailable("crash-looped")
            monkeypatch.setattr(h, "submit", gone)
            assert await llm_mod.llm("fell") == "cli:fell"
            assert (await agent(kernel, cli).run("fell too", profile="reader")).via == "cli"
            monkeypatch.setattr(h, "submit", real)
        finally:
            await h.close()
    asyncio.run(main())
    rec = [json.loads(x) for x in (kernel / "audit.jsonl").read_text().splitlines()]
    assert [(r["kind"], r["via"]) for r in rec] == [("llm", "cli"), ("agent", "cli")]


def test_a_schema_call_is_audited_once_though_it_retries(kernel, monkeypatch, cli):
    async def main():
        h, _ = install_host(kernel, monkeypatch,
                            launcher=FakeLauncher(handler=replies('{"n": "x"}', '{"n": 1}')))
        try:
            assert await llm_mod.llm("one", json_schema=SCHEMA) == {"n": 1}
        finally:
            await h.close()
    asyncio.run(main())
    rec = [json.loads(x) for x in (kernel / "audit.jsonl").read_text().splitlines()]
    assert [(r["kind"], r["via"]) for r in rec] == [("llm", "host")]
