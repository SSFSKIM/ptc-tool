"""Wait-then-submit: a poll loop in FRONT of the untouched admission machinery."""
import time

from ptc.client import Busy, Completed, KernelClient
from ptc.cells import CellRecord
from ptc.paths import Config


def _completed(cid=7):
    return Completed(cid, CellRecord(status="ok", duration_ms=1, result_repr="4",
                                     error=None, images=[], mutations=[]), "4")


def test_queued_exec_lands_when_the_slot_frees(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kc = KernelClient("q1")
    calls = {"n": 0}
    def fake_exec(code, timeout_s, config):
        calls["n"] += 1
        return Busy(3, reason="running") if calls["n"] < 3 else _completed()
    monkeypatch.setattr(kc, "exec_cell", fake_exec)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = kc.exec_cell_queued("2+2", timeout_s=30, config=Config.from_env())
    assert isinstance(out, Completed) and calls["n"] == 3


def test_queue_timeout_returns_an_honest_busy(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kc = KernelClient("q2")
    monkeypatch.setattr(kc, "exec_cell",
                        lambda code, timeout_s, config: Busy(3, reason="running"))
    monkeypatch.setattr(kc, "is_busy", lambda: Busy(3, reason="running"))
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    def sleep(s):
        clock["t"] += s
    monkeypatch.setattr(time, "sleep", sleep)
    out = kc.exec_cell_queued("2+2", timeout_s=2.0, config=Config.from_env())
    assert isinstance(out, Busy) and out.reason == "queue-timeout"
    assert out.cell_id == 3
    assert out.queued_s is not None and out.queued_s >= 2.0


def test_lost_admission_race_keeps_polling(monkeypatch, tmp_path):
    """A poller that sees the slot free but loses exec_cell's own atomic admission gets
    Busy back — that is one more poll, not a return."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kc = KernelClient("q3")
    seq = [Busy(None, reason="lock-held"), _completed()]
    monkeypatch.setattr(kc, "exec_cell", lambda code, timeout_s, config: seq.pop(0))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = kc.exec_cell_queued("2+2", timeout_s=30, config=Config.from_env())
    assert isinstance(out, Completed)
