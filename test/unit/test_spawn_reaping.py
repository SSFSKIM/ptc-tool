"""When the spawner reaps its kernel — and why never before the spawn has succeeded.

The failure path of `ensure_kernel` SIGKILLs `proc.pid` by number. An unreaped child keeps
that number (and the process group it leads) reserved, so the kill can only reach this
kernel. A reaper started at Popen time reaped a child that died during startup, freeing
its pid for reuse before the abort's group kill ran. Fakes only: the Popen, the readiness
steps and the kill are stubs that record the order things happen in.
"""
import threading
from pathlib import Path

import pytest

import ptc.client
from ptc import kernel


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    """`ensure_kernel` with every external step stubbed; returns (events, run)."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr(kernel, "spawn_venv", lambda: tmp_path / "venv")
    monkeypatch.setattr(kernel, "build_identity", lambda: None)
    events: list = []
    waited = threading.Event()
    kd = tmp_path / "kernels" / "k"

    class _Proc:
        pid = 424242

        def wait(self, timeout=None):
            events.append(("wait", timeout, (kd / "ready").exists()))
            waited.set()
            return 0

    monkeypatch.setattr(kernel.subprocess, "Popen", lambda cmd, **kw: _Proc())
    monkeypatch.setattr(kernel, "proc_start_time", lambda pid: "birth")
    monkeypatch.setattr(kernel, "_wait_ports", lambda conn: Path(conn).write_text("{}"))
    monkeypatch.setattr(kernel, "_kernel_info_roundtrip", lambda conn: None)
    monkeypatch.setattr(kernel, "kill_process_tree", lambda pid: events.append(("kill", pid)))

    def run(bootstrap):
        monkeypatch.setattr(ptc.client, "run_bootstrap", bootstrap)
        return kernel.ensure_kernel("k", cwd=str(tmp_path))

    return events, waited, run


def test_a_failed_spawn_kills_before_it_reaps(spawn):
    events, _, run = spawn

    def fail(key, cfg):
        raise RuntimeError("bootstrap failed")

    with pytest.raises(RuntimeError):
        run(fail)
    assert events == [("kill", 424242), ("wait", kernel._REAP_WAIT_S, False)], \
        "the child was reaped before (or without) the kill that relies on its pid"


def test_a_successful_spawn_is_reaped_only_after_ready(spawn):
    events, waited, run = spawn
    info = run(lambda key, cfg: None)
    assert info.spawned
    assert waited.wait(5), "nothing ever waits on the kernel: it would linger as a zombie"
    assert events == [("wait", None, True)]
