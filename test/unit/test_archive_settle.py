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
