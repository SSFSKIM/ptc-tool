"""`wait(cell_id, until=…)`: return the moment NEW output first matches a pattern.

A long-budget wait is otherwise all-or-nothing — it answers once, at settle. `until=` is
the event counterpart: with the harness's MCP auto-backgrounding, the completion
notification becomes a trigger fired by the cell's own output.

Pure filesystem, in the style of test_wait_cursor.py: owner.json points at this very
process, so the key reads as a LIVE kernel, and a cell with a log and no terminal record
is one the poll loop keeps polling — which is the loop the scan overlays.
"""
import json
import os
import re
import time

import pytest

from ptc.cells import READ_CHUNK_BYTES, default_offset
from ptc.client import MATCHED_MAX_CHARS, Completed, KernelClient, Running
from ptc.ownership import Owner, proc_start_time, write_owner
from ptc.paths import cells_dir, kernel_dir, secure_dir

_RECORD = {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
           "images": [], "mutations": []}


def _running_cell(key: str, cell_id: int, log: str | bytes = "") -> None:
    """A cell that is still going: a live kernel, a log of its own, no record yet.

    The owner is this pytest process, so `_kernel_known_dead()` is false and the loop
    neither settles the cell dead nor reports it NotFound — it polls, exactly as it does
    against a real kernel mid-cell.
    """
    d = secure_dir(cells_dir(key))
    write_owner(key, Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                           "nonce", "e1"))
    (kernel_dir(key) / "ready").write_text("e1")
    p = d / f"{cell_id}.log"
    p.write_bytes(log if isinstance(log, bytes) else log.encode())


def _settle(key: str, cell_id: int) -> None:
    (cells_dir(key) / f"{cell_id}.json").write_text(json.dumps(_RECORD))


def test_a_matching_line_returns_before_the_cell_settles(monkeypatch, tmp_path):
    """The whole point: the cell is still running, and the caller is handed its output the
    moment the pattern appears in it rather than at settle (or at the budget)."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u1", 3, "working\nPAIR DISAGREE on item 4\n")

    out = KernelClient("u1").wait_cell(3, timeout_s=10, until=r"PAIR DISAGREE")

    assert isinstance(out, Running), out
    assert out.matched == "PAIR DISAGREE"
    assert "PAIR DISAGREE on item 4" in out.output
    assert out.next_offset > 0
    assert default_offset("u1", 3) == out.next_offset, \
        "the match return did not advance this caller's cursor"


def test_a_match_straddling_the_read_boundary_still_fires(monkeypatch, tmp_path):
    """One read is bounded (`READ_CHUNK_BYTES`), so a marker written across the boundary
    arrives in two pieces. The scan keeps a rolling window of the last chunk's worth of
    output, so the two halves are searched joined rather than separately."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u2", 5, b"." * (READ_CHUNK_BYTES - 3) + b"MARKER")

    out = KernelClient("u2").wait_cell(5, timeout_s=10, until="MARKER")

    assert isinstance(out, Running) and out.matched == "MARKER", out
    assert out.output.endswith("MARKER")


def test_a_settled_cell_still_completes_even_when_the_pattern_is_there(monkeypatch,
                                                                       tmp_path):
    """Completion supersedes the scan. A cell that is over has a terminal record, a status
    and a result — answering it as a matched Running would hide all three."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u3", 7, "MARKER and then it finished\n")
    _settle("u3", 7)

    out = KernelClient("u3").wait_cell(7, timeout_s=10, until="MARKER")

    assert isinstance(out, Completed), out
    assert "MARKER" in out.output


def test_a_scan_that_never_matches_leaves_no_trace(monkeypatch, tmp_path):
    """The overlay is invisible to every other outcome: a non-matching scan must hand back
    exactly the deadline Running a plain wait returns, read from the ENTRY cursor, with the
    cursor sidecar holding exactly that return's offset."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u4", 2, "nothing interesting here\n")
    _running_cell("u5", 2, "nothing interesting here\n")

    scanned = KernelClient("u4").wait_cell(2, timeout_s=0.5, until="NEVER")
    plain = KernelClient("u5").wait_cell(2, timeout_s=0.5)

    assert isinstance(scanned, Running) and scanned.matched is None
    assert (scanned.output, scanned.next_offset) == (plain.output, plain.next_offset)
    assert default_offset("u4", 2) == scanned.next_offset


def test_a_bad_pattern_raises_before_any_disk_work(monkeypatch, tmp_path):
    """`until` is compiled at entry, so an unusable regex is a call that never happened —
    no cursor advanced, no sidecar written, nothing for a retry to have to undo."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u6", 4, "some output\n")
    before = sorted(p.name for p in cells_dir("u6").iterdir())

    with pytest.raises(re.error):
        KernelClient("u6").wait_cell(4, timeout_s=10, until="(")

    assert sorted(p.name for p in cells_dir("u6").iterdir()) == before


def test_a_pattern_that_matches_the_empty_string_never_fires_on_no_output(monkeypatch,
                                                                          tmp_path):
    """The scan fires on NEW output, and an empty log has none. A pattern that matches
    anywhere (including nowhere) would otherwise return instantly on every call, turning
    `until=` into a way to get no output at all."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u7", 6, "")

    out = KernelClient("u7").wait_cell(6, timeout_s=0.5, until="")

    assert isinstance(out, Running) and out.matched is None, out
    assert out.output == ""


