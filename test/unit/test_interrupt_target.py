"""interrupt()'s SIGINT fallback signals the kernel it entered on, or nothing (r13 f5).

Pure filesystem and fakes: the control channel is made to fail so every case falls through
to the fallback, which is the only branch under test.
"""
import os
import signal
import time

import pytest

from ptc.client import Busy, KernelClient
from ptc import ownership
from ptc.ownership import Owner, proc_start_time, write_owner
from ptc.paths import kernel_dir, secure_dir


def _owned(key: str, *, epoch: str = "e1", nonce: str = "n1") -> Owner:
    secure_dir(kernel_dir(key))
    o = Owner(os.getpid(), proc_start_time(os.getpid()), time.time(), nonce, epoch)
    write_owner(key, o)
    return o


@pytest.fixture
def signals(monkeypatch):
    """Every SIGINT the fallback sends, and no real one delivered to this process."""
    sent: list = []
    monkeypatch.setattr("ptc.client.os.kill", lambda pid, sig: sent.append((pid, sig)))
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
