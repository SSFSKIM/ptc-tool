"""Spawn/kill/list detached ipykernels. All state transitions under the per-key lock."""
import json
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .discovery import write_meta
from .lock import key_lock
from .ownership import Owner, owner_alive, proc_start_time, read_owner, write_owner
from .paths import Config, cells_dir, kernel_dir, kernels_root
from .venv import venv_python


@dataclass
class KernelInfo:
    key: str
    pid: int
    connection_file: Path
    spawned: bool
    expired_notice: str | None


def kernel_alive(key: str) -> bool:
    o = read_owner(key)
    return bool(o and owner_alive(o) and (kernel_dir(key) / "ready").exists())


def consume_expiry(key: str) -> str | None:
    m = kernel_dir(key) / "expired.marker"
    try:
        text = m.read_text()
        m.unlink()
        return text
    except OSError:
        return None


def _rotate_cells(kd: Path) -> None:
    cd = kd / "cells"
    if cd.exists():
        cd.rename(kd / f"cells-prev-{int(time.time())}")


def _clean_stale(kd: Path, key: str) -> None:
    o = read_owner(key)
    if o and owner_alive(o):
        # a live kernel with no `ready` (e.g. bootstrap failed) must be reaped
        # before respawn, or every retry leaks a detached process (F4) — and its
        # children with it, for the same reason kill_kernel takes the group
        kill_process_tree(o.pid)
    for name in ("owner.json", "ready", "connection.json"):
        (kd / name).unlink(missing_ok=True)
    _rotate_cells(kd)


def _wait_ports(conn: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(conn.read_text())
            if data.get("shell_port", 0) > 0:
                return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise TimeoutError(f"kernel never wrote ports to {conn}")


def _kernel_info_roundtrip(conn: Path, timeout: float = 20.0) -> None:
    from jupyter_client import BlockingKernelClient
    kc = BlockingKernelClient()
    kc.load_connection_file(str(conn))
    kc.start_channels()
    try:
        kc.kernel_info()
        kc.get_shell_msg(timeout=timeout)
    finally:
        kc.stop_channels()


def ensure_kernel(key: str, *, cwd: str | None = None,
                  claude_session_id: str | None = None,
                  config: Config | None = None) -> KernelInfo:
    cfg = config or Config.from_env()
    kd = kernel_dir(key)
    kd.mkdir(parents=True, exist_ok=True)
    with key_lock(key):
        o = read_owner(key)
        if o and owner_alive(o) and (kd / "ready").exists():
            return KernelInfo(key, o.pid, kd / "connection.json", False, None)
        expired = consume_expiry(key)
        _clean_stale(kd, key)
        cells_dir(key).mkdir(parents=True, exist_ok=True)
        (cells_dir(key) / "offsets").mkdir(exist_ok=True)
        conn = kd / "connection.json"
        work = cwd or cfg.cwd or os.getcwd()
        env = {**os.environ,
               "PTC_SESSION": key, "PTC_CWD": work,
               "PTC_DEPTH": str(cfg.depth), "PTC_MAX_DEPTH": str(cfg.max_depth),
               "PTC_IDLE_HOURS": str(cfg.idle_hours),
               "PTC_MAX_CONCURRENCY": str(cfg.max_concurrency),
               "PTC_HOME": str(kernels_root().parent)}
        log = open(kd / "kernel.log", "ab")
        proc = subprocess.Popen(
            [str(venv_python()), "-m", "ipykernel_launcher", "-f", str(conn)],
            cwd=work, env=env, stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            _wait_ports(conn)
            os.chmod(conn, 0o600)
            _kernel_info_roundtrip(conn)
            epoch = str(int(time.time()))
            write_owner(key, Owner(proc.pid, proc_start_time(proc.pid),
                                   time.time(), secrets.token_hex(8), epoch))
            write_meta(key, kernel_key=key, claude_session_id=claude_session_id,
                       cwd=work, depth=cfg.depth, epoch=epoch)
            from .client import run_bootstrap
            run_bootstrap(key, cfg)
            (kd / "ready").write_text(epoch)   # ready means BOOTSTRAPPED
        except BaseException:
            kill_process_tree(proc.pid)
            for name in ("owner.json", "ready", "connection.json"):
                try:
                    (kd / name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return KernelInfo(key, proc.pid, conn, True, expired)


def kill_process_tree(pid: int) -> None:
    """SIGKILL the kernel AND everything it spawned into its process group.

    A pid-only kill strands the kernel's children: agent backends spawn `claude` CLIs
    with no session of their own, so they live in the kernel's group, and the SDK's
    atexit reaper cannot run under SIGKILL. The kernel is spawned with
    `start_new_session=True`, so its group contains exactly its own descendants —
    background `bash()` children get their own session (shell.py kills those itself).
    Guarded on leadership: if the pid does not lead its group, the group belongs to
    somebody else and killing it would reach unrelated processes, so kill only the pid.
    """
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
            return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def kill_kernel(key: str) -> bool:
    with key_lock(key):
        o = read_owner(key)
        if not o:
            return False
        if owner_alive(o):     # pid + start time: never killpg a recycled pid's group
            kill_process_tree(o.pid)
        (kernel_dir(key) / "owner.json").unlink(missing_ok=True)
        (kernel_dir(key) / "ready").unlink(missing_ok=True)
        return True


def restart_kernel(key: str, **kw) -> KernelInfo:
    kill_kernel(key)
    time.sleep(0.2)
    return ensure_kernel(key, **kw)


def list_kernels() -> list[dict]:
    rows = []
    root = kernels_root()
    if not root.exists():
        return rows
    from .discovery import read_meta
    for kd in sorted(root.iterdir()):
        if not kd.is_dir():
            continue
        o = read_owner(kd.name)
        meta = read_meta(kd.name)
        cells = kd / "cells"
        try:
            last_used = max((f.stat().st_mtime for f in cells.glob("*.log")),
                            default=(o.spawned_at if o else None))
        except OSError:
            last_used = o.spawned_at if o else None
        rows.append({
            "key": kd.name,
            "pid": o.pid if o else None,
            "alive": bool(o and owner_alive(o) and (kd / "ready").exists()),
            "cwd": meta.get("cwd"),
            "depth": meta.get("depth", 0),
            "spawned_at": o.spawned_at if o else None,
            "last_used": last_used,
        })
    return rows
