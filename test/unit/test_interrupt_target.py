"""interrupt() signals the kernel AND the cell it entered on, or nothing (r13 f5, r16 f2).

Pure filesystem and fakes: the control channel is made to fail so every case falls through
to the fallback, which is the branch the incarnation tests are about. The cell-binding
tests below watch the connect attempt too, because the retarget they guard against reached
the control channel first.
"""
import json
import os
import signal
import time

import pytest

from ptc.client import Busy, KernelClient
from ptc import ownership
from ptc.ownership import Owner, proc_start_time, write_owner
from ptc.paths import cells_dir, kernel_dir, secure_dir


def _owned(key: str, *, epoch: str = "e1", nonce: str = "n1") -> Owner:
    """A live, bootstrapped kernel: `ready` too, because `is_busy` will not call a kernel
    without it busy (`_kernel_known_dead`)."""
    kd = secure_dir(kernel_dir(key))
    o = Owner(os.getpid(), proc_start_time(os.getpid()), time.time(), nonce, epoch)
    write_owner(key, o)
    (kd / "ready").write_text(epoch)
    return o


def _running(key: str, cell_id: int) -> None:
    """What the kernel-side pre_run_cell hook publishes when a cell starts."""
    cd = secure_dir(cells_dir(key))
    (cd / "current.json").write_text(json.dumps({"cell_id": cell_id}))


def _settled(key: str, cell_id: int) -> None:
    """What post_run_cell writes when it ends — the terminal record."""
    cd = secure_dir(cells_dir(key))
    (cd / f"{cell_id}.json").write_text(json.dumps(
        {"status": "ok", "duration_ms": 1, "result_repr": None, "error": None,
         "images": [], "mutations": []}))


@pytest.fixture
def signals(monkeypatch):
    """Every SIGINT the fallback sends, and no real one delivered to this process."""
    sent: list = []
    real_kill = os.kill

    def kill(pid, sig):
        # signal 0 is an identity PROBE, not an interrupt: `owner_state` asks it of every
        # record it reads, and the tests that let `is_busy` run for real go through it
        if sig == 0:
            return real_kill(pid, sig)
        sent.append((pid, sig))

    monkeypatch.setattr("ptc.client.os.kill", kill)
    # the control channel never works here, so the 2 s grace always runs out
    monkeypatch.setattr(KernelClient, "_connect",
                        lambda self, **kw: (_ for _ in ()).throw(OSError("no ports")))
    return sent


def test_the_fallback_signals_the_incarnation_it_entered_on(monkeypatch, tmp_path, signals):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    o = _owned("i1")
    monkeypatch.setattr(KernelClient, "is_busy", lambda self: Busy(3, reason="running"))

    KernelClient("i1").interrupt()

    assert signals == [(o.pid, signal.SIGINT)], "the busy kernel was never signalled"


def test_a_restart_inside_the_grace_is_not_signalled(monkeypatch, tmp_path, signals):
    """The fallback re-read the owner fresh and signalled whatever pid it found. A restart
    landing inside the 2 s grace redirects the SIGINT onto the REPLACEMENT — a kernel that
    may already be part-way into a new cell, interrupted for a cell it never ran. The
    incarnation is captured at entry instead, and a fallback that no longer recognises it
    signals nothing: the cell this call meant to stop died with its kernel anyway.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _owned("i2", epoch="e1", nonce="n1")
    replaced: list = []

    def busy(self):
        if not replaced:                       # the restart lands inside the grace
            replaced.append(_owned("i2", epoch="e2", nonce="n2"))
        return Busy(3, reason="running")

    monkeypatch.setattr(KernelClient, "is_busy", busy)

    KernelClient("i2").interrupt()

    assert replaced, "the test never restarted the kernel"
    assert signals == [], f"the replacement kernel was signalled: {signals}"


def test_an_owner_whose_identity_cannot_be_read_is_not_signalled(monkeypatch, tmp_path,
                                                                 signals):
    """Unknown is not a target. The record still names the same incarnation, but nothing
    can confirm the pid behind it is still that process — and a pid handed out again
    belongs to somebody else's process, which is not ours to interrupt."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _owned("i3")
    monkeypatch.setattr(KernelClient, "is_busy", lambda self: Busy(3, reason="running"))
    monkeypatch.setattr(ownership, "proc_start_time", lambda pid, **kw: None)

    KernelClient("i3").interrupt()

    assert signals == [], f"an unidentifiable owner was signalled: {signals}"


# --- r16 finding 2: the operation is bound to a CELL as well as to an incarnation -------

def test_a_cell_that_settles_during_the_connect_is_not_signalled(monkeypatch, tmp_path,
                                                                 signals):
    """The retarget the incarnation check cannot see, because the kernel never changed.

    Cell A settles and another client admits cell B — through the same live kernel, so the
    owner record still names the incarnation this call entered on and the SIGINT lands on a
    cell nobody asked to stop. The target is captured at entry and re-checked before every
    signalling step instead.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _owned("i4")
    _running("i4", 3)
    swapped: list = []

    def connect(self, **kw):
        _settled("i4", 3)                  # A finished while this call was connecting
        _running("i4", 4)                  # and B took the kernel
        swapped.append(1)
        raise OSError("no ports")

    monkeypatch.setattr(KernelClient, "_connect", connect)

    KernelClient("i4").interrupt()

    assert swapped, "the test never swapped the cell"
    assert signals == [], f"a cell this call never targeted was interrupted: {signals}"


def test_a_settled_target_is_not_even_sent_a_control_request(monkeypatch, tmp_path,
                                                             signals):
    """The control-channel `interrupt_request` is a signalling step like the SIGINT, and it
    is the FIRST one: an interrupt_request delivered after the swap stops B just as dead."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _owned("i5")
    _settled("i5", 3)                      # the captured cell is already over
    monkeypatch.setattr(KernelClient, "is_busy", lambda self: Busy(3, reason="running"))
    connects: list = []
    monkeypatch.setattr(KernelClient, "_connect",
                        lambda self, **kw: (connects.append(1),
                                            (_ for _ in ()).throw(OSError("no ports")))[1])

    KernelClient("i5").interrupt()

    assert connects == [], "a settled cell was sent an interrupt_request"
    assert signals == [], f"a settled cell was signalled: {signals}"


def test_an_idle_kernel_is_a_no_op_that_costs_nothing(monkeypatch, tmp_path, signals):
    """Nothing is in flight, so there is nothing to interrupt — and no reason to open a
    control channel and block up to two seconds on a reply to discover it. The silent
    no-op every caller relies on is preserved; only its cost is not."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _owned("i6")
    monkeypatch.setattr(KernelClient, "is_busy", lambda self: None)
    connects: list = []
    monkeypatch.setattr(KernelClient, "_connect",
                        lambda self, **kw: (connects.append(1),
                                            (_ for _ in ()).throw(OSError("no ports")))[1])

    KernelClient("i6").interrupt()

    assert connects == [], "an idle kernel was connected to and waited on"
    assert signals == [], f"an idle kernel was signalled: {signals}"
