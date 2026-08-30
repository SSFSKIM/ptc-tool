"""End to end, through the server's own dispatch: a mapped caller gets its own kernel.

The unit tests either side of this one fake something — the context on one side, the
mapping on the other. What neither can show is that the SDK actually HANDS the handler a
context when a real call is dispatched: if it did not (or if `ctx` were treated as an
ordinary argument), `_tool_use_id` would return None on every live call and the whole
correlation would be inert while every test above it still passed.

Dispatch path as in test_mcp_dispatch.py: `MCPServer.call_tool(name, arguments, context)`
is exactly what the stdio handler calls once it has built a Context from the request.
"""
import asyncio
import json
import types

from mcp.server.mcpserver import Context

import ptc.mcp as mcp_mod
from ptc.kernel import kill_kernel


def _ctx(tool_use_id: str) -> Context:
    """A Context shaped like the one `_handle_call_tool` builds: the host's per-call
    `claudecode/toolUseId` sitting in the request's `_meta`."""
    meta = types.SimpleNamespace(model_dump=lambda: {"claudecode/toolUseId": tool_use_id})
    return Context(request_context=types.SimpleNamespace(meta=meta))


def test_a_subagents_call_gets_a_kernel_the_parents_call_cannot_see(ptc_home, monkeypatch):
    """The incident this whole mechanism is for: parent and subagent call the same server
    over the same connection under one session key, and the subagent's variables land in
    the parent's namespace. With the hook's mapping in place the two calls resolve to two
    kernels — the parent's cannot see what the subagent defined."""
    monkeypatch.setenv("PTC_SESSION", "dispatchbase")
    run = ptc_home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "tooluse-toolu_sub.json").write_text(json.dumps(
        {"agent_id": "agent_7", "agent_type": "general-purpose", "written_at": 1}))

    sub = asyncio.run(mcp_mod.server.call_tool(
        "exec", {"code": "TOKEN = 'the subagent'"}, _ctx("toolu_sub")))
    assert "[cell" in sub.content[0].text and "ok" in sub.content[0].text

    # the parent's own call: same session, no mapping (the hook writes none for the main
    # thread), so it keys off the base rung exactly as it always did
    main = asyncio.run(mcp_mod.server.call_tool(
        "exec", {"code": "print(globals().get('TOKEN'))"}, _ctx("toolu_main")))

    assert "None" in main.content[0].text, \
        f"the parent saw the subagent's namespace: {main.content[0].text!r}"
    assert (ptc_home / "kernels" / "dispatchbase--sub-agent_7").is_dir()
    assert (ptc_home / "kernels" / "dispatchbase").is_dir()
    assert not (run / "tooluse-toolu_sub.json").exists(), "the mapping was never consumed"

    kill_kernel("dispatchbase--sub-agent_7")
    kill_kernel("dispatchbase")
