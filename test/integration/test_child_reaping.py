"""Kernel death takes the kernel's own children with it (T20 review I1).

Agent backends spawn `claude` CLIs with no session of their own, so those CLIs live in
the kernel's process group. Neither exit path can rely on the SDK's `atexit` reaper:
`kill_kernel` SIGKILLs, and the idle watchdog leaves via `os._exit`. Without a group
kill, every open child survives its kernel as a billing orphan.

A plain `sleep` started from a cell is the stand-in for a child CLI: same parent, same
process group, no credentials and no network involved.
"""
import os
import time

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

IDLE_HOURS = 0.001                                   # 3.6 s — Config.from_env reads this
PATIENCE_S = 30.0

CHILD_CELL = """
import subprocess
_child = subprocess.Popen(["sleep", "300"])     # inherits the kernel's process group
print("CHILD", _child.pid)
"""


def _child_pid(out) -> int:
    assert isinstance(out, Completed), out
    return int(out.output.split("CHILD")[1].strip())


def _running(pid: int) -> bool:
    """The child's parent is the KERNEL, not this process, so once the kernel dies the
    orphan is reparented and reaped — no zombie of ours to filter out."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kernel_running(pid: int) -> bool:
    """The kernel IS our child, so an exited one lingers as a zombie and kill(pid, 0)
    still succeeds. Reap it first, then ask."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        return False
    return _running(pid)


def _wait_gone(pid: int, patience: float = 10.0) -> bool:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        time.sleep(0.1)
    return False


def _reap(pid: int) -> None:
    try:
        os.kill(pid, 9)                              # never leave a stray sleep behind
    except OSError:
        pass


def test_kill_kernel_reaps_the_kernels_children(ptc_home):
    ensure_kernel("cr1", cwd=str(ptc_home))
    pid = _child_pid(KernelClient("cr1").exec_cell(
        CHILD_CELL, timeout_s=60, config=Config.from_env()))
    try:
        assert _running(pid), "the stand-in child never started"
        assert kill_kernel("cr1")
        assert _wait_gone(pid), f"child {pid} outlived the kernel it was spawned from"
    finally:
        _reap(pid)


BG_BASH_CELL = """
h = await bash("sleep 300", background=True)     # its OWN session, outside our group
print("CHILD", h.pid)
"""


def test_watchdog_expiry_reaps_background_bash_groups(ptc_home, monkeypatch):
    """The group kill cannot reach a background `bash()` child — it is its own session
    leader by design. The expiring kernel has to reap those groups from the registry
    first, or a TTL expiry leaves them running for the rest of the machine's uptime."""
    monkeypatch.setenv("PTC_IDLE_HOURS", str(IDLE_HOURS))
    info = ensure_kernel("cr3", cwd=str(ptc_home))
    pid = _child_pid(KernelClient("cr3").exec_cell(
        BG_BASH_CELL, timeout_s=60, config=Config.from_env()))
    try:
        assert _running(pid)
        deadline = time.monotonic() + PATIENCE_S
        while time.monotonic() < deadline and _kernel_running(info.pid):
            time.sleep(0.2)
        assert not _kernel_running(info.pid), "idle kernel never expired"
        assert _wait_gone(pid), f"background group {pid} outlived the expired kernel"
    finally:
        _reap(pid)


def test_watchdog_expiry_reaps_the_kernels_children(ptc_home, monkeypatch):
    """The watchdog's os._exit skips atexit by design, so the SDK's own reaper never
    runs on this path — the idle kernel has to take its children down itself."""
    monkeypatch.setenv("PTC_IDLE_HOURS", str(IDLE_HOURS))
    info = ensure_kernel("cr2", cwd=str(ptc_home))
    pid = _child_pid(KernelClient("cr2").exec_cell(
        CHILD_CELL, timeout_s=60, config=Config.from_env()))
    try:
        assert _running(pid)
        deadline = time.monotonic() + PATIENCE_S
        while time.monotonic() < deadline and _kernel_running(info.pid):
            time.sleep(0.2)
        assert not _kernel_running(info.pid), "idle kernel never expired"
        assert (ptc_home / "kernels" / "cr2" / "expired.marker").exists()
        assert _wait_gone(pid), f"child {pid} outlived the expired kernel"
    finally:
        _reap(pid)
