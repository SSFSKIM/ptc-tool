"""The background-bash-group registry: crash-safe writes, tolerant reaps.

The registry is read by a process that never spawned the groups it names (a CLI, the MCP
adapter) and possibly long after they exited, so every entry is advisory: gone is normal,
malformed is possible, and the reaper's own group must never be in the blast radius.
"""
import json
import os
import subprocess

from ptc import bgroups


def _dead_pgid() -> int:
    """A pgid that certainly no longer exists: its own session leader, already reaped."""
    p = subprocess.Popen(["true"], start_new_session=True)
    p.wait()
    return p.pid


def test_write_is_atomic_and_read_round_trips(tmp_path):
    bgroups.write(tmp_path, [{"pgid": 4242, "pid": 4242, "cmd": "sleep 1"}])
    assert bgroups.read(tmp_path) == [{"pgid": 4242, "pid": 4242, "cmd": "sleep 1"}]
    assert json.loads(bgroups.path_for(tmp_path).read_text())[0]["pgid"] == 4242
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed, not left"


def test_reap_tolerates_stale_and_malformed_entries(tmp_path):
    bgroups.write(tmp_path, [{"pgid": _dead_pgid()}, {"pgid": "nonsense"},
                             {"pgid": 0}, {"not": "a pgid"}])

    assert bgroups.reap(tmp_path) == []                 # nothing live to signal
    assert not bgroups.path_for(tmp_path).exists()      # and the file is consumed


def test_reap_never_signals_the_reapers_own_group(tmp_path):
    """A recycled pgid that lands on the caller would make this reap suicidal — it runs
    inside the CLI, the adapter, and the kernel's own watchdog."""
    bgroups.write(tmp_path, [{"pgid": os.getpgid(0)}])

    assert bgroups.reap(tmp_path) == []
    assert os.getpid() == os.getpid()                   # still here to say so


def test_reap_of_a_missing_file_is_a_no_op(tmp_path):
    assert bgroups.reap(tmp_path) == []
    assert bgroups.read(tmp_path) == []
