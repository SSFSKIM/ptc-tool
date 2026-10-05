"""How much memory a kernel holds, and whether the machine is short of it.

Read from two sides: the kernel's own watchdog asks about itself (memory-aware idle expiry,
the footprint stamped into each cell record), and the adapter asks about a kernel by pid
(`kernels` listing). Both answers are best-effort — None is "this platform cannot say",
and every caller treats it as "no reason to act", never as zero or as pressure.
"""
import ctypes
import os
import sys

from .ownership import _libc

_ON_DARWIN = sys.platform == "darwin"
_ON_LINUX = sys.platform.startswith("linux")

#: `proc_pid_rusage(pid, RUSAGE_INFO_V2, &ri)` — `struct rusage_info_v2` from
#: <sys/resource.h>. Only `ri_phys_footprint` is read, but the call fills the whole struct,
#: so every field is declared: a buffer shorter than the version asked for is a write past
#: its end, not a short read.
_RUSAGE_INFO_V2 = 2


class _RusageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
        "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
        "ri_proc_start_abstime", "ri_proc_exit_abstime", "ri_child_user_time",
        "ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups",
        "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread",
        "ri_diskio_byteswritten")]


def footprint(pid: int | None = None) -> int | None:
    """Bytes of memory `pid` (default: this process) is charged for; None if unreadable.

    On macOS that is `phys_footprint`, the number Activity Monitor's "Memory" column and
    jetsam both use — NOT the resident size. An idle process's pages are compressed and
    swapped out from under it, and RSS stops counting them: the kernel that prompted this
    read showed ~20 MB resident while holding 5.2 GB of namespace. Measuring RSS would let
    exactly the kernels worth expiring look light. Linux falls back to RSS from /proc,
    which undercounts swap the same way but is the cheap honest answer there.
    """
    pid = os.getpid() if pid is None else pid
    if _ON_DARWIN:
        libc = _libc()
        if libc is None:
            return None
        ri = _RusageInfoV2()
        try:
            rc = libc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(_RUSAGE_INFO_V2),
                                      ctypes.byref(ri))
        except (AttributeError, OSError, ValueError):
            return None
        return int(ri.ri_phys_footprint) if rc == 0 else None
    if _ON_LINUX:
        try:
            with open(f"/proc/{pid}/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    return None


#: `kern.memorystatus_vm_pressure_level`: 1 normal, 2 warn, 4 critical. Warn is already the
#: state the user feels — the compressor full and swap growing — so it is the line.
_PRESSURE_WARN = 2


def under_pressure() -> bool:
    """Is the machine short of memory right now? False wherever it cannot be read.

    macOS only. Linux has PSI (/proc/pressure/memory), but it reports stall percentages
    with no system-defined line between "busy" and "short", and inventing one would turn a
    guess into kernels killed early — the plain heavy-and-idle rule still applies there.
    """
    if not _ON_DARWIN:
        return False
    libc = _libc()
    if libc is None:
        return False
    level = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(level))
    try:
        rc = libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(level),
                               ctypes.byref(size), None, ctypes.c_size_t(0))
    except (AttributeError, OSError, ValueError):
        return False
    return rc == 0 and level.value >= _PRESSURE_WARN


def human(n: int) -> str:
    """`5.2G`, `740M` — the width a result header can afford."""
    gib = n / 2**30
    return f"{gib:.1f}G" if gib >= 1 else f"{n / 2**20:.0f}M"
