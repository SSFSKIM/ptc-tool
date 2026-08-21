import json
import multiprocessing as mp
import os
import stat
import subprocess
import sys
import time

import pytest

from ptc import kernel
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


def test_spawn_failure_leaks_no_process(ptc_home, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(kernel, "write_meta", _boom)

    conn_path = ptc_home / "kernels" / "failkey" / "connection.json"
    with pytest.raises(RuntimeError, match="boom"):
        ensure_kernel("failkey")

    assert read_owner("failkey") is None
    assert not (ptc_home / "kernels" / "failkey" / "ready").exists()

    deadline = time.monotonic() + 5.0
    result = subprocess.run(["pgrep", "-f", str(conn_path)],
                            capture_output=True, text=True)
    while result.returncode == 0 and time.monotonic() < deadline:
        time.sleep(0.2)
        result = subprocess.run(["pgrep", "-f", str(conn_path)],
                                capture_output=True, text=True)
    assert result.returncode != 0, f"kernel process(es) leaked: {result.stdout!r}"


def test_spawn_without_a_recordable_identity_fails_and_leaks_no_process(ptc_home, monkeypatch):
    """A kernel nobody can identify must not become a ready kernel.

    `ps` failing here (transiently, or on a host without it) left owner.json with a null
    start time, and identity is pid + start time: `owner_alive` can never call that owner
    live, so attach and kill_kernel could not recognize the process while `_clean_stale`
    deleted its metadata WITHOUT killing it and spawned another kernel beside it. The
    spawn fails instead, and reaps the process it just started.
    """
    calls = []

    def _no_identity(pid):
        calls.append(pid)
        return None

    monkeypatch.setattr(kernel, "proc_start_time", _no_identity)
    kd = ptc_home / "kernels" / "noident"
    conn = kd / "connection.json"

    with pytest.raises(RuntimeError, match="start time"):
        ensure_kernel("noident")

    assert len(calls) == 2, f"a transient ps failure is retried exactly once: {calls}"
    for name in ("owner.json", "ready", "connection.json"):
        assert not (kd / name).exists(), f"{name} survived a failed spawn"
    assert read_owner("noident") is None

    deadline = time.monotonic() + 10.0
    while _pids_for(conn) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _pids_for(conn) == [], "the unidentifiable kernel was left running"


def _pids_for(conn) -> list[int]:
    """Every live process launched against this connection file."""
    r = subprocess.run(["pgrep", "-f", str(conn)], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()] if r.returncode == 0 else []


def test_parent_death_between_spawn_and_readiness_leaves_a_reapable_kernel(ptc_home):
    """Ownership is published straight after the fork, not after the readiness checks.

    A spawner killed inside that window (an adapter shutting down, a Ctrl-C) leaves a
    detached kernel behind. With owner.json written only at the end, nothing on disk named
    that process: the key lock died with the parent, so the next ensure_kernel could
    neither identify nor reap it and simply spawned a second orphan beside it.
    """
    script = (
        "import os\n"
        "import ptc.kernel as k\n"
        "orig = k._wait_ports\n"
        "def die(conn, timeout=20.0):\n"
        "    orig(conn, timeout)\n"          # the kernel is genuinely up: a real orphan
        "    os._exit(0)\n"                  # and now the parent dies mid-transaction
        "k._wait_ports = die\n"
        "k.ensure_kernel('prov')\n")
    subprocess.run([sys.executable, "-c", script], timeout=180,
                   env={**os.environ, "PTC_HOME": str(ptc_home)})

    kd = ptc_home / "kernels" / "prov"
    conn = kd / "connection.json"
    orphan = json.loads((kd / "owner.json").read_text())["pid"]
    assert not (kd / "ready").exists(), "a kernel that never bootstrapped is not ready"
    assert not kernel_alive("prov"), "provisional ownership must not read as a live kernel"
    assert _pids_for(conn) == [orphan]

    try:
        info = ensure_kernel("prov")
        assert info.spawned and info.pid != orphan
        deadline = time.monotonic() + 10
        while orphan in _pids_for(conn) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert _pids_for(conn) == [info.pid], "the provisional kernel was not reaped"
    finally:
        for pid in _pids_for(conn):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def test_kernel_state_is_owner_only(ptc_home):
    """State dirs 0700 and their files 0600: cell logs, records, audit and registries
    hold model-written code, command output and task prompts, and the common 022 umask
    made every one of them readable by every other local account."""
    from ptc.client import KernelClient
    from ptc.paths import Config

    ensure_kernel("perm1", cwd=str(ptc_home))
    out = KernelClient("perm1").exec_cell("print('hello')", timeout_s=60,
                                          config=Config.from_env())
    assert out.record.status == "ok"
    kd = ptc_home / "kernels" / "perm1"

    def mode(p):
        return stat.S_IMODE(p.stat().st_mode)

    for d in (ptc_home, kd.parent, kd, kd / "cells", kd / "cells" / "offsets"):
        # 0o700 with the group/other bits masked off, not an exact match: jupyter_client
        # adds the sticky bit to whatever directory holds a connection file, and that
        # hardening must survive our own (see connect.py's write_connection_file).
        assert mode(d) & 0o077 == 0 and mode(d) & 0o700 == 0o700, (d, oct(mode(d)))
    for name in ("owner.json", "meta.json", "connection.json", "kernel.log"):
        assert mode(kd / name) == 0o600, name
    for f in (kd / "cells").glob("*.*"):
        assert mode(f) == 0o600, f

    # a directory created before this rule is repaired on the next ensure
    kd.chmod(0o755)
    (kd / "cells").chmod(0o755)
    ensure_kernel("perm1", cwd=str(ptc_home))
    assert mode(kd) & 0o077 == 0 and mode(kd / "cells") & 0o077 == 0
    kill_kernel("perm1")


def test_attaching_to_a_live_kernel_repairs_a_missing_session_id(ptc_home):
    """meta.json is where history() and agent.fork() read the Claude session id. A kernel
    first created by a caller that did not know it (a CLI exec off an env rung) used to
    keep the gap forever: attachment returned early without ever repairing it."""
    ensure_kernel("meta1", cwd=str(ptc_home))
    meta = ptc_home / "kernels" / "meta1" / "meta.json"
    assert json.loads(meta.read_text()).get("claude_session_id") is None

    info = ensure_kernel("meta1", claude_session_id="sess-late")
    assert not info.spawned, "the repair must happen on ATTACH, not by respawning"
    assert json.loads(meta.read_text())["claude_session_id"] == "sess-late"

    ensure_kernel("meta1", claude_session_id="sess-other")
    assert json.loads(meta.read_text())["claude_session_id"] == "sess-late", "id overwritten"
    kill_kernel("meta1")
