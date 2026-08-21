from ptc.cells import current_cell, read_output_since, read_record
from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_exec_writes_log_and_record(ptc_home):
    ensure_kernel("c1", cwd=str(ptc_home))
    kc = KernelClient("c1")
    out = kc.exec_cell("x = 40 + 2\nprint('hello', x)\nx", timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed)
    assert "hello 42" in out.output
    rec = read_record("c1", out.cell_id)
    assert rec.status == "ok" and rec.result_repr == "42"
    assert 0 <= rec.duration_ms < 60_000     # a real elapsed span, not a since-boot counter
    text, off = read_output_since("c1", out.cell_id, 0)
    assert "hello 42" in text and off > 0
    # state persists across cells
    out2 = kc.exec_cell("print(x + 1)", timeout_s=60, config=Config.from_env())
    assert "43" in out2.output and out2.cell_id == out.cell_id + 1
    kill_kernel("c1")


def test_cell_id_alignment(ptc_home):
    """F1 guard: execute_input id == current.json == log == record == audit cell.
    If an IPython release changes hook/count ordering, THIS fails — fix _cell_no."""
    ensure_kernel("ca1", cwd=str(ptc_home))
    kc = KernelClient("ca1")
    out = kc.exec_cell("print('align')", timeout_s=60, config=Config.from_env())
    n = out.cell_id
    cells = ptc_home / "kernels" / "ca1" / "cells"
    assert (cells / f"{n}.log").exists() and "align" in (cells / f"{n}.log").read_text()
    assert read_record("ca1", n) is not None
    assert current_cell("ca1") == n
    kill_kernel("ca1")


def test_error_cell_records_error(ptc_home):
    ensure_kernel("c2", cwd=str(ptc_home))
    kc = KernelClient("c2")
    out = kc.exec_cell("1/0", timeout_s=60, config=Config.from_env())
    rec = read_record("c2", out.cell_id)
    assert rec.status == "error" and rec.error["ename"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in out.output      # IPython traceback went to the log
    assert current_cell("c2") == out.cell_id       # current.json points at last cell (done)
    kill_kernel("c2")


def test_a_result_whose_repr_raises_still_settles_the_cell(ptc_home):
    """`repr()` is user code. When it raised, the terminal record was never written, so
    current.json went on naming a cell that would never get one — and every later
    submission reported the kernel busy until somebody restarted it."""
    ensure_kernel("c9", cwd=str(ptc_home))
    kc = KernelClient("c9")
    out = kc.exec_cell(
        "class Hostile:\n"
        "    def __repr__(self): raise ValueError('no repr for you')\n"
        "Hostile()",
        timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed), out

    rec = read_record("c9", out.cell_id)
    assert rec is not None, "the cell never got a terminal record"
    assert "repr raised ValueError: no repr for you" in (rec.result_repr or "")

    nxt = kc.exec_cell("print('admitted')", timeout_s=60, config=Config.from_env())
    assert isinstance(nxt, Completed) and "admitted" in nxt.output
    kill_kernel("c9")
