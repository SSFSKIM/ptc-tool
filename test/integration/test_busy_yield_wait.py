import multiprocessing as mp
import os
import time

from ptc.client import Busy, Completed, KernelClient, Running
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_yield_wait_busy_interrupt(ptc_home):
    ensure_kernel("y1", cwd=str(ptc_home))
    cfg = Config.from_env()
    kc = KernelClient("y1")
    out = kc.exec_cell("import time\nprint('start', flush=True)\ntime.sleep(600)", timeout_s=3, config=cfg)
    assert isinstance(out, Running) and "start" in out.output

    # busy: a second exec must NOT queue
    out2 = KernelClient("y1").exec_cell("1+1", timeout_s=3, config=cfg)
    assert isinstance(out2, Busy) and out2.cell_id == out.cell_id

    # wait from a FRESH client object (fresh-adapter recovery), caller-held cursor
    w = KernelClient("y1").wait_cell(out.cell_id, timeout_s=2, since=out.next_offset)
    assert isinstance(w, Running) and w.output == ""      # no new output while sleeping

    kc.interrupt()
    w2 = KernelClient("y1").wait_cell(out.cell_id, timeout_s=15)
    assert isinstance(w2, Completed) and w2.record.status == "interrupted"

    out3 = KernelClient("y1").exec_cell("print(6*7)", timeout_s=30, config=cfg)
    assert isinstance(out3, Completed) and "42" in out3.output
    kill_kernel("y1")


def _submit(home: str, q) -> None:
    os.environ["PTC_HOME"] = home
    from ptc.client import KernelClient
    from ptc.paths import Config
    try:
        out = KernelClient("y4").exec_cell("import time; time.sleep(30)",
                                           timeout_s=1, config=Config.from_env())
        q.put(type(out).__name__)
    except BaseException as e:                       # a silently queued submit lands here
        q.put(f"{type(e).__name__}: {e}")


def test_two_simultaneous_execs_one_runs_one_is_told_busy(ptc_home):
    """The submit lock is the linearization point (F2): it is held from the busy check
    until current.json names OUR cell, so two racing client PROCESSES can only resolve
    to one running cell plus one caller TOLD busy. Move the check out from under the
    lock and both submit — the loser's request is then silently queued behind the
    running cell and its exec_cell dies on `no execute_input` instead."""
    ensure_kernel("y4", cwd=str(ptc_home))
    q = mp.Queue()
    ps = [mp.Process(target=_submit, args=(str(ptc_home), q)) for _ in range(2)]
    [p.start() for p in ps]
    kinds = sorted(q.get(timeout=120) for _ in ps)
    [p.join(timeout=30) for p in ps]
    assert kinds == ["Busy", "Running"], kinds
    kill_kernel("y4")


def test_wait_on_archived_epoch_cell(ptc_home):
    """F3: a cell yielded before a restart settles from the archive, never from a
    new epoch's cell — ids are monotonic across epochs."""
    from ptc.kernel import restart_kernel
    ensure_kernel("y3", cwd=str(ptc_home))
    cfg = Config.from_env()
    out = KernelClient("y3").exec_cell("import time\nprint('old-epoch', flush=True)\ntime.sleep(600)",
                                       timeout_s=2, config=cfg)
    assert isinstance(out, Running)
    old_id = out.cell_id
    restart_kernel("y3")
    w = KernelClient("y3").wait_cell(old_id, timeout_s=5)
    assert isinstance(w, Completed)
    assert "previous kernel epoch" in w.output and "old-epoch" in w.output
    # monotonic: the new epoch's first user cell id is above the archived max
    out2 = KernelClient("y3").exec_cell("print('new')", timeout_s=30, config=cfg)
    assert out2.cell_id > old_id
    kill_kernel("y3")


def test_wait_on_dead_kernel_reports_kernel_died(ptc_home):
    info = ensure_kernel("y2", cwd=str(ptc_home))
    kc = KernelClient("y2")
    out = kc.exec_cell("import time; time.sleep(600)", timeout_s=2, config=Config.from_env())
    assert isinstance(out, Running)
    os.kill(info.pid, 9)
    time.sleep(0.5)
    w = KernelClient("y2").wait_cell(out.cell_id, timeout_s=3)
    assert isinstance(w, Completed) and w.record.error["ename"] == "KernelDied"
