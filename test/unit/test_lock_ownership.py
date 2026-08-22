import json
import multiprocessing as mp
import os
import time

from ptc.lock import flock_path, key_lock, submit_lock
from ptc.ownership import Owner, owner_alive, proc_start_time, read_owner, write_owner


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
        import pytest
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
    from ptc.ownership import start_time_matches

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    me = os.getpid()
    monkeypatch.setenv("TZ", "Asia/Seoul")
    legacy = proc_start_time(me, pinned=False)
    assert legacy and legacy != proc_start_time(me), "this machine renders TZ into lstart"

    assert start_time_matches(me, legacy) is True
    assert owner_alive(Owner(me, legacy, time.time(), "n", "e1"))
    # and a string from neither read is still a mismatch, not a free pass
    assert start_time_matches(me, "Thu Jan  1 00:00:00 1970") is False
