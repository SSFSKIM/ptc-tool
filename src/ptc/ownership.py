"""owner.json: which OS process is this key's kernel. Identity = pid + start time."""
import json
import os
import subprocess
from dataclasses import asdict, dataclass

from .paths import kernel_dir, private_write_text, secure_dir


#: `ps -o lstart=` renders a wall-clock date THROUGH the caller's timezone and LC_TIME —
#: the same live process is "Fri Aug 21 09:14:02 2026" to one reader and a different string
#: to the next. That string is half of PTC's process identity, and it is WRITTEN by one
#: process and COMPARED by others that need not share either setting: a kernel spawned by an
#: adapter under TZ=UTC, then inspected by a CLI in a shell with TZ set to a local zone, fails
#: its own identity check — `owner_alive` calls the live owner dead, so `kill` drops its
#: metadata without ever signalling it and the next `ensure_kernel` spawns a duplicate kernel
#: beside the one still running. Every read is pinned to one rendering instead, so the
#: comparison is between two descriptions of the same instant rather than two locales.
_PINNED_ENV = {"TZ": "UTC", "LC_ALL": "C", "LANG": "C"}


def proc_start_time(pid: int, *, pinned: bool = True) -> str | None:
    """`ps -o lstart=` works on macOS and Linux procps; None if unreadable.

    Pinned (the default, and what every caller should use): rendered under TZ=UTC/LC_ALL=C,
    so any process on this machine reads the same string for the same process. `pinned=False`
    reproduces a pre-pinning read and exists only for `start_time_matches`'s migration case.
    """
    env = {**os.environ, **_PINNED_ENV} if pinned else None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, env=env)
        s = out.stdout.strip()
        return s or None
    except (OSError, subprocess.SubprocessError):
        return None


def start_time_matches(pid: int, recorded: str | None) -> bool | None:
    """Is `pid` still the process whose birth time was recorded as `recorded`?

    True and False are answers; None means the birth time could not be read at all (or was
    never recorded), which is not evidence either way — callers decide what to do with a
    process they cannot identify, and they do not all decide the same thing.

    Migration tolerance, in one extra read: an `owner.json` (or registry row) written before
    reads were pinned holds whatever string its writer's timezone and locale produced, so the
    pinned read can differ for a process that never went anywhere. A single unpinned re-read
    settles that case exactly as the pre-pinning code did — it proves identity whenever writer
    and reader share those settings, which is the only case that ever worked. It cannot rescue
    an old record written under settings this reader does not have; that record simply reads as
    a mismatch, as it did before. Nothing has to be migrated: every kernel spawn writes a fresh
    `owner.json` and every `bash()` registration a fresh row, so records become pinned on both
    sides on their own and never reach the second read again.
    """
    if not recorded:
        return None
    current = proc_start_time(pid)
    if current is None:
        return None
    if current == recorded:
        return True
    legacy = proc_start_time(pid, pinned=False)
    return legacy is not None and legacy == recorded


@dataclass
class Owner:
    pid: int
    proc_start_time: str | None
    spawned_at: float
    nonce: str
    epoch: str


def write_owner(key: str, o: Owner) -> None:
    d = secure_dir(kernel_dir(key))
    private_write_text(d / "owner.json", json.dumps(asdict(o)), tmp=d / "owner.json.tmp")


def read_owner(key: str) -> Owner | None:
    p = kernel_dir(key) / "owner.json"
    try:
        return Owner(**json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def owner_alive(o: Owner) -> bool:
    try:
        os.kill(o.pid, 0)
    except OSError:
        return False
    return start_time_matches(o.pid, o.proc_start_time) is True
