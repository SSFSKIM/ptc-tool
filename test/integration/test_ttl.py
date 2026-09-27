"""Watchdog TTL end-to-end through the client/MCP path (T16).

test_watchdog_epoch.py (T5) already proves the watchdog itself: idle expiry via raw
ensure_kernel(), the in-flight guard sparing a busy cell, and cell-id monotonicity
across a restart. This file's job is the piece those tests don't cover: that the
*mcp.exec_tool* path — what a real MCP client actually sees — surfaces the
"previous kernel expired" notice text (T8) on the next call after the watchdog fires,
prepended to a fresh, empty namespace.
"""
import asyncio
import time

from ptc.kernel import kernel_alive
from ptc.mcp import exec_tool
from ptc.paths import Config


def test_ttl_expiry_and_notice(ptc_home, monkeypatch):
    monkeypatch.setenv("PTC_IDLE_HOURS", "0.0006")     # ~2.2 s
    cfg = Config.from_env()
    assert cfg.idle_hours == 0.0006
    r = asyncio.run(exec_tool(code="ttl_x = 1", session="t1", timeout_s=60))
    assert "ok" in r[0].text
    deadline = time.time() + 30
    while kernel_alive("t1") and time.time() < deadline:
        time.sleep(0.5)
    assert not kernel_alive("t1"), "watchdog never fired"
    assert (ptc_home / "kernels" / "t1" / "expired.marker").exists()
    r2 = asyncio.run(exec_tool(code="print('ttl_x' in dir())", session="t1", timeout_s=60))
    assert "previous kernel expired" in r2[0].text and "False" in r2[0].text


def test_a_subagent_keyed_kernel_gets_the_short_ttl(ptc_home, monkeypatch):
    """A subagent's kernel is spawned under min(idle_hours, sub_idle_hours) and expires on
    it, while its parent's kernel — same adapter, same environment — keeps the long TTL."""
    import json
    import types

    from mcp.server.mcpserver import Context

    import ptc.mcp as mcp_mod
    from ptc.discovery import read_meta
    from ptc.kernel import kill_kernel

    monkeypatch.setenv("PTC_SESSION", "ttlbase")
    monkeypatch.delenv("PTC_IDLE_HOURS", raising=False)
    monkeypatch.setenv("PTC_SUB_IDLE_HOURS", "0.0006")     # ~2.2 s
    (ptc_home / "run").mkdir(parents=True, exist_ok=True)
    (ptc_home / "run" / "tooluse-toolu_ttl.json").write_text(json.dumps(
        {"agent_id": "agent_ttl", "agent_type": "general-purpose", "written_at": 1}))

    def ctx(tid):
        return Context(request_context=types.SimpleNamespace(
            meta={"claudecode/toolUseId": tid}))

    sub_key = "ttlbase--sub-agent_ttl"
    sub = asyncio.run(mcp_mod.server.call_tool("exec", {"code": "x = 1"}, ctx("toolu_ttl")))
    assert "ok" in sub.content[0].text
    main = asyncio.run(mcp_mod.server.call_tool("exec", {"code": "y = 1"}, ctx("toolu_main")))
    assert "ok" in main.content[0].text
    assert read_meta(sub_key)["idle_hours"] == 0.0006
    assert read_meta("ttlbase")["idle_hours"] == 24.0

    deadline = time.time() + 30
    while kernel_alive(sub_key) and time.time() < deadline:
        time.sleep(0.5)
    assert not kernel_alive(sub_key), "the subagent kernel's watchdog never fired"
    assert (ptc_home / "kernels" / sub_key / "expired.marker").exists()
    assert kernel_alive("ttlbase"), "the parent's kernel took the subagent TTL"
    kill_kernel("ttlbase")
