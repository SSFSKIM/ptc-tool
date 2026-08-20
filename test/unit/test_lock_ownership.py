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
