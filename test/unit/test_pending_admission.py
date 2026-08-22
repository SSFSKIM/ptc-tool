"""The sent-but-unconfirmed marker discharges on EVIDENCE, never on age (F2, r2 finding 4).

Pure filesystem: owner.json is pointed at this very process, so `kernel_alive` is true
without spawning anything — what is under test is which facts on disk are allowed to
reopen admission after a request went out unconfirmed.
"""
import json
import os
import time

import pytest

from ptc.client import Busy, KernelClient
from ptc.ownership import Owner, proc_start_time, write_owner
from ptc.paths import Config, kernel_dir

_RECORD = {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
           "images": [], "mutations": []}


def _live_kernel(key: str, epoch: str = "e1", nonce: str = "nonce"):
    kd = kernel_dir(key)
    (kd / "cells").mkdir(parents=True)
    write_owner(key, Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                           nonce, epoch))
    (kd / "ready").write_text(epoch)
    return kd


def _mark(kd, cell_id, *, age: float = 3600.0, epoch: str = "e1", nonce=...):
    data = {"msg_id": "m-1", "cell_id": cell_id, "submitted_at": time.time() - age,
            "epoch": epoch}
    if nonce is not ...:
        data["nonce"] = nonce
    (kd / "cells" / "pending.json").write_text(json.dumps(data))


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


# --- r3 finding 1: the marker is written BEFORE the request goes on the wire ----------

class _Killed(BaseException):
    """Stands in for the adapter dying between the send and the exception handler."""


class _FakeKC:
    """A kernel client whose execute() records what is on disk at the instant of the send
    and then never returns — the shape of a SIGKILL landing inside the wire window."""

    def __init__(self, marker):
        self._marker = marker
        self.marker_at_send = None

    def execute(self, *a, **kw):
        self.marker_at_send = self._marker.exists()
        raise _Killed()

    def stop_channels(self):
        pass


