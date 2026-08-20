"""Watchdog expiry (F5), the in-flight guard on it (I1), and cell ids across epochs (F3)."""
import os
import time

from ptc.cells import read_record
from ptc.client import Completed, KernelClient, Running
from ptc.kernel import ensure_kernel, kill_kernel, restart_kernel
from ptc.paths import Config

IDLE_HOURS = 0.001                                   # 3.6 s — Config.from_env reads this
TTL_S = IDLE_HOURS * 3600
TICK_S = min(30.0, max(TTL_S / 10, 0.5))             # bootstrap._watchdog's own cadence
PATIENCE_S = 30.0


def _alive(pid: int) -> bool:
    """The test process is the kernel's parent, so an exited kernel lingers as a
    zombie and `kill(pid, 0)` still succeeds. Reap it first, then ask."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        return False                                 # already reaped elsewhere: dead
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_watchdog_expires_idle_kernel(ptc_home, monkeypatch):
    """F5: an idle kernel expires itself, leaves the marker, and drops its ownership."""
    monkeypatch.setenv("PTC_IDLE_HOURS", str(IDLE_HOURS))
    info = ensure_kernel("wd1", cwd=str(ptc_home))
    kd = ptc_home / "kernels" / "wd1"

    deadline = time.monotonic() + PATIENCE_S
    while time.monotonic() < deadline and _alive(info.pid):
        time.sleep(TICK_S / 2)
    assert not _alive(info.pid), f"idle kernel survived {PATIENCE_S}s at a {TTL_S}s ttl"
    assert (kd / "expired.marker").exists()
    assert not (kd / "owner.json").exists() and not (kd / "ready").exists()

    monkeypatch.setenv("PTC_IDLE_HOURS", "24")       # the replacement must outlive the asserts
    info2 = ensure_kernel("wd1", cwd=str(ptc_home))
    assert info2.spawned and info2.pid != info.pid
    assert info2.expired_notice is not None          # consume_expiry surfaced the death
    assert not (kd / "expired.marker").exists()      # ...and consumed the marker
    kill_kernel("wd1")


def test_watchdog_spares_running_cell(ptc_home, monkeypatch):
    """I1: `last_activity` is stamped at cell START, so a cell that runs past the TTL
    looks exactly like an idle kernel unless the watchdog checks for one in flight."""
    monkeypatch.setenv("PTC_IDLE_HOURS", str(IDLE_HOURS))
    info = ensure_kernel("wd2", cwd=str(ptc_home))
    kd = ptc_home / "kernels" / "wd2"

    out = KernelClient("wd2").exec_cell(
        f"import time; time.sleep({TTL_S * 4}); print('survived')",
        timeout_s=1.0, config=Config.from_env())
    assert isinstance(out, Running), f"cell should still be running, got {out!r}"

    watch_until = time.monotonic() + TTL_S * 2       # well past the ttl, well inside the cell
    while time.monotonic() < watch_until:
        assert _alive(info.pid), "watchdog killed a kernel with a cell in flight"
        assert not (kd / "expired.marker").exists(), "watchdog expired a busy kernel"
        time.sleep(TICK_S / 2)

    deadline = time.monotonic() + PATIENCE_S
    rec = read_record("wd2", out.cell_id)
    while rec is None and time.monotonic() < deadline:
        time.sleep(0.2)
        rec = read_record("wd2", out.cell_id)
    assert rec is not None and rec.status == "ok", f"spared cell never completed: {rec}"
    kill_kernel("wd2")


def test_cell_ids_monotonic_across_restart(ptc_home):
    """F3: a new epoch numbers above the archived epoch, so a cell id yielded before a
    restart can never name a different cell after it."""
    ensure_kernel("wd3", cwd=str(ptc_home))
    cfg = Config.from_env()
    kc = KernelClient("wd3")
    first = kc.exec_cell("a = 1", timeout_s=60, config=cfg)
    second = kc.exec_cell("print('epoch1')", timeout_s=60, config=cfg)
    assert isinstance(first, Completed) and isinstance(second, Completed)
    pre_max = max(first.cell_id, second.cell_id)

    restart_kernel("wd3", cwd=str(ptc_home))
    after = KernelClient("wd3").exec_cell("print('epoch2')", timeout_s=60, config=cfg)
    # Completed with its own output proves the id names this cell's record and log,
    # not a stale one carried over from the previous epoch.
    assert isinstance(after, Completed) and "epoch2" in after.output
    assert after.cell_id > pre_max, f"{after.cell_id} did not clear pre-restart {pre_max}"

    kd = ptc_home / "kernels" / "wd3"
    archived = [p for p in kd.iterdir() if p.name.startswith("cells-prev-")]
    assert archived, "restart did not archive the previous epoch's cells"
    assert any("epoch1" in (d / f"{pre_max}.log").read_text() for d in archived
               if (d / f"{pre_max}.log").exists())
    kill_kernel("wd3")
