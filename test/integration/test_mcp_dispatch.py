"""Dispatch-path regression tests for the MCP adapter (T8 review finding: the
existing test_mcp_tools.py calls exec_tool/wait_tool/etc. directly — a thin async
pass-through — never through the MCP server's own dispatch machinery, so a
structured_content regression or a fabricated-id regression in that machinery
would go unnoticed).

Real dispatch path used here: MCPServer.call_tool(name, arguments, context=None)
(mcp 2.0.0, mcp/server/mcpserver/server.py). The low-level protocol handler that a
stdio client's CallToolRequest actually reaches is wired as
`Server(..., on_call_tool=self._handle_call_tool, ...)`; `_handle_call_tool` does
nothing but build a `Context` from the incoming JSON-RPC params and then
`return await self.call_tool(params.name, params.arguments or {}, context)` — the
exact method called below. Calling it directly on the same module-level `server`
instance `ptc-mcp` runs exercises the real tool lookup, argument validation, tool
body, and result conversion (mcp/server/mcpserver/utilities/func_metadata.py); only
the outer JSON-RPC (de)serialization is skipped, which carries no dispatch logic
of its own.
"""
import asyncio

import ptc.mcp as mcp_mod
from ptc.client import Busy
from ptc.kernel import kill_kernel
from ptc.paths import Config
from ptc.shape import render


def _run(coro):
    return asyncio.run(coro)


def test_exec_dispatch_round_trip_has_no_structured_content(ptc_home):
    """A real exec round trip through server.call_tool: structured_content stays
    None/absent (structured_output=False held), and content carries the rendered
    header as its first text item."""
    result = _run(mcp_mod.server.call_tool("exec", {"code": "1+1", "session": "d1"}))

    assert result.structured_content is None

    assert isinstance(result.content, list) and len(result.content) > 0
    first = result.content[0]
    assert isinstance(first, mcp_mod.TextContent)
    assert "[cell" in first.text and "ok" in first.text  # the rendered header

    kill_kernel("d1")


def test_busy_reasons_reach_the_wire_without_fabricated_ids(ptc_home, monkeypatch):
    """The other two Busy reasons — "lock-held" with no id at all, and
    "pending-unconfirmed" carrying the wedged -1 sentinel — reach the wire as
    shape.render's exact wording, with structured_content still None and no
    fabricated "None"/"-1" cell id in the text."""

    def _lock_held(self, code, timeout_s, config):
        return Busy(None, "lock-held")

    monkeypatch.setattr(mcp_mod.KernelClient, "exec_cell", _lock_held)
    r1 = _run(mcp_mod.server.call_tool("exec", {"code": "1", "session": "d2"}))
    assert r1.structured_content is None
    text1 = r1.content[0].text
    assert text1 == render(Busy(None, "lock-held"), "d2", Config.from_env()).text
    assert "None" not in text1
    assert "-1" not in text1

    def _pending_sentinel(self, code, timeout_s, config):
        return Busy(-1, "pending-unconfirmed")

    monkeypatch.setattr(mcp_mod.KernelClient, "exec_cell", _pending_sentinel)
    r2 = _run(mcp_mod.server.call_tool("exec", {"code": "1", "session": "d2"}))
    assert r2.structured_content is None
    text2 = r2.content[0].text
    assert text2 == render(Busy(-1, "pending-unconfirmed"), "d2", Config.from_env()).text
    assert "None" not in text2
    assert "-1" not in text2

    kill_kernel("d2")
