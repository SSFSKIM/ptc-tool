"""The tool schema is the one doctrine channel every caller gets.

The skill is read only when someone invokes it — subagents never do — and the server
instructions are a discovery digest. What is guaranteed to sit in front of ANY caller at
call time is the tool's own description, so the call-time contracts live there: the traps
that live usage keeps hitting (a missed `await` silently returning a coroutine object,
`timeout=` in the kernel vs `timeout_s=` on the tools, parallel callers contending for one
kernel) and the parameters whose semantics the schema alone cannot carry (`until`, `since`).
The SDK ships `fn.__doc__` as the description, so these assertions pin the docstrings.
"""
import asyncio

from ptc.mcp import server


def _descriptions() -> dict[str, str]:
    tools = asyncio.run(server.list_tools())
    return {t.name: t.description or "" for t in tools}


def test_every_tool_ships_a_nonempty_description():
    desc = _descriptions()
    assert set(desc) == {"exec", "wait", "interrupt", "restart", "kernels", "peek"}
    for name, text in desc.items():
        assert len(text) > 40, f"{name} has no real description: {text!r}"


def test_exec_description_carries_the_call_time_traps():
    text = _descriptions()["exec"]
    assert "await" in text, "the missed-await trap is the most-hit live failure"
    assert "session" in text, "parallel callers must learn session isolation here"
    assert "timeout=" in text and "timeout_s" in text, \
        "the kernel/tool timeout naming split is a documented live trap"


def test_exec_offers_queue_in_both_the_schema_and_the_description():
    """The parameter is what makes the wait callable; the sentence is what makes a caller
    facing `busy` know it exists — the schema alone cannot say the budget is timeout_s."""
    tools = asyncio.run(server.list_tools())
    exec_tool = next(t for t in tools if t.name == "exec")
    assert "queue" in exec_tool.input_schema["properties"]
    assert "queue=True" in (exec_tool.description or "")


def test_wait_description_defines_until_and_since():
    text = _descriptions()["wait"]
    assert "until" in text and "since" in text
    assert "regex" in text, "until= takes a Python regex; the schema type alone says str"


def test_instructions_point_at_the_skill():
    """The always-on channel names where the deep doctrine lives. Server instructions are
    injected into the system prompt even when the tools themselves are deferred (observed
    live), so this is the one place a caller who never browses skills is still told that
    ptc:ptc exists and when to open it."""
    from ptc.mcp import INSTRUCTIONS
    assert "ptc:ptc" in INSTRUCTIONS
    assert "skill" in INSTRUCTIONS.lower()


def test_peek_description_carries_the_contract():
    from ptc import mcp
    doc = mcp.peek_tool.__doc__
    for token in ("busy", "repr", "restart()", "no calls"):
        assert token in doc, token


def test_instructions_mention_peek_and_queue():
    from ptc.mcp import INSTRUCTIONS
    assert "peek" in INSTRUCTIONS and "queue=True" in INSTRUCTIONS


def test_pre_peek_message_is_exact(monkeypatch, tmp_path):
    """A kernel spawned before peek existed has no socket, and the only fix is a restart.
    `PeekUnavailable` is also what a dead socket raises, so the text names the build it
    was asked about — the reader can tell "old build" from "wrong kernel"."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    import ptc.mcp as mcp
    monkeypatch.setattr("ptc.kernel.kernel_alive", lambda key: True)
    monkeypatch.setattr("ptc.discovery.read_meta", lambda key: {"build": "abc123def456"})
    from ptc.peek_client import PeekUnavailable
    def raise_unavailable(key, expr):
        raise PeekUnavailable("no socket")
    monkeypatch.setattr("ptc.peek_client.peek_kernel", raise_unavailable)
    text = mcp._peek_text("k", "x")
    assert text == "[kernel build abc123def456 predates peek — restart() to upgrade]"
