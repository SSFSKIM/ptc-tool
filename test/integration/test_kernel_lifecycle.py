import json
import multiprocessing as mp
import os

from ptc.kernel import ensure_kernel, kernel_alive, kill_kernel, list_kernels
from ptc.ownership import read_owner


def test_spawn_ready_and_reuse(ptc_home):
    info = ensure_kernel("k1", cwd=str(ptc_home))
    assert info.spawned and info.pid > 0
    assert (ptc_home / "kernels" / "k1" / "ready").exists()
    conn = json.loads(info.connection_file.read_text())
    assert conn["shell_port"] > 0          # kernel wrote real ports back
    assert kernel_alive("k1")
    info2 = ensure_kernel("k1")
    assert not info2.spawned and info2.pid == info.pid
    rows = list_kernels()
    assert any(r["key"] == "k1" and r["alive"] for r in rows)
    assert kill_kernel("k1")
    assert not kernel_alive("k1")


def _race(home, q):
    os.environ["PTC_HOME"] = home
    from ptc.kernel import ensure_kernel
    q.put(ensure_kernel("race").pid)


def test_concurrent_first_exec_spawns_one_kernel(ptc_home):
    q = mp.Queue()
    ps = [mp.Process(target=_race, args=(str(ptc_home), q)) for _ in range(2)]
    [p.start() for p in ps]
    pids = {q.get(timeout=120) for _ in ps}
    [p.join() for p in ps]
    assert len(pids) == 1                   # exactly one kernel won
    kill_kernel("race")


def test_dead_owner_is_cleaned_and_respawned(ptc_home):
    info = ensure_kernel("k2")
    os.kill(info.pid, 9)
    import time; time.sleep(0.5)
    info2 = ensure_kernel("k2")
    assert info2.spawned and info2.pid != info.pid
    # old cells dir rotated
    assert any(p.name.startswith("cells-prev-") for p in (ptc_home / "kernels" / "k2").iterdir())
    kill_kernel("k2")
