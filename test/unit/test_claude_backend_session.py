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


# --- r7 finding 1: a failed terminal result is not an answer --------------------------
#
# The CLI reports a rate limit, a max-turns stop or an execution failure IN BAND: the
# stream ends on the same ResultMessage a successful turn ends on, with `is_error` set and
# the error text sitting where the answer would be. Read as a normal end, "Claude AI usage
# limit reached" became the answer and the handle went `done`.

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

from ptc.runtime.agents import AgentFailed  # noqa: E402


def _result(*, is_error: bool, subtype: str = "success", text: str | None = None,
            terminal_reason: str | None = None) -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=10, duration_api_ms=8,
                         is_error=is_error, num_turns=1, session_id="sess-1",
                         result=text, terminal_reason=terminal_reason)


def _install_query(monkeypatch, messages: list) -> None:
    async def fake_query(*, prompt, options):
        for m in messages:
            yield m
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)


def test_run_once_raises_on_a_failed_terminal_result(monkeypatch):
    _install_query(monkeypatch, [
        AssistantMessage(content=[TextBlock(text="I got part way")], model="sonnet"),
        _result(is_error=True, subtype="error_max_turns",
                text="Claude AI usage limit reached|1234567890"),
    ])

    with pytest.raises(AgentFailed) as e:
        asyncio.run(claude_backend.run_once("classify this", AgentOpts()))

    assert "error_max_turns" in str(e.value)
    assert "usage limit reached" in str(e.value), "the error text must reach the caller"
    # the partial rides along — it is still readable, it just is not an answer
    assert e.value.result.text == "I got part way"
    assert e.value.result.session_id == "sess-1"


def test_run_once_returns_normally_on_a_successful_terminal_result(monkeypatch):
    _install_query(monkeypatch, [
        AssistantMessage(content=[TextBlock(text="the answer")], model="sonnet"),
        _result(is_error=False, text="the answer"),
    ])

    r = asyncio.run(claude_backend.run_once("q", AgentOpts()))
    assert r.text == "the answer" and r.session_id == "sess-1"


def test_run_once_keeps_the_partial_of_an_interrupted_turn(monkeypatch):
    """S1's interrupt contract is the one is_error terminal that is NOT a failure: an
    interrupted turn ends normally with terminal_reason='aborted_streaming', and handing
    that partial back is exactly what `AgentHandle.interrupt` exists to do."""
    _install_query(monkeypatch, [
        AssistantMessage(content=[TextBlock(text="half a thought")], model="sonnet"),
        _result(is_error=True, subtype="error_during_execution",
                terminal_reason="aborted_streaming"),
    ])

    r = asyncio.run(claude_backend.run_once("q", AgentOpts()))
    assert r.text == "half a thought" and r.session_id == "sess-1"


class _StreamingClient(FakeClient):
    """A connected session whose turn ends on the messages the test hands it."""

    def __init__(self, options=None, messages=()):
        super().__init__(options=options)
        self._messages = list(messages)

    async def receive_response(self):
        for m in self._messages:
            yield m


def test_a_session_turn_surfaces_a_failed_terminal_result(monkeypatch):
    msgs = [_result(is_error=True, subtype="error_during_execution",
                    text="Execution error")]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient",
                        lambda options=None: _StreamingClient(options, msgs))

    async def flow():
        s = await claude_backend.open_session("go", AgentOpts())
        with pytest.raises(AgentFailed, match="Execution error"):
            await s.wait_result()
        # the id of a failed turn is still the id its transcript is filed under
        assert s.session_id == "sess-1"

    asyncio.run(flow())


def test_a_session_turn_keeps_an_interrupted_partial(monkeypatch):
    msgs = [AssistantMessage(content=[TextBlock(text="partial")], model="sonnet"),
            _result(is_error=True, terminal_reason="aborted_streaming")]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient",
                        lambda options=None: _StreamingClient(options, msgs))

    async def flow():
        s = await claude_backend.open_session("go", AgentOpts())
        return await s.wait_result()

    assert asyncio.run(flow()).text == "partial"