def test_the_marker_is_on_disk_before_the_request_is_sent(monkeypatch, tmp_path):
    """An exception handler cannot cover the window that matters.

    A submitter killed between `kc.execute()` putting the request on the wire and the
    handler running leaves NO marker, and its death releases the submit lock — so the next
    adapter sees neither current.json nor a marker while the kernel still holds the first
    request, and queues a second cell silently on top of it. The marker therefore goes down
    first, and survives a failed send: only proof discharges it, and an exception out of
    execute() does not prove the request stayed off the wire.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p7")
    marker = kd / "cells" / "pending.json"
    kc = _FakeKC(marker)
    monkeypatch.setattr(KernelClient, "_connect", lambda self, **kw: kc)

    with pytest.raises(_Killed):
        KernelClient("p7").exec_cell("1 + 1", 1.0, Config.from_env(env={}))

    assert kc.marker_at_send is True, "the request went out before the marker was written"
    assert marker.exists(), "the marker was cleared by a send whose failure proves nothing"
    # a NEW client — the one that would have queued the second cell — refuses admission
    assert KernelClient("p7").is_busy() == Busy(-1, reason="pending-unconfirmed")


def test_a_marker_that_cannot_be_written_aborts_the_submission(monkeypatch, tmp_path):
    """Before the send the marker IS the admission guard, so one that cannot be written
    fails the submission rather than sending unguarded."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p8")
    kc = _FakeKC(kd / "cells" / "pending.json")
    monkeypatch.setattr(KernelClient, "_connect", lambda self, **kw: kc)
    monkeypatch.setattr("ptc.client.private_write_text",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")))

    with pytest.raises(OSError):
        KernelClient("p8").exec_cell("1 + 1", 1.0, Config.from_env(env={}))

    assert kc.marker_at_send is None, "the request was sent with no marker behind it"


# --- r5 finding 1: a stale submitter never marks the REPLACEMENT epoch ----------------

class _RestartingKC:
    """A kernel client that is replaced mid-flight: the request goes out, the key is
    restarted under a new incarnation, and only then does the submitter come back to
    refresh its marker — the shape of restart() landing inside an exec's wire window."""

    def __init__(self, restart):
        self._restart = restart

    def execute(self, *a, **kw):
        self._restart()
        raise _Killed()

    def stop_channels(self):
        pass


def test_a_stale_submitter_never_marks_the_replacement_epoch(monkeypatch, tmp_path):
    """The old adapter's marker refresh re-read the CURRENT owner and wrote into the NEW
    kernel's cells/ under the new epoch. Nothing can discharge a marker naming no cell
    (`_pending_discharged`), so the replacement was permanently busy the moment it was
    ready. The old request died with the old kernel — the restart already settled it.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p9", epoch="e1")

    def restart():
        # what restart_kernel leaves behind: the old cells/ — marker and all — is
        # archived, and a new incarnation takes the key
        (kd / "cells").rename(kd / "cells-prev-1")
        (kd / "cells").mkdir()
        write_owner("p9", Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                                "nonce-2", "e2"))
        (kd / "ready").write_text("e2")

    monkeypatch.setattr(KernelClient, "_connect",
                        lambda self, **kw: _RestartingKC(restart))

    with pytest.raises(_Killed):
        KernelClient("p9").exec_cell("1 + 1", 1.0, Config.from_env(env={}))

    assert (kd / "cells-prev-1" / "pending.json").exists(), \
        "the marker for the old kernel must still be the one that was archived with it"
    assert not (kd / "cells" / "pending.json").exists(), \
        "the stale submitter planted a marker in the replacement's cells/"
    assert KernelClient("p9").is_busy() is None, "the fresh kernel was born busy"


# --- r8 finding 1: the marker names the kernel the request actually reached -----------

class _EpochBoundKC:
    """A connection that remembers WHICH incarnation's ports it opened, and records the
    marker standing on disk at the instant the request goes out on them."""

    def __init__(self, epoch: str, marker):
        self.epoch = epoch
        self._marker = marker
        self.marker_epoch_at_send = None

    def execute(self, *a, **kw):
        try:
            self.marker_epoch_at_send = json.loads(self._marker.read_text())["epoch"]
        except (OSError, json.JSONDecodeError, KeyError):
            self.marker_epoch_at_send = None
        raise _Killed()

    def stop_channels(self):
        pass


def test_the_marker_names_the_incarnation_the_connection_reached(monkeypatch, tmp_path):
    """`connection.json` and `owner.json` are rewritten one after the other by a restart,
    and the owner used to be read once the ports were already open. A restart landing in
    between therefore stamped the marker with the REPLACEMENT's epoch while the request
    went out on the OLD kernel's ports: that request dies unanswered, and a marker naming
    no cell under the live epoch is one nothing can discharge — the fresh kernel is busy
    from the moment it is ready. The connection and the incarnation are bound together
    instead, so whichever kernel a submission reaches is the one its marker is about.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p10", epoch="e1")
    marker = kd / "cells" / "pending.json"
    ports = {"epoch": "e1"}
    opened: list = []

    def restart():
        # what restart_kernel leaves behind: cells/ archived, new ports published under a
        # new incarnation. It lands AFTER the first connection has opened the old ports.
        (kd / "cells").rename(kd / "cells-prev-1")
        (kd / "cells").mkdir()
        ports["epoch"] = "e2"
        write_owner("p10", Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                                 "nonce-2", "e2"))
        (kd / "ready").write_text("e2")

    def connect(self, **kw):
        kc = _EpochBoundKC(ports["epoch"], marker)
        opened.append(kc)
        if len(opened) == 1:
            restart()
        return kc

    monkeypatch.setattr(KernelClient, "_connect", connect)

    with pytest.raises(_Killed):
        KernelClient("p10").exec_cell("1 + 1", 1.0, Config.from_env(env={}))

    sent = [kc for kc in opened if kc.marker_epoch_at_send is not None]
    assert len(sent) == 1, f"the request went out {len(sent)} times"
    assert sent[0].marker_epoch_at_send == sent[0].epoch, (
        "the marker was stamped with a kernel other than the one the request reached: "
        f"marker={sent[0].marker_epoch_at_send} ports={sent[0].epoch}")


