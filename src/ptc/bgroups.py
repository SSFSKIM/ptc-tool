"""Background `bash()` process groups: one per-kernel registry both sides can reap.

A background bash child is spawned `start_new_session=True` on purpose — that is what lets
a timeout kill the command's whole tree without touching the kernel. The cost is that the
child is also OUTSIDE the kernel's own process group, so the group kill that reaps every
other kernel child (`kill_process_tree` on kill/restart, `_reap_and_exit` on TTL) never
reaches it: `bash("sleep 3600", background=True)` outlived all three exits as an orphan.

The kernel writes its live groups here as they start and stop. The file is the only channel
to the HOST process — a CLI or MCP adapter that never saw those pids — and to the kernel's
own watchdog exit, which leaves through `os._exit` and so runs no atexit handler.

Entries are advisory: a group may have exited between the last write and the reap, so ESRCH
is normal and skipped. Nothing here raises — every caller is on a path whose job is to end
a kernel cleanly.
"""
import json
import os
import signal
from pathlib import Path

FILENAME = "bash-pgids.json"


def path_for(kernel_dir) -> Path:
    return Path(kernel_dir) / FILENAME


def write(kernel_dir, rows: list) -> None:
    """Atomically replace the registry (same tmp+rename the cell records use, so a crash
    mid-write leaves the previous list rather than a truncated one)."""
    p = path_for(kernel_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(p)
    except OSError:
        pass


def read(kernel_dir) -> list:
    try:
        rows = json.loads(path_for(kernel_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def reap(kernel_dir) -> list:
    """SIGKILL every recorded group, then drop the file. Returns the pgids signalled."""
    killed = []
    for row in read(kernel_dir):
        pgid = row.get("pgid")
        if not isinstance(pgid, int) or pgid <= 1:
            continue
        try:
            # never the reaper's own group: a stale entry whose pgid has been recycled
            # onto the caller (a CLI, a test runner, the kernel itself mid-cleanup) would
            # otherwise make this reap suicidal
            if pgid == os.getpgid(0):
                continue
        except OSError:
            pass
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pgid)
        except OSError:
            pass                        # already gone (ESRCH) or not ours (EPERM)
    try:
        path_for(kernel_dir).unlink(missing_ok=True)
    except OSError:
        pass
    return killed
