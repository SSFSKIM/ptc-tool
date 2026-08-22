import json
import multiprocessing as mp
import os
import subprocess
import sys
import time

import pytest
from ptc import ownership
from ptc.lock import flock_path, key_lock, submit_lock
from ptc.ownership import (
    Owner,
    UnknownOwner,
    owner_alive,
    owner_state,
    proc_start_time,
    read_owner,
    settled_owner_state,
    start_time_matches,
    write_owner,
)


def _hold(path, dur, q):
    from ptc.lock import flock_path
    from pathlib import Path
    with flock_path(Path(path)):
        q.put("held")
        time.sleep(dur)


def test_flock_excludes_across_processes(tmp_path):
    p = tmp_path / "l"
    q = mp.Queue()
    proc = mp.Process(target=_hold, args=(str(p), 1.0, q))
    proc.start()
    assert q.get(timeout=5) == "held"
    t0 = time.monotonic()
    with flock_path(p):          # must block until child releases
        waited = time.monotonic() - t0
    proc.join()
    assert waited > 0.5


def test_flock_timeout(tmp_path):
    p = tmp_path / "l"
    q = mp.Queue()
    proc = mp.Process(target=_hold, args=(str(p), 2.0, q))
    proc.start()
    q.get(timeout=5)
    try:
        with pytest.raises(TimeoutError):
            with flock_path(p, timeout=0.2):
                pass
    finally:
        proc.join()


def test_owner_roundtrip_and_liveness(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    me = os.getpid()
    st = proc_start_time(me)
    assert st  # own process must have a readable start time
    o = Owner(pid=me, proc_start_time=st, spawned_at=time.time(), nonce="n", epoch="e1")
    write_owner("k1", o)
    got = read_owner("k1")
    assert got == o and owner_alive(got)
    # wrong start time == PID reuse -> not alive
    assert not owner_alive(Owner(me, "Thu Jan  1 00:00:00 1970", 0.0, "n", "e"))
    # dead pid -> not alive
    assert not owner_alive(Owner(99999999, st, 0.0, "n", "e"))


def test_read_owner_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    assert read_owner("nope") is None


# --- r6 finding 4: the identity string must not depend on who is reading it -----------

def test_owner_identity_survives_a_different_timezone_and_locale(monkeypatch, tmp_path):
    """`ps -o lstart=` formats through TZ and LC_TIME, and PTC's identity string is written
    by one process (the spawner) and compared by others (a CLI, the adapter, the watchdog).
    Under a different TZ the same live process read back as a DIFFERENT string, so
    `owner_alive` called the live owner dead: `kill` then dropped its metadata without
    signalling it and the next ensure spawned a duplicate kernel beside it."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    me = os.getpid()

    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LC_ALL", "C")
    recorded = proc_start_time(me)
    assert recorded

    # the kernel is inspected later from a shell with its own timezone and locale
    monkeypatch.setenv("TZ", "Asia/Seoul")
    monkeypatch.setenv("LC_TIME", "C")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert proc_start_time(me) == recorded, "the pinned read must not follow the reader's TZ"
    assert owner_alive(Owner(me, recorded, time.time(), "n", "e1"))


def test_an_owner_recorded_before_pinning_is_still_recognized(monkeypatch, tmp_path):
    """Migration tolerance: an owner.json on disk from before reads were pinned holds the
    writer's unpinned string. One unpinned re-read settles it exactly as the pre-pinning
    code did, so upgrading PTC does not orphan the kernel that is already running."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    me = os.getpid()
    monkeypatch.setenv("TZ", "Asia/Seoul")
    legacy = proc_start_time(me, pinned=False)
    assert legacy and legacy != ownership._ps(me, "lstart="), \
        "this machine renders TZ into lstart"

    assert start_time_matches(me, legacy) is True
    assert owner_alive(Owner(me, legacy, time.time(), "n", "e1"))
    # and a string from neither read is still a mismatch, not a free pass
    assert start_time_matches(me, "Thu Jan  1 00:00:00 1970") is False


# --- r9 finding 1: a birth time good only to the second is not an identity -------------

def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])


def test_identity_is_finer_than_the_second_the_process_was_born_in():
    """`ps -o lstart=` resolves to the second, and a pid or pgid can be handed out again
    inside one — after which the recorded identity matched a completely unrelated process
    and kill/restart/TTL cleanup would signal its whole group. The OS kernel's own birth
    stamp (`/proc/<pid>/stat` ticks on Linux, libproc microseconds on macOS) tells apart
    two processes born in the same second, which is all this asks of it."""
    a, b = _sleeper(), _sleeper()
    try:
        same_second = ownership._ps(a.pid, "lstart=") == ownership._ps(b.pid, "lstart=")
        if not same_second:
            pytest.skip("the two probe processes did not land in the same second")
        assert proc_start_time(a.pid) != proc_start_time(b.pid)
    finally:
        for p in (a, b):
            p.kill()
            p.wait()