def test_a_key_replaced_faster_than_it_can_be_bound_fails_before_sending(monkeypatch,
                                                                         tmp_path):
    """The retry has to end somewhere. A key whose kernel is replaced on every connect
    can never be bound to, and guessing which incarnation to name is the failure this
    binding exists to prevent — so the submission is refused with its reason, having sent
    nothing and left no marker for the next caller to trip over.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p11", epoch="e1")
    marker = kd / "cells" / "pending.json"
    seq = iter(("e2", "e3", "e4"))

    def connect(self, **kw):
        kc = _EpochBoundKC("whatever", marker)
        write_owner("p11", Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                                 "n", next(seq)))
        return kc

    monkeypatch.setattr(KernelClient, "_connect", connect)

    with pytest.raises(RuntimeError, match="replaced twice"):
        KernelClient("p11").exec_cell("1 + 1", 1.0, Config.from_env(env={}))

    assert not marker.exists(), "a submission that never sent anything left a marker"


# --- r10 finding 3: the marker names the incarnation, not just the second it started ---

def test_a_marker_from_a_same_second_predecessor_discharges(monkeypatch, tmp_path):
    """The epoch is a whole-second timestamp, so two incarnations born inside one second
    carry the SAME one — which is why `_owner_identity` is a pair. The marker recorded only
    the epoch, so a stamp that lost the race between `_refresh_pending`'s identity check
    and its write (check-then-write, no lock) landed in the REPLACEMENT's fresh cells/
    looking exactly like the replacement's own: equal epochs, no cell id for a record to
    settle, nothing left to discharge it. The fresh kernel was busy for good. The nonce is
    drawn per spawn and tells the two apart."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p12", epoch="e1", nonce="nonce-2")
    _mark(kd, None, epoch="e1", nonce="nonce-1")

    assert KernelClient("p12").is_busy() is None, "the fresh kernel inherited a stranger"
    assert not (kd / "cells" / "pending.json").exists(), "a discharged marker must go"


def test_a_marker_from_this_very_incarnation_still_holds_admission(monkeypatch, tmp_path):
    """The other half: the nonce must not discharge the marker the LIVE kernel's own
    submitter wrote — that request may still be queued, and admitting past it is the silent
    queueing this whole path forbids."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p13", epoch="e1", nonce="nonce-1")
    _mark(kd, None, epoch="e1", nonce="nonce-1")

    assert KernelClient("p13").is_busy() == Busy(-1, reason="pending-unconfirmed")


def test_a_marker_written_before_nonces_keeps_the_epoch_only_verdict(monkeypatch,
                                                                     tmp_path):
    """Markers live for seconds and are archived by the restart that ends their epoch, so
    there is nothing to migrate — but one written by the previous build carries no nonce,
    and absence of the key is absence of evidence, not evidence of a mismatch."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p14", epoch="e1", nonce="nonce-2")
    _mark(kd, None, epoch="e1")                          # no nonce key at all

    assert KernelClient("p14").is_busy() == Busy(-1, reason="pending-unconfirmed")


def test_the_marker_records_the_nonce_it_was_stamped_with(monkeypatch, tmp_path):
    """The comparison above is only worth as much as what is written: the marker carries
    the whole identity pair the submission bound itself to."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p15", epoch="e1", nonce="nonce-1")

    KernelClient("p15")._mark_pending("m-1", 4, ("e1", "nonce-1"))

    data = json.loads((kd / "cells" / "pending.json").read_text())
    assert data["epoch"] == "e1" and data["nonce"] == "nonce-1", data


# --- r12 finding 1: an UNKNOWN identity discharges nothing -----------------------------

def _unreadable_identity(monkeypatch):
    """What a transient `ps`/libproc failure looks like to `owner_state`: the pid is ours
    and answers signal 0, and its birth identity cannot be read — the third answer, which
    `owner_alive` (and so `kernel_alive`) collapses to "not alive"."""
    from ptc import ownership
    monkeypatch.setattr(ownership, "proc_start_time", lambda *a, **kw: None)


def test_an_unreadable_identity_never_discharges_the_marker(monkeypatch, tmp_path):
    """`_pending_discharged` opened with `not kernel_alive(...)`, and its caller UNLINKS
    the marker on that verdict — so one failed identity read against a LIVE kernel threw
    away the admission guard and let a second cell be submitted on top of an in-flight
    one. Unknown is not death: the marker holds until the kernel can be identified."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p16")
    _mark(kd, 4)
    _unreadable_identity(monkeypatch)

    assert KernelClient("p16").is_busy() == Busy(4, reason="pending-unconfirmed")
    assert (kd / "cells" / "pending.json").exists(), \
        "the marker was discharged on an identity nobody could read"


def test_an_unreadable_identity_keeps_a_running_cell_busy(monkeypatch, tmp_path):
    """The same collapse one branch earlier: current.json names a cell with no terminal
    record — the kernel is running it — and an unreadable identity made `is_busy` fall
    straight through to "not busy". Admission fails closed on unknown."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = _live_kernel("p17")
    (kd / "cells" / "current.json").write_text(json.dumps({"cell_id": 7}))
    _unreadable_identity(monkeypatch)

    assert KernelClient("p17").is_busy() == Busy(7, reason="running")
