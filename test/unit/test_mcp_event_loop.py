"""The MCP handlers must leave the stdio event loop free (final review F3).

The client library is synchronous — blocking flock, poll-and-sleep follow loops — and the
handlers used to call it inline, so a single in-flight exec/wait froze the whole adapter
for the full timeout. An interrupt sent to stop that very call could not be dispatched
until the call had already finished on its own, which is exactly when it is useless.

Fakes only: the point under test is scheduling, so no kernel is spawned.
"""
import asyncio
import threading
import time

import ptc.mcp as mcp_mod
from ptc.cells import CellRecord
from ptc.client import Completed

BLOCK_S = 1.0


def test_interrupt_dispatches_while_a_wait_is_in_flight(monkeypatch):
    entered = threading.Event()

    def slow_wait(self, cell_id, timeout_s, since):
        entered.set()
        time.sleep(BLOCK_S)                      # a blocking client call, as in real life
        return Completed(cell_id, CellRecord("ok", 1, None, None, [], []), "done\n")

    signalled: list[float] = []

    monkeypatch.setattr(mcp_mod.KernelClient, "wait_cell", slow_wait)
    monkeypatch.setattr(mcp_mod.KernelClient, "is_busy", lambda self: None)
    monkeypatch.setattr(mcp_mod.KernelClient, "interrupt",
                        lambda self: signalled.append(time.monotonic()))

    async def flow():
        t0 = time.monotonic()
        waiting = asyncio.ensure_future(mcp_mod.wait_tool(cell_id=1, session="loop1",
                                                          timeout_s=30))
        await asyncio.to_thread(entered.wait, 5)
        assert entered.is_set(), "the wait never started"

        r = await asyncio.wait_for(mcp_mod.interrupt_tool(session="loop1"), 5)
        elapsed = time.monotonic() - t0

        assert not waiting.done(), "the wait finished first — nothing was proven"
        assert elapsed < BLOCK_S, (
            f"the interrupt waited {elapsed:.2f}s for the blocked wait to return")
        assert signalled, "the interrupt never reached the kernel"
        await waiting
        return r

    r = asyncio.run(flow())
    assert "interrupt sent to kernel loop1" in r[0].text
