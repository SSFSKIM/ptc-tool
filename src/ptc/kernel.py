"""Spawn/kill/list detached ipykernels. All state transitions under the per-key lock."""
import json
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import bgroups
from .discovery import read_meta, write_meta
from .lock import key_lock
from .ownership import (
    Owner,
    owner_alive,
    proc_start_time,
    read_owner,
    settled_owner_state,
    write_owner,
)
from .paths import (
    Config,
    cells_dir,
    kernel_dir,
    kernels_root,
    private_open,
    secure_dir,
)
from .venv import build_identity, venv_python


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


def read_expiry(key: str) -> str | None:
    """The TTL watchdog's note that this key's namespace died, WITHOUT taking it.

    Reading and consuming used to be one step, taken before stale cleanup, the spawn,
    readiness and bootstrap — so any failure past that point destroyed the notice, and the
    retry that finally worked reported an ordinary result with nothing to say that a whole
    session's namespace had been lost. Deletion belongs at the far end of the successful
    spawn instead (`ensure_kernel`), which is also the only place the notice is delivered
    from, so it is still delivered exactly once.
    """
    try:
        return (kernel_dir(key) / "expired.marker").read_text()
    except OSError:
        return None


def _upgraded_under(key: str) -> str | None:
    """Has the venv this kernel is running from been rebuilt since it started?

    The launcher clears and recreates the shared `~/.ptc/venv` whenever its stamp changes,
    and a kernel already running keeps executing modules out of the directory that was
    removed: every import of something not yet loaded fails, and everything already loaded
    is a version nobody ships any more. Attaching to it hands the caller a namespace built
    on deleted code, indefinitely — a kernel outlives the adapter that spawned it by up to
    the idle TTL, so this is not a narrow window.

    Both identities have to be KNOWN before this answers yes. One is absent for a kernel
    spawned outside a provisioned venv (a dev run, a test fixture, a hand-made venv), and
    absent again for the moments a provision spends between `uv venv --clear` and writing
    the new stamp. Neither is evidence of an upgrade, and acting on the second would try to
    spawn a replacement out of a half-built venv; both attach exactly as before, and the
    next ensure — once two known identities differ — recycles then.

    The text is shaped for the same channel the TTL notice travels on (`KernelInfo
    .expired_notice`, rendered as "previous kernel expired: ..."), because it is the same
    news to the caller: the namespace is gone and here is why.
    """
    was = read_meta(key).get("build")
    now = build_identity()
    if not was or not now or was == now:
        return None
    return (f"replaced after a ptc runtime upgrade — the venv it was running from "
            f"(build {was[:12]}) was rebuilt as {now[:12]} and no longer exists")


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


