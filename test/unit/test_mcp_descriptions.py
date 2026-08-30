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
    assert set(desc) == {"exec", "wait", "interrupt", "restart", "kernels"}
    for name, text in desc.items():
        assert len(text) > 40, f"{name} has no real description: {text!r}"


def test_exec_description_carries_the_call_time_traps():
    text = _descriptions()["exec"]
    assert "await" in text, "the missed-await trap is the most-hit live failure"
    assert "session" in text, "parallel callers must learn session isolation here"
    assert "timeout=" in text and "timeout_s" in text, \
        "the kernel/tool timeout naming split is a documented live trap"


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
