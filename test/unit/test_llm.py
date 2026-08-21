"""llm() against a fake _run_once — no SDK, no auth, no network.

Covers the plan-specified contract (delegates to claude_backend.run_once with bare_llm=True,
json_schema round-trips through structured output) and the deadline: a hung call must not
strand the shared semaphore's permit past its own timeout.
"""
import asyncio

import pytest

from ptc.runtime import llm as llm_mod
from ptc.runtime.state import STATE


def test_llm_calls_backend_bare(monkeypatch, tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8, "depth": 0, "max_depth": 1}
    seen = {}

    async def fake_run_once(task, o, *, resume=None, fork=False, bare_llm=False):
        seen.update(task=task, model=o.model, system=o.system, bare=bare_llm,
                    schema=o.output_schema)
        from ptc.runtime.agents import AgentResult
        return AgentResult('{"a": 1}', "s", {"a": 1} if o.output_schema else None, 0, 1, 5)

    monkeypatch.setattr(llm_mod, "_run_once", fake_run_once)
    out = asyncio.run(llm_mod.llm("classify this", model="sonnet", system="be terse"))
    assert out == '{"a": 1}' and seen["bare"] and seen["model"] == "sonnet"
    assert seen["task"] == "classify this" and seen["system"] == "be terse"
    out2 = asyncio.run(llm_mod.llm("classify", json_schema={"type": "object"}))
    assert out2 == {"a": 1}


def test_llm_default_system_prompt_is_terse(monkeypatch, tmp_path):
    """No `system=` given: llm() supplies its own terse default rather than inheriting
    whatever a full agent turn would (M2 spec: `llm()` is a lightweight single-shot call,
    not a conversational agent)."""
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    seen = {}

    async def fake_run_once(task, o, *, resume=None, fork=False, bare_llm=False):
        seen["system"] = o.system
        from ptc.runtime.agents import AgentResult
        return AgentResult("ok", "s", None, 0, 1, 5)

    monkeypatch.setattr(llm_mod, "_run_once", fake_run_once)
    asyncio.run(llm_mod.llm("x"))
    assert seen["system"] and "concise" in seen["system"].lower()


def test_llm_timeout(monkeypatch, tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}

    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(llm_mod, "_run_once", slow)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(llm_mod.llm("x", timeout=0.05))


def test_llm_timeout_releases_the_shared_permit(monkeypatch, tmp_path):
    """A timed-out llm() call must not strand the shared semaphore: with a bound of 1,
    the next agent.run() would hang forever if the permit leaked (mirrors T20's
    test_run_timeout_releases_permit for the agent namespace's own semaphore)."""
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 1, "depth": 0, "max_depth": 1}

    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(llm_mod, "_run_once", slow)

    from ptc.runtime.agents import AgentResult, _Agent

    class FakeBackend:
        async def run_once(self, task, o, *, resume=None, fork=False):
            return AgentResult(f"did:{task}", None, None, 0.0, 1, 10)

    a = _Agent(STATE.config, {"claude": FakeBackend()})

    async def flow():
        with pytest.raises(asyncio.TimeoutError):
            await llm_mod.llm("x", timeout=0.05)
        return await asyncio.wait_for(a.run("after"), 2)
    assert asyncio.run(flow()).text == "did:after"
