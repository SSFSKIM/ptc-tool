import json
import multiprocessing as mp
import os
import time

import pytest

from ptc.client import Busy, Completed, KernelClient, Running
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_yield_wait_busy_interrupt(ptc_home):
    ensure_kernel("y1", cwd=str(ptc_home))
    cfg = Config.from_env()
    kc = KernelClient("y1")
    out = kc.exec_cell("import time\nprint('start', flush=True)\ntime.sleep(600)", timeout_s=3, config=cfg)
    assert isinstance(out, Running) and "start" in out.output

    # the yield seeded the cursor sidecar: a wait that carries no cursor of its own
    # resumes after what the yield already delivered instead of replaying it
    w0 = KernelClient("y1").wait_cell(out.cell_id, timeout_s=1)
    assert isinstance(w0, Running) and w0.output == "" and w0.next_offset == out.next_offset

    # busy: a second exec must NOT queue
    out2 = KernelClient("y1").exec_cell("1+1", timeout_s=3, config=cfg)
    assert isinstance(out2, Busy) and out2.cell_id == out.cell_id
    assert out2.reason == "running"

    # wait from a FRESH client object (fresh-adapter recovery), caller-held cursor
    w = KernelClient("y1").wait_cell(out.cell_id, timeout_s=2, since=out.next_offset)
    assert isinstance(w, Running) and w.output == ""      # no new output while sleeping

    t0 = time.monotonic()
    kc.interrupt()
    w2 = KernelClient("y1").wait_cell(out.cell_id, timeout_s=15)
    assert isinstance(w2, Completed) and w2.record.status == "interrupted"
    # the control channel did the interrupting: interrupt() waits a 2 s grace before
    # falling back to SIGINT, so a settle inside that window cannot be the fallback's
    assert time.monotonic() - t0 < 2.0, "interrupt fell through to the SIGINT fallback"

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


def test_unconfirmed_submit_fails_closed(ptc_home):
    """F2 fail-closed: a kernel that accepts a request but never publishes current.json
    leaves a cell nobody can see. exec_cell refuses to pretend otherwise — it gives up
    on its confirm budget, records the unacknowledged submission, and every later busy
    check reports busy off that marker rather than admitting a second cell.

    The swallowed cell is a LONG one on purpose. The marker is discharged by evidence
    (r2 finding 4), never by age, and a swallowed cell that finishes on its own produces
    the strongest evidence there is — its terminal record. Keeping this one running is
    what makes the refusal below mean something, and interrupting it at the end is what
    proves the door reopens on evidence rather than on a clock.
    """
    ensure_kernel("y5", cwd=str(ptc_home))
    cfg = Config.from_env()
    kc = KernelClient("y5")
    # take away the kernel's ability to publish current.json — precisely the fault the
    # confirm loop exists for, and the only way to reach it without a wedged kernel
    silence = kc.exec_cell(
        "import ptc.runtime.bootstrap as _b\n"
        "from IPython import get_ipython\n"
        "get_ipython().events.unregister('pre_run_cell', _b._pre_run_cell)",
        timeout_s=30, config=cfg)
    assert isinstance(silence, Completed) and silence.record.status == "ok"

    with pytest.raises(RuntimeError, match="never published it"):
        kc.exec_cell("import time; time.sleep(600)", timeout_s=5, config=cfg)
    pend = ptc_home / "kernels" / "y5" / "cells" / "pending.json"
    assert pend.exists()
    # the kernel got as far as execute_input, so the marker names the cell it swallowed
    stranded = json.loads(pend.read_text())["cell_id"]
    assert stranded is not None
    assert KernelClient("y5").is_busy() == Busy(stranded, reason="pending-unconfirmed")
    assert KernelClient("y5").exec_cell("2+2", timeout_s=5, config=cfg) == Busy(
        stranded, reason="pending-unconfirmed")

    # evidence, not the clock: the swallowed cell's terminal record is what reopens
    # admission, and interrupting it is how that record finally lands
    KernelClient("y5").interrupt()
    deadline = time.monotonic() + 30
    while KernelClient("y5").is_busy() is not None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert KernelClient("y5").is_busy() is None
    assert not pend.exists(), "a discharged marker must be consumed"
    kill_kernel("y5")


def test_submit_that_dies_before_acknowledgement_fails_closed(ptc_home, monkeypatch):
    """Fail-closed covers the whole sent-unacknowledged window, not only the confirm
    loop. Once kc.execute() has put the request on the wire the kernel may run it, so
    every later failure — a lost execute_input, a torn-down channel, a Ctrl-C — has to
    leave the marker behind; without it the next caller admits a second cell on top."""
    ensure_kernel("y6", cwd=str(ptc_home))
    cfg = Config.from_env()

    def never_acknowledged(self, kc, msg_id, timeout=15.0):
        raise TimeoutError("kernel never acknowledged the cell (no execute_input)")

    monkeypatch.setattr(KernelClient, "_await_cell_id", never_acknowledged)
    with pytest.raises(TimeoutError):
        KernelClient("y6").exec_cell("1+1", timeout_s=5, config=cfg)
    pend = ptc_home / "kernels" / "y6" / "cells" / "pending.json"
    assert pend.exists()
    assert json.loads(pend.read_text())["cell_id"] is None    # id never learned

    # let the cell the kernel really did accept finish, so the busy verdict below can
    # only be coming from the marker
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and KernelClient("y6").is_busy().reason == "running":
        time.sleep(0.05)
    # still patched: a Busy here proves the second caller never reached the wire
    assert KernelClient("y6").exec_cell("2+2", timeout_s=5, config=cfg) == Busy(
        -1, reason="pending-unconfirmed")
    kill_kernel("y6")


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
    assert "previous kernel epoch" in w.output
    # ...and NOT the log line the yield above already handed THIS caller. A restart renames
    # `cells/` with its `offsets/` inside it, so this cursor did not vanish, it moved — and
    # the archived read looks it up where it went (r11 finding 4). Until then every epoch
    # rotation reset every long-lived adapter to 0 and replayed what it had already served,
    # which is the one thing the per-caller cursor exists to prevent.
    assert "old-epoch" not in w.output
    # the archived log itself is intact, and an explicit cursor still outranks all of it
    assert "old-epoch" in KernelClient("y3").wait_cell(old_id, timeout_s=5, since=0).output
    # the archive honors the cursor too: settling it advanced the sidecar, so a repeat
    # wait re-states which epoch the cell belongs to without replaying its log
    again = KernelClient("y3").wait_cell(old_id, timeout_s=5)
    assert isinstance(again, Completed)
    assert "previous kernel epoch" in again.output and "old-epoch" not in again.output
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


def test_exec_whose_cell_kills_the_kernel_settles_instead_of_yielding(ptc_home):
    """The initial follow polled only for a record.

    A cell that takes the kernel down with it — os._exit here, a segfault or an OOM kill
    in the field — can never write one, so the follow burned the whole yield budget (300 s
    by default) and then reported the cell as Running: two lies for the price of one wait.
    wait_cell has made the liveness check since F3; this path owes the same answer.
    """
    ensure_kernel("y6", cwd=str(ptc_home))
    t0 = time.monotonic()
    out = KernelClient("y6").exec_cell(
        "import os\nprint('bye', flush=True)\nos._exit(9)", timeout_s=60,
        config=Config.from_env())
    elapsed = time.monotonic() - t0

    assert isinstance(out, Completed), f"still following a dead kernel after {elapsed:.1f}s"
    assert out.record.error["ename"] == "KernelDied"
    assert "bye" in out.output
    assert elapsed < 20, f"the follow blocked on a dead kernel for {elapsed:.1f}s"