def test_the_drain_cannot_outlive_the_budget(monkeypatch, tmp_path):
    """A cell can write faster than the scan reads it — a print firehose — and the drain
    consumed every chunk available before it ever looked at the clock. The wait then ran
    past the budget its caller asked for, with no bound but the writer's appetite. The
    deadline is checked between chunks, and an expired one falls through to the ordinary
    deadline branch: the plain Running an unscanned wait would have returned.

    `timeout_s=0` is that state at its sharpest — the budget is already spent on entry, so
    exactly one chunk may be processed and no more.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u9", 8, b"." * (3 * READ_CHUNK_BYTES))

    import ptc.client as client_mod
    real = client_mod.read_output_since
    reads = []

    def counted(*a, **kw):
        reads.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(client_mod, "read_output_since", counted)

    out = KernelClient("u9").wait_cell(8, timeout_s=0, until="NEVER")

    assert isinstance(out, Running) and out.matched is None, out
    assert len(reads) <= 2, (
        f"the drain read {len(reads)} chunks past an expired deadline — one scan read, "
        "then the deadline branch's own read, is the whole of what a spent budget allows")


def test_a_window_that_dropped_output_says_where_its_own_output_begins(monkeypatch,
                                                                       tmp_path):
    """The match window is bounded, so a chatty cell's early output falls out of it — and
    a `Running` whose `output` silently began somewhere after the caller's cursor was a
    lie by omission. Those bytes were read, never shown, and unreachable by the route the
    render itself recommends: `wait(since=next_offset)` resumes AFTER the window. The
    window therefore reports the byte its own output starts at.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("ua", 1, b"." * (3 * READ_CHUNK_BYTES) + b"MARKER")

    out = KernelClient("ua").wait_cell(1, timeout_s=10, until="MARKER")

    assert isinstance(out, Running) and out.matched == "MARKER", out
    assert len(out.output) <= 2 * READ_CHUNK_BYTES, "the window grew without bound"
    assert out.next_offset == 3 * READ_CHUNK_BYTES + len("MARKER")
    assert out.window_start > 0, "output began mid-log and claimed to start at the cursor"
    # the log is ASCII, so bytes and characters agree: what came back really is exactly
    # the span [window_start, next_offset) — nothing claimed that was not delivered
    assert out.window_start + len(out.output) == out.next_offset

    from ptc.shape import to_dict
    assert to_dict(out, "ua")["window_start"] == out.window_start


def test_the_match_is_capped_where_it_is_taken(monkeypatch, tmp_path):
    """`matched` names the EVENT — it is not a payload channel; the output window carries
    the data. A greedy pattern (`.*MARKER`, `(?s)^.*done`) otherwise hands the whole window
    back a second time through a field every renderer treats as a short label, and the
    caller pays for it twice. Capping it where it is taken bounds render, to_dict and the
    CLI's JSON at once, rather than asking three call sites to remember."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("ub", 2, b"." * (2 * READ_CHUNK_BYTES) + b"x" * 600 + b"MARKER")

    out = KernelClient("ub").wait_cell(2, timeout_s=10, until=r"x{600}MARKER")

    assert isinstance(out, Running) and out.matched, out
    assert len(out.matched) == MATCHED_MAX_CHARS, \
        f"a 606-character match came back {len(out.matched)} characters long"


def test_a_cell_that_settles_during_the_scan_still_completes(monkeypatch, tmp_path):
    """The record is read at the top of the loop and the scan reads the log after it, so a
    settle landing between the two made a FINISHED cell come back as a matched Running —
    with no status, no result and no error, and the caller told to keep waiting on a cell
    that was already over. Completion supersedes the scan, so the record is re-read before
    a match is returned and the loop's own top settles the cell from the entry cursor."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("uc", 3, "MARKER\n")

    import ptc.client as client_mod
    real = client_mod.read_output_since

    def settles_behind_the_read(*a, **kw):
        out = real(*a, **kw)
        _settle("uc", 3)          # the kernel finished while we were reading its log
        return out

    monkeypatch.setattr(client_mod, "read_output_since", settles_behind_the_read)

    out = KernelClient("uc").wait_cell(3, timeout_s=10, until="MARKER")

    assert isinstance(out, Completed), f"a settled cell came back as a matched Running: {out}"
    assert "MARKER" in out.output


def test_the_scan_only_sees_output_this_caller_has_not_been_served(monkeypatch, tmp_path):
    """`until=` reads through the caller's own cursor, so a marker already delivered by an
    earlier wait does not re-fire — the trigger is about what is NEW."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _running_cell("u8", 9, "MARKER once\n")

    kc = KernelClient("u8")
    first = kc.wait_cell(9, timeout_s=10, until="MARKER")
    assert isinstance(first, Running) and first.matched == "MARKER"

    second = kc.wait_cell(9, timeout_s=0.5, until="MARKER")
    assert isinstance(second, Running) and second.matched is None, second
    assert second.output == ""