def _clean_stale(kd: Path, key: str, o: "Owner | None", alive: bool) -> None:
    """Owner and liveness are passed in, not re-read: the caller has already SETTLED them
    (`settled_owner_state`), and re-asking here would let a second, unluckier read answer
    "not alive" to a question that was already answered — the deletions below are exactly
    what must never happen on a guess."""
    if o and alive:
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
        # Settled BEFORE the expiry marker is consumed and before anything is deleted: an
        # owner whose identity cannot be read is neither attached to nor cleaned up, and
        # `UnknownOwner` leaves this key exactly as it was found.
        alive = settled_owner_state(o)
        attachable = bool(o and alive and (kd / "ready").exists())
        # A live kernel whose venv was rebuilt under it is not attachable: it runs deleted
        # code, and the caller has to be told its namespace is gone rather than handed one
        # that half-works. Recycling is the ordinary spawn path below — `_clean_stale`
        # reaps it exactly as it reaps any other kernel standing in the way.
        upgraded = _upgraded_under(key) if attachable else None
        if attachable and upgraded is None:
            # Attaching to a live kernel is the only chance to repair a meta.json that
            # was written before anyone knew the Claude session id (a CLI exec keyed off
            # an env rung, an MCP attach after the CLI spawned the kernel): history() and
            # agent.fork() read the id back from there and are unavailable without it.
            if claude_session_id and not read_meta(key).get("claude_session_id"):
                write_meta(key, claude_session_id=claude_session_id)
            return KernelInfo(key, o.pid, kd / "connection.json", False, None)
        # A marker and an upgrade cannot both be standing — a key with an expiry marker has
        # no live kernel to have been upgraded under — so this is a choice on paper only,
        # and the marker wins because it is the one that must also be CONSUMED.
        expired = read_expiry(key) or upgraded
        _clean_stale(kd, key, o, alive)
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
        # Read beside the interpreter it names and BEFORE the fork, so meta.json records
        # the build this kernel really launched from rather than whatever the venv became
        # while it was starting (`_upgraded_under` compares against it for the rest of
        # this kernel's life).
        build = build_identity()
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
            # Identity is pid + start time, and a pid alone is not identity: without the
            # start time `owner_alive` can never call this owner live, so attach and
            # kill_kernel cannot recognize the process while `_clean_stale` deletes its
            # metadata without killing it and spawns another kernel beside it — an
            # unreapable orphan still holding this key's ports. `ps` failing is normally
            # transient, so it is retried once; a spawn that still cannot record who it
            # started fails here and takes that process down with it (the handler below).
            start = proc_start_time(proc.pid) or proc_start_time(proc.pid)
            if start is None:
                raise RuntimeError(
                    f"could not read the start time of the kernel just spawned for {key} "
                    f"(pid {proc.pid}): without that identity nothing could attach to it "
                    "or reap it, so the spawn is aborted")
            write_owner(key, Owner(proc.pid, start,
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
                       cwd=work, depth=cfg.depth, epoch=epoch, build=build)
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
        # The notice is spent HERE, past the last thing that can fail the spawn — every
        # path above leaves the marker standing for the retry to find. Deliberately
        # outside the handler: a marker that cannot be unlinked must not take a kernel
        # that is already ready down with it, and the worst a leftover costs is one
        # repeated notice after the NEXT death.
        if expired is not None:
            try:
                (kd / "expired.marker").unlink(missing_ok=True)
            except OSError:
                pass
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
    """SIGKILL this key's kernel and clear up after it. True only if one was SIGNALLED.

    The two halves answer to different facts. Cleanup runs on the strength of the RECORD:
    a record whose process is gone (it died, or its pid was recycled) is stale, and
    deleting it — with the orphaned bash groups it can no longer reap — is the whole point
    of running `kill` against such a key. The return value answers to the SIGNAL, because
    that is what the CLI publishes as its exit code: 0 for a kernel killed, 1 for nothing
    to kill. Returning True for a record deletion made `ptc kill` claim a kill it had not
    made, and a shell loop could not tell a live cleanup from a sweep of dead metadata.

    An identity that cannot be read is neither: `settled_owner_state` raises `UnknownOwner`
    out of here, and nothing is signalled, deleted or reported (the CLI prints it and exits
    1). A live kernel's metadata is never dropped on a guess.
    """
    with key_lock(key):
        o = read_owner(key)
        # pid + birth identity: never killpg a recycled pid's group — and never drop a
        # live kernel's metadata on an identity that could not be read (`UnknownOwner`
        # leaves the key untouched rather than reporting an orphaning as a success).
        killed = bool(o and settled_owner_state(o))
        if killed:
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
        return killed


def restart_kernel(key: str, **kw) -> KernelInfo:
    """Replace this key's kernel, keeping the config the kernel itself was created with.

    A restart is not a fresh spawn: the key already has an identity, and the caller doing
    the restarting need not be the one that made it. Depth is the case that bites. A parent
    adapter (or a terminal) restarts a CHILD kernel by explicit key while running at
    PTC_DEPTH=0, and rebuilding Config from that caller's environment overwrites the
    child's stored depth with zero — the still-running child adapter then attaches to a
    depth-0 kernel and can spawn grandchildren of its own, with PTC_MAX_DEPTH=1 in force
    and the recursion brake silently released. The depth belongs to the KERNEL, and
    meta.json has recorded it since the spawn, so that is where a restart reads it.

    cwd and the Claude session id are the same story and are already restored by both
    restart callers, which is where they have to be: only the caller knows whether it
    means to move the kernel.
    """
    depth = read_meta(key).get("depth")
    kill_kernel(key)
    time.sleep(0.2)
    if isinstance(depth, int):
        cfg = kw.pop("config", None) or Config.from_env()
        if cfg.depth != depth:
            cfg = replace(cfg, depth=depth)
        kw["config"] = cfg
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
