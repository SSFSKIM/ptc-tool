import asyncio

from ptc.kernel import kill_kernel
from ptc.mcp import exec_tool, interrupt_tool, kernels_tool, restart_tool, wait_tool


def _run(coro):
    return asyncio.run(coro)


def test_exec_wait_interrupt_roundtrip(ptc_home):
    r = _run(exec_tool(code="x = 21\nprint('a' * 10)", session="m1", timeout_s=60))
    assert "[cell" in r[0].text and "ok" in r[0].text and "aaaaaaaaaa" in r[0].text

    r2 = _run(exec_tool(code="import time; time.sleep(300)", session="m1", timeout_s=2))
    assert "running" in r2[0].text
    cell_id = int(r2[0].text.split("cell ")[1].split(" ")[0])

    r3 = _run(exec_tool(code="1", session="m1", timeout_s=2))
    assert "busy" in r3[0].text and "Nothing was queued" in r3[0].text

    _run(interrupt_tool(session="m1"))
    r4 = _run(wait_tool(cell_id=cell_id, session="m1", timeout_s=15))
    assert "interrupted" in r4[0].text

    r5 = _run(exec_tool(code="print(x * 2)", session="m1", timeout_s=60))
    assert "42" in r5[0].text

    rows = _run(kernels_tool())
    assert "m1" in rows[0].text

    r6 = _run(restart_tool(session="m1"))
    assert "restart" in r6[0].text.lower()
    r7 = _run(exec_tool(code="print('x' in dir())", session="m1", timeout_s=60))
    assert "False" in r7[0].text            # namespace really was lost
    kill_kernel("m1")


def test_interrupt_returns_the_interrupted_cells_tail(ptc_home):
    """The spec promises `interrupt` returns the interrupted cell's tail. A static ack
    ("[interrupt sent]") threw away the one thing the caller needed — which cell stopped,
    whether it actually stopped, and what it had printed before it did."""
    r = _run(exec_tool(code="import time\nprint('before the interrupt', flush=True)\ntime.sleep(300)",
                       session="m3", timeout_s=5))
    assert "running" in r[0].text
    cell_id = int(r[0].text.split("cell ")[1].split(" ")[0])

    out = _run(interrupt_tool(session="m3"))[0].text

    assert "[interrupt sent to kernel m3]" in out       # the ack survives
    assert f"[cell {cell_id} · interrupted" in out, out  # settled, and says which cell
    assert "KeyboardInterrupt" in out
    kill_kernel("m3")


def test_interrupt_with_nothing_running_says_so(ptc_home):
    _run(exec_tool(code="1", session="m4", timeout_s=60))
    out = _run(interrupt_tool(session="m4"))[0].text
    assert out == "[interrupt sent to kernel m4] — no cell was running"
    kill_kernel("m4")


def test_truncation_and_clamp(ptc_home):
    r = _run(exec_tool(code="print('y' * 100_000)", session="m2",
                       timeout_s=60, max_output_chars=999_999))
    text = r[0].text
    assert len(text) < 60_000               # server clamp at 50k held
    assert "[truncated" in text and ".log" in text
    kill_kernel("m2")
