"""open_session's teardown contract against a fake SDK client — no CLI, no auth.

`connect()` spawns a `claude` process. If anything after it fails, the caller gets an
exception and NO session object, so nothing anywhere holds a handle on that process: the
only chance to reap it is inside open_session itself.
"""
import asyncio

import claude_agent_sdk
import pytest

from ptc.runtime import claude_backend
from ptc.runtime.agents import AgentOpts


class FakeClient:
    def __init__(self, options=None, query_error: BaseException | None = None):
        self.options = options
        self.events: list[str] = []
        self.query_error = query_error

    async def connect(self):
        self.events.append("connect")

    async def query(self, msg):
        self.events.append("query")
        if self.query_error is not None:
            raise self.query_error

    async def disconnect(self):
        self.events.append("disconnect")


def _install(monkeypatch, **kw) -> list:
    made: list[FakeClient] = []

    def factory(options=None):
        c = FakeClient(options=options, **kw)
        made.append(c)
        return c

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", factory)
    return made


def test_open_session_disconnects_when_query_raises(monkeypatch):
    made = _install(monkeypatch, query_error=RuntimeError("CLI died mid-query"))

    with pytest.raises(RuntimeError, match="CLI died mid-query"):
        asyncio.run(claude_backend.open_session("do a thing", AgentOpts()))

    assert made[0].events == ["connect", "query", "disconnect"], (
        "a post-connect failure must not leak the CLI connect() spawned")


def test_open_session_disconnects_when_the_caller_is_cancelled(monkeypatch):
    made = _install(monkeypatch, query_error=asyncio.CancelledError())

    async def flow():
        with pytest.raises(asyncio.CancelledError):
            await claude_backend.open_session("do a thing", AgentOpts())

    asyncio.run(flow())
    assert made[0].events == ["connect", "query", "disconnect"]


def test_open_session_keeps_the_client_on_the_happy_path(monkeypatch):
    made = _install(monkeypatch)

    s = asyncio.run(claude_backend.open_session("do a thing", AgentOpts()))

    assert made[0].events == ["connect", "query"], "a live session must stay connected"
    assert s._client is made[0]
