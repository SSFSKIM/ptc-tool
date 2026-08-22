"""Whose state an implicit (`since=-1`) wait cursor is.

No kernel here: a cell with a terminal record and a log settles from the filesystem alone,
which is exactly the surface the cursor lives on.
"""
import json
import multiprocessing as mp
import os

from ptc.client import Completed, KernelClient
from ptc.paths import cells_dir, secure_dir

_RECORD = {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
           "images": [], "mutations": []}


def _seed(key: str, cell_id: int, text: str) -> None:
    d = secure_dir(cells_dir(key))
    (d / f"{cell_id}.log").write_text(text)
    (d / f"{cell_id}.json").write_text(json.dumps(_RECORD))


def _wait_in_another_process(home: str, q) -> None:
    os.environ["PTC_HOME"] = home
    from ptc.client import KernelClient as KC
    out = KC("c1").wait_cell(9, timeout_s=10)
    q.put((os.getpid(), out.output))


def test_two_implicit_waiters_in_different_processes_each_see_the_whole_output(
        monkeypatch, tmp_path):
    """The MCP adapter and a `ptc wait` on the same cell both waited with no cursor of
    their own, and both read and advanced ONE kernel-global sidecar: whichever ran first
    consumed the output and the other was silently handed a gap it could never ask for
    again. An implicit cursor is caller state, so it is kept per caller."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed("c1", 9, "the whole output\n")

    first = KernelClient("c1").wait_cell(9, timeout_s=10)
    assert isinstance(first, Completed) and "the whole output" in first.output

    q = mp.Queue()
    p = mp.Process(target=_wait_in_another_process, args=(str(tmp_path), q))
    p.start()
    try:
        pid, text = q.get(timeout=120)
    finally:
        p.join(30)
        if p.is_alive():
            p.kill()
    assert pid != os.getpid(), "the second waiter was not a second process"
    assert "the whole output" in text, "the first waiter consumed the second's output"


def test_one_caller_still_resumes_where_it_left_off(monkeypatch, tmp_path):
    """Per-caller must not mean per-call: within one caller the cursor still advances, so
    a second implicit wait is not a replay of everything already delivered (T6)."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed("c2", 4, "first delivery\n")

    kc = KernelClient("c2")
    assert "first delivery" in kc.wait_cell(4, timeout_s=10).output
    assert kc.wait_cell(4, timeout_s=10).output == "", "already-delivered output was replayed"

    (cells_dir("c2") / "4.log").write_text("first delivery\nsecond delivery\n")
    assert kc.wait_cell(4, timeout_s=10).output == "second delivery\n"


def test_a_forked_child_does_not_inherit_its_parents_cursor(monkeypatch, tmp_path):
    """A fork copies globals, so a cursor identity computed before the fork came out of
    the child unchanged — and on Linux, where `multiprocessing` forks by default, that is
    the ordinary case rather than an exotic one. Parent and child then read and advance
    one sidecar: whichever waits first consumes the output and the other is handed the gap
    per-caller cursors exist to prevent. The identity is recomputed when the pid moves.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed("c4", 5, "the whole output\n")

    kc = KernelClient("c4")
    assert "the whole output" in kc.wait_cell(5, timeout_s=10).output   # parent consumes it
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                                    # child: never returns to pytest
        try:
            os.write(w, KernelClient("c4").wait_cell(5, timeout_s=10).output.encode())
        finally:
            os._exit(0)
    os.close(w)
    seen = os.read(r, 4096).decode()
    os.close(r)
    os.waitpid(pid, 0)

    assert "the whole output" in seen, "the forked child inherited the parent's cursor"


def test_an_epoch_rotation_does_not_replay_what_this_caller_already_read(monkeypatch,
                                                                        tmp_path):
    """A restart renames `cells/` — every caller's cursor sidecar inside it — and puts a
    fresh empty `cells/` in its place, so `default_offset` answered 0 for a long-lived
    adapter that had already been served this cell. `wait(cell_id, since=-1)` then replayed
    the whole cell out of the archive: the per-caller cursor contract, broken by the one
    event that moves the cursor out from under it.

    The archive keeps its own `offsets/` beside its logs, under the same per-caller
    filename, and that is what the archived read consults when this caller has no LIVE
    cursor. A live cursor still wins — the live path always does.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed("c6", 8, "already delivered\n")

    kc = KernelClient("c6")
    assert "already delivered" in kc.wait_cell(8, timeout_s=10).output

    from ptc.kernel import _rotate_cells
    from ptc.paths import kernel_dir
    _rotate_cells(kernel_dir("c6"))
    secure_dir(cells_dir("c6") / "offsets")

    out = kc.wait_cell(8, timeout_s=10)
    assert isinstance(out, Completed) and "previous kernel epoch" in out.output
    assert "already delivered" not in out.output, \
        "the epoch rotation replayed output this caller had already been served"


