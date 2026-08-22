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

from .ownership import start_time_matches
from .paths import private_write_text, secure_dir

FILENAME = "bash-pgids.json"


def path_for(kernel_dir) -> Path:
    return Path(kernel_dir) / FILENAME


def write(kernel_dir, rows: list) -> None:
    """Atomically replace the registry (same tmp+rename the cell records use, so a crash
    mid-write leaves the previous list rather than a truncated one)."""
    p = path_for(kernel_dir)
    try:
        secure_dir(p.parent)
        private_write_text(p, json.dumps(rows))
    except OSError:
        pass


def read(kernel_dir) -> list:
    try:
        rows = json.loads(path_for(kernel_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _pid_exists(pid: int) -> bool:
    """Is there a process wearing this pid at all? Existence only — never identity.

    EPERM proves existence just as well as success does (the process is there, it is simply
    somebody else's), and only ESRCH proves absence.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _recycled(row: dict) -> bool:
    """True when this row's group may NOT be signalled on identity grounds — the recorded
    pgid is led by a different process than the one registered, or by one nobody can
    identify at all.

    `start_time_matches` has three answers and the third is the one that bites. None means
    the identity could not be READ: a `ps` that timed out under load, a `/proc` read that
    failed, an EPERM on a live process that now leads this recycled pgid. Folding that
    unknown into "not recycled" is fail-open — it licenses a SIGKILL of a group nothing
    ever confirmed was ours — so an unreadable leader fails closed.

    `leader_exited` rows are the exception, and the exception has a gate. Such a row's
    unreadable leader is the RECORDED, expected state: a daemonizing background `bash`
    exits its shell and leaves its children in the same group, so the shell keeps the row
    precisely to reap them (`runtime/shell.py` `_retire`) and the leader is unreadable
    forever after. But an exemption granted on the strength of "unreadable" alone is
    fail-open in one state — the group emptied, the pgid was handed out again, and the read
    on the NEW leader transiently failed — and there the reap SIGKILLed a stranger.

    EXISTENCE decides it, because on a `leader_exited` row it decides it by itself. The
    recorded leader is dead, so a live process wearing that pid can only be a recycle: fail
    closed. NO process wearing it is the retained-orphan case the exemption is for — POSIX
    will not hand the number out while it is still a live group's id, and a group that has
    since emptied makes `killpg` a harmless ESRCH the reap already tolerates.

    One residual is accepted rather than closed: the recycled id's new leader could ALSO
    have died leaving its own orphan group, between the recycle and this reap, which reads
    exactly like our own retained orphan. That is a double coincidence inside a window
    `runtime/shell.py` `_watch_orphans` already keeps short by dropping retained rows as
    soon as their group empties.

    A row with no identity at all is a different question entirely and is not answered here
    — see `unverifiable()`.
    """
    match = start_time_matches(row["pgid"], row.get("leader_start"))
    if match is None:
        if not row.get("leader_exited"):
            return True
        return _pid_exists(row["pgid"])
    return match is False


def _construction_known(row: dict) -> bool:
    """Is this row's pgid known by CONSTRUCTION rather than by a read?

    `bash()` spawns `start_new_session=True`, so setsid makes the child's pgid equal to its
    pid before anything reads either. Registration leaves a row that knows its group id
    exactly and its leader's birth stamp not at all whenever that identity read comes back
    empty — because the leader exited inside the call, or because `ps` and `/proc` both
    failed there (`runtime/shell.py` `_register`, both branches). That is the opposite
    shape from the row `unverifiable` exists to quarantine, which knows neither and says so
    by claiming neither. The marker is what tells the two apart, so a LEGACY bare row
    cannot be mistaken for this one.

    `leader_exited` is required alongside it because the marker alone proves only the
    NUMBER. While the leader is still alive and unidentified nothing has been established
    about who leads that group now, and the row waits in quarantine like any other; the
    exemption begins when the exit is recorded and the existence gate can stand in for the
    stamp (`_recycled`).
    """
    return row.get("pgid_source") == "setsid" and bool(row.get("leader_exited"))


def unverifiable(row: dict) -> bool:
    """True when nothing ever proved WHICH process led this row's group.

    `_recycled` can only compare against a recorded identity, and a pgid outlives the group
    it named — the OS hands that number to unrelated same-user work and the next
    kill/restart/TTL reap would SIGKILL a stranger's process group. A row that could not be
    identified when it was written is therefore quarantined HERE, ahead of any comparison,
    so its eligibility never depends on what an identity read happens to answer for a
    number nobody recorded: `reap` may DROP it (the file is consumed either way, and a row
    whose group is already empty costs nothing to forget), but never signals it. A missing
    `leader_start` is the whole test, so rows written before identities were recorded at
    all land here too — the safe direction for a registry whose rows are recreated on every
    command.

    This is not the `leader_exited` case: those rows WERE identified at registration and
    keep their signal-eligibility by design, which is the whole point of retaining them.
    Nor is it the row registration could not identify at all — because the leader was gone
    before it asked, or because both reads failed. Such a row has no birth stamp to offer
    either, but its pgid is a construction guarantee rather than a reading
    (`_construction_known`), and `_recycled`'s existence gate is what stands in for the
    stamp it cannot have.
    """
    if _construction_known(row):
        return False
    return bool(row.get("unverifiable")) or not row.get("leader_start")


def signalable(row: dict) -> bool:
    """May this row's group be SIGKILLed at all?

    The two questions above, asked together, because they are one rule and every path that
    signals a recorded pgid owes it: `reap` on the host side and `BashHandle.kill()` inside
    the kernel, which holds the same recorded identity and must not reach a group the
    registry rules would have spared.

    Signalling takes proof, in both directions: a row nobody could identify when it was
    written is out, and so is one whose leader cannot be identified NOW — unless the row
    records that the leader is expected to be gone AND no process is wearing that pid to
    contradict it. Everything else is spared, at the cost of leaving the odd already-empty
    group unsignalled, which costs nothing.
    """
    pgid = row.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    return not unverifiable(row) and not _recycled(row)


def reap_leaderless_group(pgid: int) -> bool:
    """SIGKILL a process group whose recorded leader is CONFIRMED gone. True if signalled.

    The kernel is a session leader (`start_new_session=True`), so its pgid IS its pid and
    every ordinary child it makes — the Claude SDK's `claude` CLI, a `codex app-server`, a
    plain `subprocess` — lives in that group. A kernel killed by the OOM killer or a crash
    therefore leaves a leaderless group of its own descendants still running and still
    billing, and neither the group kill (there is no live pid to take the group from) nor
    `reap` above (background `bash()` groups are in sessions of their own, which is the
    whole reason they are registered separately) reaches them.

    The gate is `_recycled`'s, for a `leader_exited` row, because it is exactly the same
    question: the recorded leader is known dead, so a process WEARING that pid can only be
    a recycle and the group behind it a stranger's — fail closed, and note that an
    unreadable identity already answered that mismatch upstream, since only a settled
    `False` reaches here. NO process wearing it is the orphaned-descendants case this
    exists for: POSIX will not hand the number out while it still names a live group, and a
    group that has already emptied makes this a harmless ESRCH.

    The self-check is `reap`'s, for `reap`'s reason: a caller sitting in a group whose own
    leader has exited would otherwise SIGKILL itself out of a cleanup path.
    """
    if not isinstance(pgid, int) or pgid <= 1 or _pid_exists(pgid):
        return False
    try:
        if pgid == os.getpgid(0):
            return False
    except OSError:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return False                    # already empty (ESRCH) or not ours (EPERM)
    return True


def reap(kernel_dir) -> list:
    """SIGKILL every recorded group, then drop the file. Returns the pgids signalled."""
    killed = []
    for row in read(kernel_dir):
        pgid = row.get("pgid")
        if not signalable(row):
            # never identified, or somebody else's group now: drop the row, unsignalled
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
