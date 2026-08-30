"""The adapter's half of the caller correlation (issue #1: subagents contending for one
kernel).

The host stamps every `tools/call` with `claudecode/toolUseId` in the request `_meta`, and
the PreToolUse hook files the caller's agent identity under that same id. Nothing else in
the request distinguishes a subagent's call from its parent's — they share one stdio
connection — so this id is the whole of what the handlers have to pass on to discovery.

Fakes throughout: `Context` is the SDK's request-scoped object and the handlers only ever
read one attribute chain off it.
"""
import asyncio
import types

import pytest

import ptc.mcp as mcp


def _ctx(meta):
    return types.SimpleNamespace(request_context=types.SimpleNamespace(meta=meta))


def _meta(d):
    return types.SimpleNamespace(model_dump=lambda: d)


def test_the_call_id_is_read_out_of_request_meta():
    assert mcp._tool_use_id(_ctx(_meta({"claudecode/toolUseId": "toolu_1"}))) == "toolu_1"


def test_every_missing_piece_of_the_meta_chain_is_simply_no_id():
    """Fail-open is the whole contract of this path: a host that does not send the stamp,
    an SDK that shapes the context differently, a meta that will not dump — each means
    "unknown caller", which resolves the way it resolves today. None of them is an error
    the caller should see, because the call itself is perfectly fine."""
    class Exploding:
        def model_dump(self):
            raise RuntimeError("not dumpable")

    for ctx in (None,
                _ctx(None),
                _ctx(_meta({})),
                _ctx(_meta({"other": "field"})),
                _ctx(Exploding()),
                types.SimpleNamespace(),
                types.SimpleNamespace(request_context=None)):
        assert mcp._tool_use_id(ctx) is None, ctx


def test_the_context_parameter_is_not_part_of_any_tool_schema():
    """`ctx` is injected by the SDK, not supplied by the model. If it ever surfaced in the
    input schema, every caller would be shown a parameter it cannot fill and some would try
    — the tool contract must read exactly as it did before the correlation existed."""
    tools = asyncio.run(mcp.server.list_tools())
    for t in tools:
        assert "ctx" not in t.input_schema.get("properties", {}), t.name
        assert "ctx" not in t.input_schema.get("required", []), t.name


class _Stop(Exception):
    """Ends the handler at the point this test is about — the resolve call."""


@pytest.mark.parametrize("call", [
    lambda: mcp.exec_tool("1+1", ctx=_ctx(_meta({"claudecode/toolUseId": "toolu_9"}))),
    lambda: mcp.wait_tool(1, ctx=_ctx(_meta({"claudecode/toolUseId": "toolu_9"}))),
    lambda: mcp.interrupt_tool(ctx=_ctx(_meta({"claudecode/toolUseId": "toolu_9"}))),
    lambda: mcp.restart_tool(ctx=_ctx(_meta({"claudecode/toolUseId": "toolu_9"}))),
])
def test_every_session_resolving_handler_names_its_caller(monkeypatch, call):
    """The correlation is only as good as its thinnest handler: a tool that resolves a
    session without passing the id keys its subagent callers to the parent's kernel, which
    is the contention this whole mechanism exists to end."""
    seen: list = []

    def recorder(*a, **kw):
        seen.append((a, kw))
        raise _Stop

    monkeypatch.setattr(mcp, "_resolve", recorder)
    with pytest.raises(_Stop):
        asyncio.run(call())

    assert seen and seen[0][1].get("tool_use_id") == "toolu_9", seen


def test_a_handler_called_without_a_context_asks_for_no_overlay(monkeypatch):
    """Nothing about the id is required: a direct call (a test, a host that stamps no
    `_meta`) passes no context at all, and the handler must still resolve — with `None`,
    which is discovery's own instruction to leave the base key alone."""
    seen: list = []

    def recorder(*a, **kw):
        seen.append(kw)
        raise _Stop

    monkeypatch.setattr(mcp, "_resolve", recorder)
    with pytest.raises(_Stop):
        asyncio.run(mcp.exec_tool("1+1"))

    assert seen and seen[0]["tool_use_id"] is None