def test_an_explicit_cursor_is_still_the_cross_process_contract(monkeypatch, tmp_path):
    """`since=` is what one process hands another (every `Running` render prints it), and
    it must keep meaning the same offset regardless of which caller carries it."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed("c3", 2, "one\ntwo\n")

    out = KernelClient("c3").wait_cell(2, timeout_s=10, since=len("one\n"))
    assert out.output == "two\n"


def test_an_immediately_completed_exec_advances_the_cursor(monkeypatch, tmp_path):
    """A cell that finishes inside the exec yield has its output handed straight to the
    caller — but only the Running exit saved the offset, so the sidecar stayed unseeded and
    a later `wait(cell_id=…)` with no `since` from the same adapter replayed the whole cell.
    `since=-1` means "resume after what this adapter last served", whichever exit served it.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed("c5", 3, "all of it\n")

    kc = KernelClient("c5")
    done = kc._follow(3, timeout_s=5)
    assert isinstance(done, Completed) and "all of it" in done.output
    assert kc.wait_cell(3, timeout_s=5).output == "", "the cursorless wait replayed the cell"


# --- r15 finding 4: since=0 is an explicit request for the whole log ------------------

def _archived_with_a_cursor(key: str, cell_id: int, text: str, cursor: int):
    """An archived cell plus the archived copy of THIS caller's cursor sidecar — what a
    restart leaves behind for a caller that had already been served part of the log."""
    from ptc.cells import offset_name
    from ptc.paths import kernel_dir

    d = kernel_dir(key) / "cells-prev-1"
    (d / "offsets").mkdir(parents=True)
    (d / f"{cell_id}.log").write_text(text)
    (d / f"{cell_id}.json").write_text(json.dumps(_RECORD))
    (d / "offsets" / offset_name(cell_id)).write_text(str(cursor))
    secure_dir(cells_dir(key))


def test_an_explicit_since_zero_reads_the_archived_log_from_the_top(monkeypatch, tmp_path):
    """Zero is an offset, not an absence.

    `since=0` is a caller asking for the whole cell from byte zero — a fresh reader, a
    retry after a lost render, an operator reading a log somebody else already consumed.
    The archived-cursor consult tested it for truthiness, so an explicit 0 fell through to
    the sidecar the previous epoch left behind and the caller was handed the tail with no
    way to ask for the rest. Only the implicit form (`since=-1`, "resume after what I was
    last served") has a cursor to look up.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _archived_with_a_cursor("c7", 5, "first half\nsecond half\n", len("first half\n"))

    out = KernelClient("c7").wait_cell(5, timeout_s=10, since=0)
    assert isinstance(out, Completed)
    assert "first half" in out.output and "second half" in out.output, \
        f"an explicit since=0 was answered from the archived cursor: {out.output!r}"


def test_the_implicit_form_still_resumes_from_the_archived_cursor(monkeypatch, tmp_path):
    """The other half of the same rule: a restart moves this caller's sidecar into the
    archive, and a cursorless wait must still resume behind it rather than replay (r11)."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _archived_with_a_cursor("c8", 5, "first half\nsecond half\n", len("first half\n"))

    out = KernelClient("c8").wait_cell(5, timeout_s=10, since=-1)
    assert isinstance(out, Completed)
    assert "first half" not in out.output and "second half" in out.output, out.output