def test_the_identity_recorded_at_spawn_still_matches_moments_later():
    """The identity is written the instant after the fork and compared for the whole life
    of the process, so its source must be settled at the fork. `ps -o comm=` is not: macOS
    reports the invoked path for a freshly forked process and the resolved executable a
    moment later, so composing it into the identity made every live kernel read as a
    recycled pid a second after it started — the next ensure deleting its metadata and
    spawning a duplicate beside it."""
    p = _sleeper()
    try:
        recorded = proc_start_time(p.pid)
        assert recorded
        time.sleep(1.2)
        assert start_time_matches(p.pid, recorded) is True
    finally:
        p.kill()
        p.wait()


def test_linux_identity_is_the_kernels_own_subsecond_birth_stamp(monkeypatch):
    """Field 22 of `/proc/<pid>/stat` is start time in clock ticks since boot. Field 2 is
    the command in parentheses and may contain spaces and parentheses of its own, which is
    why the fields are counted from the last ')' rather than by splitting the line."""
    fields = (["4242", "(my (odd) prog)"]
              + [f"f{n}" for n in range(3, 22)] + ["8675309", "tail"])
    monkeypatch.setattr(ownership, "_ON_LINUX", True)
    monkeypatch.setattr(ownership, "_ON_DARWIN", False)
    monkeypatch.setattr(ownership, "_proc_stat", lambda pid: " ".join(fields) + "\n")
    assert ownership._start_ticks(" ".join(fields)) == "8675309"
    assert proc_start_time(4242) == "btime=8675309"
    assert start_time_matches(4242, "btime=8675309") is True
    assert start_time_matches(4242, "btime=8675310") is False


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS only")
def test_macos_birth_stamp_is_read_from_libproc_and_refuses_a_dead_pid():
    """The struct is declared here and filled by the OS, so the read is validated rather
    than trusted: the call must fill exactly the declared length and report back the pid it
    was asked about, or the answer is "unknown" instead of somebody else's birth time."""
    assert ownership._darwin_birth(os.getpid()) == ownership._darwin_birth(os.getpid())
    assert "." in ownership._darwin_birth(os.getpid())     # seconds.microseconds
    assert ownership._darwin_birth(99999999) is None


def test_a_platform_with_no_birth_stamp_falls_back_to_the_pinned_lstart(monkeypatch,
                                                                        tmp_path):
    """No third behaviour for a platform PTC has never met: it gets exactly the identity
    r6 shipped, and records written that way stay comparable in both directions."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr(ownership, "_birth", lambda pid: None)
    me = os.getpid()
    recorded = proc_start_time(me)
    assert recorded == ownership._ps(me, "lstart=")
    assert start_time_matches(me, recorded) is True
    assert owner_alive(Owner(me, recorded, time.time(), "n", "e1"))


def test_an_owner_recorded_before_the_birth_stamp_is_still_recognized(monkeypatch,
                                                                      tmp_path):
    """Migration, r6's pattern again: rows written by the previous version hold a bare
    `ps -o lstart=` string, which can never equal a birth stamp. A record that names no
    source is re-compared against exactly the two readings that used to be written, so an
    upgrade does not orphan the kernel already running."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    me = os.getpid()
    legacy = ownership._ps(me, "lstart=")          # byte-for-byte what r6 wrote
    assert legacy and not legacy.startswith("btime=")
    assert start_time_matches(me, legacy) is True
    assert owner_alive(Owner(me, legacy, time.time(), "n", "e1"))


# --- r9 finding 2: an identity that could not be READ is not a dead kernel -------------

def test_an_unreadable_identity_neither_deletes_metadata_nor_respawns(monkeypatch,
                                                                      tmp_path):
    """A `ps` that fails transiently used to read as "the owner is dead": `ensure_kernel`
    then deleted a live kernel's metadata and spawned a duplicate beside it, and
    `kill_kernel` reported success while orphaning it. Unknown is now its own answer —
    retried once, then refused, with everything on disk left exactly as it was found."""
    from ptc.kernel import ensure_kernel, kill_kernel
    from ptc.paths import kernel_dir, secure_dir

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    key = "unreadable"
    kd = secure_dir(kernel_dir(key))
    write_owner(key, Owner(os.getpid(), proc_start_time(os.getpid()), time.time(),
                           "n", "e1"))
    (kd / "ready").write_text("e1")

    reads: list = []
    monkeypatch.setattr(ownership, "proc_start_time",
                        lambda pid, **kw: reads.append(pid) or None)

    with pytest.raises(UnknownOwner, match="cannot tell"):
        ensure_kernel(key)
    assert len(reads) == 2, f"a transient ps failure is retried exactly once: {reads}"
    assert not (kd / "connection.json").exists(), "a duplicate kernel was spawned"

    with pytest.raises(UnknownOwner, match="cannot tell"):
        kill_kernel(key)

    assert read_owner(key) is not None, "the live owner's metadata was deleted"
    assert (kd / "ready").exists()


