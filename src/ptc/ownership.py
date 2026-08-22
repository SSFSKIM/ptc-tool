"""owner.json: which OS process is this key's kernel. Identity = pid + birth identity."""
import ctypes
import ctypes.util
import functools
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import kernel_dir, private_write_text, secure_dir


class UnknownOwner(RuntimeError):
    """A recorded owner that could not be identified either way, twice running.

    Not "alive" and not "dead" — a `ps` (or `/proc`) read that failed. Every caller whose
    next move would DESTROY something (delete the metadata, signal the group, spawn a
    replacement) raises this instead of guessing, because both guesses are damaging: call
    a live kernel dead and its metadata is dropped while it keeps running, call a dead one
    live and the key is stuck. Callers that merely REPORT keep guessing "not alive".
    """


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


#: The prefix on every identity that came from a KERNEL birth stamp. Nothing else can
#: produce it, so a record either names that source or is a bare `ps -o lstart=` string —
#: written by an older PTC, or by a platform with no finer source — and that is the whole
#: of the migration rule `start_time_matches` applies. Nothing on disk is rewritten and
#: there is no version field.
_BIRTH_MARK = "btime="

_ON_LINUX = sys.platform.startswith("linux")
_ON_DARWIN = sys.platform == "darwin"

#: `proc_pidinfo(pid, PROC_PIDTBSDINFO, ...)` — libproc's `struct proc_bsdinfo`, whose last
#: two fields are the process's birth time to the microsecond. Only those two are read; the
#: rest is declared because the call fills the whole struct and its length is the argument
#: that validates the layout (it returns the byte count it wrote, and `pbi_pid` is checked
#: against the pid asked for, so a struct that ever stops matching the OS reads as
#: "unknown" rather than as somebody else's birth time).
_PROC_PIDTBSDINFO = 3


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@functools.lru_cache(maxsize=1)
def _libc():
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    except OSError:
        return None


def _darwin_birth(pid: int) -> str | None:
    libc = _libc()
    if libc is None:
        return None
    info = _ProcBsdInfo()
    try:
        n = libc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(_PROC_PIDTBSDINFO),
                              ctypes.c_uint64(0), ctypes.byref(info),
                              ctypes.c_int(ctypes.sizeof(info)))
    except (AttributeError, OSError, ValueError):
        return None
    if n != ctypes.sizeof(info) or info.pbi_pid != pid:
        return None                      # dead pid, refused read, or a layout that moved
    return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"


def _ps(pid: int, fmt: str, *, pinned: bool = True) -> str | None:
    env = {**os.environ, **_PINNED_ENV} if pinned else None
    try:
        out = subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, env=env)
        s = out.stdout.strip()
        return s or None
    except (OSError, subprocess.SubprocessError):
        return None


