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


def _until_dead(key: str, patience: float = 30.0) -> bool:
    deadline = time.time() + patience
    while kernel_alive(key) and time.time() < deadline:
        time.sleep(0.5)
    return not kernel_alive(key)


def test_a_heavy_idle_kernel_expires_early_and_the_notice_says_why(ptc_home, monkeypatch):
    """Footprint over PTC_HEAVY_MB and idle past PTC_HEAVY_IDLE_MIN: gone long before its
    24 h TTL, while a light kernel under the same rules stays, and the next attach is told
    it died for its memory."""
    from ptc.discovery import read_meta
    from ptc.kernel import kill_kernel

    monkeypatch.delenv("PTC_IDLE_HOURS", raising=False)
    monkeypatch.setenv("PTC_HEAVY_MB", "400")
    monkeypatch.setenv("PTC_HEAVY_IDLE_MIN", "0.05")         # 3 s
    monkeypatch.setenv("PTC_PRESSURE_MB", "0")               # this machine's state is not ours
    r = asyncio.run(exec_tool(code="blob = b'\\x01' * (600 * 2**20)", session="heavy",
                              timeout_s=60))
    assert "ok" in r[0].text
    r = asyncio.run(exec_tool(code="x = 1", session="light", timeout_s=60))
    assert "ok" in r[0].text
    assert read_meta("heavy")["heavy_mb"] == 400.0

    assert _until_dead("heavy"), "the heavy idle kernel was never expired"
    assert kernel_alive("light"), "a light kernel was expired by the memory rule"
    note = (ptc_home / "kernels" / "heavy" / "expired.marker").read_text()
    assert "of memory" in note, note
    r2 = asyncio.run(exec_tool(code="print('blob' in dir())", session="heavy", timeout_s=60))
    assert "previous kernel expired" in r2[0].text and "of memory" in r2[0].text
    assert "False" in r2[0].text
    kill_kernel("heavy")
    kill_kernel("light")


def test_a_stopped_subagents_kernel_expires_after_the_grace(ptc_home, monkeypatch):
    """The SubagentStop hook marks the finished subagent's kernel and its own watchdog
    expires it after PTC_STOP_GRACE_MIN — well inside the 1 h sub TTL — while a sibling
    subagent's kernel that did not stop is untouched."""
    import json
    import os
    import subprocess
    import types
    from pathlib import Path

    from mcp.server.mcpserver import Context

    import ptc.mcp as mcp_mod
    from ptc.kernel import kill_kernel

    monkeypatch.setenv("PTC_SESSION", "stopbase")
    monkeypatch.setenv("PTC_STOP_GRACE_MIN", "0.05")         # 3 s
    run = ptc_home / "run"
    run.mkdir(parents=True, exist_ok=True)
    for tid, agent in (("toolu_s1", "agent_done"), ("toolu_s2", "agent_busy")):
        (run / f"tooluse-{tid}.json").write_text(json.dumps(
            {"agent_id": agent, "agent_type": "general-purpose", "written_at": 1}))

    def ctx(tid):
        return Context(request_context=types.SimpleNamespace(
            meta={"claudecode/toolUseId": tid}))

    for tid in ("toolu_s1", "toolu_s2"):
        out = asyncio.run(mcp_mod.server.call_tool("exec", {"code": "x = 1"}, ctx(tid)))
        assert "ok" in out.content[0].text
    done, busy = "stopbase--sub-agent_done", "stopbase--sub-agent_busy"
    time.sleep(4)                       # past the grace: nothing happens without a stop
    assert kernel_alive(done) and kernel_alive(busy)

    hook = Path(__file__).resolve().parents[2] / "hooks" / "subagent_stop.py"
    r = subprocess.run(["python3", str(hook)], input=json.dumps({"agent_id": "agent_done"}),
                       text=True, capture_output=True, timeout=20,
                       env={**os.environ, "PTC_HOME": str(ptc_home)})
    assert r.returncode == 0, r.stderr

    assert _until_dead(done), "the stopped subagent's kernel outlived its grace"
    assert kernel_alive(busy), "a subagent that did not stop lost its kernel"
    note = (ptc_home / "kernels" / done / "expired.marker").read_text()
    assert "subagent stopped" in note, note
    kill_kernel(busy)
