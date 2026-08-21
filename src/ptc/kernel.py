"""Spawn/kill/list detached ipykernels. All state transitions under the per-key lock."""
import json
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import bgroups
from .discovery import read_meta, write_meta
from .lock import key_lock
from .ownership import Owner, owner_alive, proc_start_time, read_owner, write_owner
from .paths import (
    Config,
    cells_dir,
    kernel_dir,
    kernels_root,
    private_open,
    secure_dir,
)
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
    if not cd.exists():
        return
    # Two rotations in the same second — a cleanup, then the retry after a failed spawn —
    # otherwise target the same name, and renaming onto a NONEMPTY archive raises
    # FileExistsError. That is how a recoverable spawn failure became a permanent one:
    # the archive from the first attempt blocked every later attempt to clean up.
    stamp = int(time.time())
    target = kd / f"cells-prev-{stamp}"
    n = 0
    while target.exists():
        n += 1
        target = kd / f"cells-prev-{stamp}-{n}"
    cd.rename(target)


def _clean_stale(kd: Path, key: str) -> None:
    o = read_owner(key)
    if o and owner_alive(o):
        # A live kernel with no `ready` must be reaped before respawn, or every retry
        # leaks a detached process (F4) — and its children with it, for the same reason
        # kill_kernel takes the group. Ownership is keyed off owner.json alone, never
        # `ready`, so this reaps a PROVISIONAL kernel too: one whose spawner died between
        # the fork and the end of bootstrap.
        kill_process_tree(o.pid)
    # the dead kernel's background bash groups are in sessions of their own, so the group
    # kill above never reached them (F4)
    bgroups.reap(kd)
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
    # secure_dir, not mkdir: kernel state is owner-only, and a directory from before this
    # rule (or from a laxer umask) has its mode repaired here on every ensure — including
    # the attach path below, which is the only ensure a long-lived kernel ever sees again.
    kd = secure_dir(kernel_dir(key))
    secure_dir(cells_dir(key))
    secure_dir(cells_dir(key) / "offsets")
    with key_lock(key):
        o = read_owner(key)
        if o and owner_alive(o) and (kd / "ready").exists():
            # Attaching to a live kernel is the only chance to repair a meta.json that
            # was written before anyone knew the Claude session id (a CLI exec keyed off
            # an env rung, an MCP attach after the CLI spawned the kernel): history() and
            # agent.fork() read the id back from there and are unavailable without it.
            if claude_session_id and not read_meta(key).get("claude_session_id"):
                write_meta(key, claude_session_id=claude_session_id)
            return KernelInfo(key, o.pid, kd / "connection.json", False, None)
        expired = consume_expiry(key)
        _clean_stale(kd, key)
        secure_dir(cells_dir(key))          # the rotation above took the old one away
        secure_dir(cells_dir(key) / "offsets")
        conn = kd / "connection.json"
        work = cwd or cfg.cwd or os.getcwd()
        env = {**os.environ,
               "PTC_SESSION": key, "PTC_CWD": work,
               "PTC_DEPTH": str(cfg.depth), "PTC_MAX_DEPTH": str(cfg.max_depth),
               "PTC_IDLE_HOURS": str(cfg.idle_hours),
               "PTC_MAX_CONCURRENCY": str(cfg.max_concurrency),
               "PTC_HOME": str(kernels_root().parent)}
        log = private_open(kd / "kernel.log", "ab")
        epoch = str(int(time.time()))
        proc = subprocess.Popen(
            [str(venv_python()), "-m", "ipykernel_launcher", "-f", str(conn)],
            cwd=work, env=env, stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            # Ownership is published IMMEDIATELY, before the readiness checks rather than
            # after them. A spawner killed inside that window (the adapter shut down, a
            # Ctrl-C) leaves a detached kernel behind, and with no owner.json nothing can
            # identify or reap it: the key lock dies with the parent and the next
            # ensure_kernel spawns a second orphan on top of the first. `ready` remains
            # the completed-BOOTSTRAP marker, so a provisional owner is never mistaken
            # for a usable kernel — kernel_alive() and the reuse check above both require
            # `ready`, while _clean_stale and kill_kernel key off the owner alone and so
            # reap it.
            write_owner(key, Owner(proc.pid, proc_start_time(proc.pid),
                                   time.time(), secrets.token_hex(8), epoch))
            _wait_ports(conn)
            os.chmod(conn, 0o600)
            _kernel_info_roundtrip(conn)
            # A caller that does not know the Claude session id (an env-keyed rung, a CLI
            # restart) passes None — which must not ERASE the id a previous spawn under
            # this key already learned: history() and fork() read it back from here.
            write_meta(key, kernel_key=key,
                       claude_session_id=claude_session_id or read_meta(key).get(
                           "claude_session_id"),
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
        if o and owner_alive(o):   # pid + start time: never killpg a recycled pid's group
            kill_process_tree(o.pid)
        # Only now, with the kernel down and unable to start another one, reap the
        # background bash groups it spawned: each is its own session, so the group kill
        # above cannot reach them and they would outlive the kernel as orphans (F4).
        # Also correct when there is no owner at all — those groups are orphans already.
        bgroups.reap(kernel_dir(key))
        if not o:
            return False
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