def _proc_stat(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text()
    except OSError:          # no /proc, or a hardened mount: the `ps` form still answers
        return None


def _start_ticks(stat: str) -> str | None:
    """Field 22 of a `/proc/<pid>/stat` line: start time in clock ticks since boot.

    Field 2 is the executable name in parentheses and may itself contain spaces AND
    parentheses, so the fields are counted from after the LAST ')' — where field 3 lands at
    index 0 and field 22 at index 19. Splitting the raw line on whitespace instead reads
    whatever the process happened to name itself.
    """
    _, sep, rest = stat.rpartition(")")
    if not sep:
        return None
    parts = rest.split()
    return parts[19] if len(parts) > 19 else None


def _birth(pid: int) -> str | None:
    """The OS kernel's own birth stamp for `pid`, subsecond, or None where there is none.

    Linux keeps it in `/proc/<pid>/stat` (clock ticks since boot); macOS answers it from
    libproc (microseconds). Both are values the kernel assigned at creation and never
    revises, which is the property that matters here: PTC records this the instant after
    it forks a kernel and compares it for the rest of that process's life.
    """
    if _ON_LINUX:
        stat = _proc_stat(pid)
        return _start_ticks(stat) if stat else None
    if _ON_DARWIN:
        return _darwin_birth(pid)
    return None


def proc_start_time(pid: int, *, pinned: bool = True) -> str | None:
    """The finest birth identity this machine can give for `pid`; None if unreadable.

    A subsecond kernel birth stamp where the platform has one (`_birth`), because
    `ps -o lstart=` resolves only to the SECOND: a pid or pgid handed out again inside one
    second reads as the process that held it before, and kill, restart and TTL cleanup all
    signal a whole process group on the strength of that identity.

    Where there is no such stamp the answer is the pinned `lstart` string this has always
    used — weaker, and honestly so. It is deliberately NOT composed with `ps -o comm=`:
    macOS reports the invoked path for a freshly forked process and the resolved executable
    a moment later, so an identity carrying it stops matching the process it was recorded
    for seconds after the spawn — a live kernel reading as a recycled pid, which is the
    exact failure this identity exists to prevent.

    Pinned (the default, and what every caller should use): the `lstart` form is rendered
    under TZ=UTC/LC_ALL=C, so any process on this machine reads the same string for the
    same process. `pinned=False` reproduces a pre-pinning read and exists only for
    `start_time_matches`'s migration case.
    """
    if not pinned:
        return _ps(pid, "lstart=", pinned=False)
    birth = _birth(pid)
    if birth is not None:
        return f"{_BIRTH_MARK}{birth}"
    return _ps(pid, "lstart=", pinned=True)


def start_time_matches(pid: int, recorded: str | None) -> bool | None:
    """Is `pid` still the process whose birth identity was recorded as `recorded`?

    True and False are answers; None means the identity could not be read at all (or was
    never recorded), which is not evidence either way — callers decide what to do with a
    process they cannot identify, and they do not all decide the same thing.

    Migration tolerance, in up to two extra reads: an `owner.json` (or registry row)
    written before this version holds a bare `ps -o lstart=` string, pinned (r6) or — older
    still — rendered through whatever timezone and locale its writer had. Neither can equal
    a birth stamp, so a record that names no source is re-compared against exactly the two
    readings that used to be written, which proves identity in every case that ever worked
    and rescues nothing beyond it. A record that DOES name its source is answered by its own
    reading alone: a birth stamp is what this machine writes now, so a mismatch is a
    mismatch. Nothing on disk is migrated — every kernel spawn writes a fresh `owner.json`
    and every `bash()` registration a fresh row, so records self-upgrade and never take this
    path twice.
    """
    if not recorded:
        return None
    current = proc_start_time(pid)
    if current is None:
        return None
    if current == recorded:
        return True
    if recorded.startswith(_BIRTH_MARK):
        return False
    for legacy in (_ps(pid, "lstart="), proc_start_time(pid, pinned=False)):
        if legacy is not None and legacy == recorded:
            return True
    return False


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


def owner_state(o: Owner) -> bool | None:
    """Is this key's recorded kernel still running? True / False / None (cannot tell).

    A pid that no longer exists — or that exists and belongs to another user, which our own
    kernel never can — is a definite no. A pid that is ours and whose birth identity cannot
    be read is the third answer, and it is the one that matters: converting it to False is
    what let a transient `ps` failure delete a live kernel's metadata and spawn a duplicate
    beside it.
    """
    try:
        os.kill(o.pid, 0)
    except OSError:
        return False
    return start_time_matches(o.pid, o.proc_start_time)


def owner_alive(o: Owner) -> bool:
    """`owner_state` collapsed to two values, unknown reading as not-alive.

    For callers that only REPORT (`ptc list`, `kernel_alive`): the worst an unknown costs
    them is one pessimistic row. Anything that kills, deletes or respawns must use
    `settled_owner_state` instead, which refuses to answer at all rather than guess.
    """
    return owner_state(o) is True


def settled_owner_state(o: Owner | None) -> bool:
    """`owner_state` with one retry, raising `UnknownOwner` rather than guessing.

    `ps` fails transiently — a fork bottleneck, a signal, a process-table hiccup — so the
    read is retried once, the same single retry the kernel spawn gives its own identity
    read. Beyond that the honest answer is that we do not know, and the caller's next move
    would destroy something on the strength of a guess.
    """
    if o is None:
        return False
    state = owner_state(o)
    if state is None:
        state = owner_state(o)
    if state is None:
        raise UnknownOwner(
            f"cannot tell whether the recorded kernel process (pid {o.pid}) is still "
            "running: reading its identity failed twice. Nothing was signalled, deleted "
            "or respawned. Retry; if that process is definitely gone, remove the key's "
            "owner.json by hand.")
    return state
