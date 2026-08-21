"""Settling a cell from a previous kernel epoch — pure filesystem, no kernel needed."""
import json

from ptc.client import Completed, KernelClient


def _archive(home, key: str, cell_id: int, log: str, rec: dict | None) -> None:
    d = home / "kernels" / key / "cells-prev-1"
    d.mkdir(parents=True)
    (d / f"{cell_id}.log").write_text(log)
    if rec is not None:
        (d / f"{cell_id}.json").write_text(json.dumps(rec))


def test_archived_record_from_a_drifted_schema_still_settles(monkeypatch, tmp_path):
    """A record written by a build whose CellRecord has other fields must settle like
    any other ended epoch: a wait that raises strands the caller on a dead cell."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _archive(tmp_path, "a1", 7, "hello\n",
             {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
              "images": [], "mutations": [], "field_from_another_build": True})
    out = KernelClient("a1")._archived(7)
    assert isinstance(out, Completed)
    assert out.record.error["ename"] == "KernelEpochEnded"
    assert "hello" in out.output and "previous kernel epoch" in out.output


def test_archived_settle_resumes_at_the_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _archive(tmp_path, "a2", 3, "one\ntwo\n", None)
    out = KernelClient("a2")._archived(3, len("one\n"))
    assert "one" not in out.output and "two" in out.output


def test_two_archives_in_the_same_second_both_succeed(tmp_path):
    """`cells-prev-<timestamp>` collided when a cleanup and the retry after a failed
    spawn landed in one second: renaming onto a NONEMPTY archive raises FileExistsError,
    so the first attempt's archive blocked every later attempt to recover."""
    from ptc import kernel

    kd = tmp_path / "k"
    for text in ("first", "second"):
        (kd / "cells").mkdir(parents=True)
        (kd / "cells" / "1.log").write_text(text)
        kernel._rotate_cells(kd)

    archives = list(kd.glob("cells-prev-*"))
    assert len(archives) == 2, archives
    assert {p.read_text() for a in archives for p in a.glob("*.log")} == {"first", "second"}
    assert not (kd / "cells").exists()


def test_archived_images_are_remapped_into_the_archive(monkeypatch, tmp_path):
    """A record names its images by the path they had under cells/, and restart MOVED
    those files into the archive. The renderer drops paths that no longer exist, so an
    archived wait silently lost every image the cell had produced."""
    from ptc.paths import Config, cells_dir
    from ptc.shape import render

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    live = cells_dir("a4")
    _archive(tmp_path, "a4", 5, "plot\n",
             {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
              "images": [str(live / "5-0.png"), str(live / "5-1.png")], "mutations": []})
    d = tmp_path / "kernels" / "a4" / "cells-prev-1"
    (d / "5-0.png").write_bytes(b"png-bytes")      # 5-1 never made it into the archive

    out = KernelClient("a4")._archived(5)

    assert out.record.images == [str(d / "5-0.png")]
    assert render(out, "a4", Config.from_env(env={})).images == [d / "5-0.png"]


# --- r5 finding 4: an id this kernel never ran is not a running cell ------------------

def _live_kernel(key: str):
    """owner.json pointed at this very process, so `kernel_alive` is true with nothing
    spawned — the same fixture test_pending_admission.py builds its cases on."""
    import os
    import time

    from ptc.ownership import Owner, proc_start_time, write_owner
    from ptc.paths import kernel_dir

    kd = kernel_dir(key)
    (kd / "cells").mkdir(parents=True)
    write_owner(key, Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                           "nonce", "e1"))
    (kd / "ready").write_text("e1")
    return kd


def test_wait_on_an_id_this_kernel_never_ran_is_not_found(monkeypatch, tmp_path):
    """A mistyped id, or one from another session: no record, no log, no archive. A live
    kernel made the loop answer Running after every timeout, forever — and nothing could
    ever make that id valid, since PTC queues nothing and a confirmed cell has its log
    before any caller can learn its number."""
    import time

    from ptc.client import NotFound

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _live_kernel("w1")

    t0 = time.monotonic()
    out = KernelClient("w1").wait_cell(4242, timeout_s=30)
    assert isinstance(out, NotFound) and out.cell_id == 4242
    assert time.monotonic() - t0 < 5, "an unknown id was waited on rather than reported"


def test_wait_on_an_acknowledged_but_unpublished_cell_still_waits(monkeypatch, tmp_path):
    """The sent-but-unconfirmed marker names a cell the kernel acknowledged (its number
    came back on execute_input) but has not started, so it has no log yet. That id is
    known to be real: calling it unknown would strand a caller on a cell that is about to
    run."""
    from ptc.client import Running

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("w2")
    (kd / "cells" / "pending.json").write_text(json.dumps(
        {"msg_id": "m-1", "cell_id": 7, "submitted_at": 0.0, "epoch": "e1"}))

    assert isinstance(KernelClient("w2").wait_cell(7, timeout_s=0.5), Running)
