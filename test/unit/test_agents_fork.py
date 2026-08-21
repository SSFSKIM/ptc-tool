"""Fork/resume plumbing, narrowly: meta.json lookup, the no-session error, and a
resume-then-send round trip. The lifecycle contract (permits, teardown, interrupt,
registry rows) is test_agents.py's job — this file is only the T21-specific gaps:
`fork()`'s dependency on meta.json (T13) rather than an explicit session id, and its
refusal when no `claude_session_id` is known for the kernel's key.
"""
import asyncio

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_agents import FakeBackend  # reuse (same directory)

from ptc.runtime.agents import _Agent
from ptc.runtime.state import STATE


def _agent_with_meta(tmp_path, monkeypatch, sid):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    from ptc.discovery import write_meta
    STATE.kernel_dir = tmp_path / "kernels" / "k"
    STATE.kernel_dir.mkdir(parents=True)
    STATE.config = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 2}
    if sid:
        write_meta("k", claude_session_id=sid)
    return _Agent(STATE.config, {"claude": FakeBackend()})


def test_fork_uses_meta_session(tmp_path, monkeypatch):
    a = _agent_with_meta(tmp_path, monkeypatch, "real-uuid-1")
    r = asyncio.run(a.fork("what marker?"))
    assert r.text == "forked:what marker?" and r.session_id == "real-uuid-1"


def test_fork_errors_without_claude_session(tmp_path, monkeypatch):
    a = _agent_with_meta(tmp_path, monkeypatch, None)
    with pytest.raises(RuntimeError, match="no claude_session_id known"):
        asyncio.run(a.fork("x"))


def test_resume_then_send(tmp_path, monkeypatch):
    a = _agent_with_meta(tmp_path, monkeypatch, None)

    async def flow():
        h = a.resume("sess-9")
        await h.result()
        return await h.send("follow")
    r = asyncio.run(flow())
    assert r.text == "reply:follow"
