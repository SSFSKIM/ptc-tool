"""The sent-but-unconfirmed marker discharges on EVIDENCE, never on age (F2, r2 finding 4).

Pure filesystem: owner.json is pointed at this very process, so `kernel_alive` is true
without spawning anything — what is under test is which facts on disk are allowed to
reopen admission after a request went out unconfirmed.
"""
import json
import os
import time

from ptc.client import Busy, KernelClient
from ptc.ownership import Owner, proc_start_time, write_owner
from ptc.paths import kernel_dir

_RECORD = {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
           "images": [], "mutations": []}


def _live_kernel(key: str, epoch: str = "e1"):
    kd = kernel_dir(key)
    (kd / "cells").mkdir(parents=True)
    write_owner(key, Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                           "nonce", epoch))
    (kd / "ready").write_text(epoch)
    return kd


def _mark(kd, cell_id, *, age: float = 3600.0, epoch: str = "e1"):
    (kd / "cells" / "pending.json").write_text(json.dumps(
        {"msg_id": "m-1", "cell_id": cell_id, "submitted_at": time.time() - age,
         "epoch": epoch}))


def test_an_aged_marker_alone_never_reopens_admission(monkeypatch, tmp_path):
    """The old rule deleted the marker on 60s of age and admitted the next cell. Age is
    not evidence: the unconfirmed request may still be queued behind a wedged cell, and
    admitting a second one is exactly the silent queueing this path forbids."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p1")
    _mark(kd, 4)

    assert KernelClient("p1").is_busy() == Busy(4, reason="pending-unconfirmed")
    assert (kd / "cells" / "pending.json").exists(), "the marker was deleted on age alone"


def test_a_marker_with_no_confirmed_id_stays_closed(monkeypatch, tmp_path):
    """Nothing on disk can ever name that request, so it stays busy until the kernel
    epoch ends — restart() archives cells/ and the marker with it."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p2")
    _mark(kd, None)

    assert KernelClient("p2").is_busy() == Busy(-1, reason="pending-unconfirmed")


def test_a_terminal_record_for_the_marked_cell_discharges_it(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p3")
    _mark(kd, 4)
    (kd / "cells" / "4.json").write_text(json.dumps(_RECORD))

    assert KernelClient("p3").is_busy() is None
    assert not (kd / "cells" / "pending.json").exists(), "a settled marker must be consumed"


def test_a_later_cell_in_current_json_discharges_it(monkeypatch, tmp_path):
    """Cells run in order, so a later one can only have started after ours left the
    queue — even though ours never got a record of its own."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p4")
    _mark(kd, 4)
    (kd / "cells" / "current.json").write_text(json.dumps({"cell_id": 5}))
    (kd / "cells" / "5.json").write_text(json.dumps(_RECORD))

    assert KernelClient("p4").is_busy() is None


def test_a_new_kernel_epoch_discharges_it(monkeypatch, tmp_path):
    """A marker is about the kernel that took the request. One that came back under a
    new epoch cannot be running it."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p5", epoch="e2")
    _mark(kd, 4, epoch="e1")

    assert KernelClient("p5").is_busy() is None


def test_a_dead_kernel_discharges_it(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p6")
    _mark(kd, 4)
    (kd / "owner.json").unlink()

    assert KernelClient("p6").is_busy() is None
