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