# --- r12 finding 4: a kill that signalled nothing did not kill anything ----------------

def test_killing_a_stale_record_reports_that_nothing_was_killed(monkeypatch, tmp_path):
    """The record is there and the process behind it is not — the kernel died, or its pid
    was handed to somebody else. Clearing the metadata is right and stays; reporting a
    KILL for it is not. `ptc kill` exited 0 on it, against the contract its own epilog and
    the README state — 0 for a kernel actually signalled, 1 when there was nothing to
    kill — so a shell loop could not tell a cleanup from a sweep of dead records.
    """
    from ptc import kernel
    from ptc.kernel import kill_kernel
    from ptc.paths import kernel_dir, secure_dir

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kd = secure_dir(kernel_dir("stale"))
    # our own pid, recorded with a birth stamp that is not ours: exactly what a recycled
    # pid looks like, and answerable without a second process
    write_owner("stale", Owner(os.getpid(), "btime=1.000000", time.time(), "n", "e1"))
    (kd / "ready").write_text("e1")
    signalled: list = []
    monkeypatch.setattr(kernel, "kill_process_tree", lambda pid: signalled.append(pid))

    assert kill_kernel("stale") is False, "a kill that signalled nothing reported success"

    assert signalled == [], f"a pid whose identity no longer matches was signalled: {signalled}"
    assert not (kd / "owner.json").exists(), "the stale record was left behind"
    assert not (kd / "ready").exists()


# --- r13 finding 1: a zombie is a corpse, not a kernel with a matching birth stamp -----

def _stat_line(state: str, ticks: str = "8675309") -> str:
    """A `/proc/<pid>/stat` line in the shape the parser has to survive: an executable
    name carrying spaces AND parentheses, then field 3 (the run state) and field 22
    (start ticks) where the field arithmetic says they are."""
    fields = (["4242", "(my (odd) prog)", state]
              + [f"f{n}" for n in range(4, 22)] + [ticks, "tail"])
    return " ".join(fields) + "\n"


def test_a_zombie_kernels_intact_birth_stamp_does_not_make_it_alive(monkeypatch, tmp_path):
    """The kernel is spawned `start_new_session=True` and its `Popen` is never retained,
    so nothing ever wait()s it: a kernel that dies while its spawning adapter lives lingers
    as a zombie for as long as that adapter does. `kill(pid, 0)` still succeeds on one and
    Linux goes on serving its ORIGINAL start ticks out of `/proc`, so the identity matched
    and `owner_state` called a corpse alive — `_follow` reported Running forever and
    `ensure_kernel` attached to it. The state field is field 3 of the same line the birth
    stamp is read from, and it is now read with it.
    """
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr(ownership, "_ON_LINUX", True)
    monkeypatch.setattr(ownership, "_ON_DARWIN", False)
    me = os.getpid()                     # signalable for real; the stat line is fabricated
    o = Owner(me, "btime=8675309", time.time(), "n", "e1")

    monkeypatch.setattr(ownership, "_proc_stat", lambda pid: _stat_line("S"))
    assert owner_state(o) is True, "a sleeping kernel is alive"

    zombie = _stat_line("Z")
    monkeypatch.setattr(ownership, "_proc_stat", lambda pid: zombie)
    assert ownership._start_ticks(zombie) == "8675309", "the birth stamp still matches..."
    assert start_time_matches(me, "btime=8675309") is True
    assert owner_state(o) is False, "...and the process behind it is still dead"
    assert settled_owner_state(o) is False, "unknown is not the answer either"

    # X/x is the same fact one moment later: a task already tearing down.
    monkeypatch.setattr(ownership, "_proc_stat", lambda pid: _stat_line("X"))
    assert owner_state(o) is False


def test_a_real_unreaped_child_reads_as_dead_on_this_platform(monkeypatch, tmp_path):
    """The same fact end to end, on whatever this machine really is — no fabricated
    `/proc` line and no platform branch in the test. macOS gets there by a different road
    than the obvious one (libproc refuses the read for a zombie rather than reporting
    SZOMB; see `_has_exited`), so the platform that cannot use the Linux parser still has
    to answer False here."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    try:
        recorded = proc_start_time(p.pid)
        assert recorded, "the child must be identifiable while it is alive"
        assert owner_state(Owner(p.pid, recorded, time.time(), "n", "e1")) is True
        time.sleep(1.5)                  # it exits; nobody wait()s it, so it stays a zombie
        os.kill(p.pid, 0)                # ...and is still signalable, which is the trap
        assert owner_state(Owner(p.pid, recorded, time.time(), "n", "e1")) is False
    finally:
        p.wait()
