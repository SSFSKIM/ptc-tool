"""The tool process groups of the kernel's `claude` children, learned while they live and
reaped when they die.

Claude Code runs every Bash tool detached: the tool's shell is a direct child of the
`claude` process and leads a process group (and a session) of its own. A `claude` that ends
gracefully kills the tool groups it still tracks; one that is SIGKILLed kills none, and its
tools are reparented to init in groups nothing of ptc's holds. That is the agent host
SIGKILLed by `_SdkHost.close`'s escalation or from outside (an OOM kill, `kill -9`), and a
CLI-route child the same way, or with the kernel's whole group (`ptc kill`, restart).

Once a `claude` is dead its tools cannot be found by parentage, so they are learned while
it lives:

  * A watcher thread in the kernel lists, every `TICK_S`, the kernel's `claude` children
    and each one's children that lead their own process group, and records every such
    group in `<kernel_dir>/agent-pgids.json` in `bgroups`' row format: the pgid with its
    leader's birth stamp, `leader_exited` once the leader is gone while the group lives on.
    A thread, not a task: a cell that blocks the event loop must not blind it.
  * When a recorded `claude` is gone, its rows are reaped under `bgroups.signalable`'s rule.
    The file is what `bgroups.reap` consumes on every kernel exit, kill and restart, so a
    kernel that dies with its children loses none of what it had learned.
  * Where ptc's own code does the killing, the `claude` is stopped (SIGSTOP) first and its
    tool groups read then, so a tool started since the last tick is not missed:
    `stop_and_kill` (the host's escalation), `stop_all_and_reap` (the kernel's TTL exit),
    `freeze_tree` (`kill_process_tree`, for `ptc kill` and restart).

Only groups led by a `claude`'s DIRECT children are taken, and never one led by a `claude`:
a per-user daemon a tool happens to start (the `claude` daemon, tmux, gpg-agent) sits in a
group of its own below the tool's shell and is never reaped here.

Residual, accepted: a kill ptc does not make reaches only what a tick saw, so a tool whose
shell had lived less than `TICK_S` when its `claude` was killed from outside keeps running,
as does a process that left its tool's group (`setsid`, a double fork).

Nothing here raises: every caller is on a path whose job is to end something cleanly.
"""
import ctypes
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import bgroups
from .ownership import _darwin_bsdinfo, _has_exited, _libc, proc_start_time, start_time_matches
from .paths import private_write_text, secure_dir

FILENAME = "agent-pgids.json"
#: How often the watcher reads the `claude` children's tools. A tick costs a few syscalls
#: per `claude` (libproc on macOS, `/proc` on Linux); it is also the residual's window.
TICK_S = 0.25
#: The watcher stops once it has had nothing to watch this long, and `watch()` restarts it.
IDLE_EXIT_S = 15.0
#: The process names taken for a Claude Code engine: the SDK's bundled CLI is `claude`.
AGENT_COMMS = frozenset({"claude"})

_ON_DARWIN = sys.platform == "darwin"
_ON_LINUX = sys.platform.startswith("linux")


# ---- reading the process table -----------------------------------------------------------

def children(pid: int) -> list[int]:
    """The direct children of `pid`, zombies included; [] when they cannot be read."""
    if _ON_DARWIN:
        return _darwin_children(pid)
    if _ON_LINUX:
        return _linux_children(pid)
    return []


def _darwin_children(pid: int) -> list[int]:
    libc = _libc()
    if libc is None:
        return []
    size = 256
    while True:
        buf = (ctypes.c_int * size)()
        try:
            # Returns the number of pids it wrote (darwin 25: 2 for two children).
            n = libc.proc_listchildpids(ctypes.c_int(pid), buf, ctypes.c_int(ctypes.sizeof(buf)))
        except (AttributeError, OSError, ValueError):
            return []
        if n < 0:
            return []
        if n < size:
            return [p for p in buf[:n] if p > 0]
        size *= 4


def _linux_children(pid: int) -> list[int]:
    """`/proc/<pid>/task/*/children` (CONFIG_PROC_CHILDREN), else a scan of every stat."""
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []
    out: list[int] = []
    for tid in tids:
        try:
            out += [int(x) for x in Path(f"/proc/{pid}/task/{tid}/children").read_text().split()]
        except FileNotFoundError:
            return _linux_scan_children(pid)
        except (OSError, ValueError):
            continue
    return out


def _linux_scan_children(pid: int) -> list[int]:
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text()
        except OSError:
            continue
        rest = stat.rpartition(")")[2].split()
        if len(rest) > 1 and rest[1] == str(pid):
            out.append(int(name))
    return out


def comm(pid: int) -> str | None:
    """The process's name (`claude` for the engine), None when it cannot be read — a zombie
    on macOS, whose libproc read fails."""
    if _ON_DARWIN:
        info, _ = _darwin_bsdinfo(pid)
        return info.pbi_comm.decode(errors="replace") if info is not None else None
    if _ON_LINUX:
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return None
    return None


def is_agent(pid: int) -> bool:
    """A live Claude Code engine. A zombie is not one: its children are already init's."""
    return comm(pid) in AGENT_COMMS and not _has_exited(pid)


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _group_row(agent: int, child: int) -> dict | None:
    """The row for `child`'s process group when `child` leads it and is not a `claude`,
    else None. A child in someone else's group is covered by that group's own kill, and a
    `claude` is never taken (module docstring)."""
    if _pgid(child) != child or comm(child) in AGENT_COMMS:
        return None
    start = proc_start_time(child) or proc_start_time(child)
    # The pgid was READ equal to the pid: a detached child leads its group by setsid, which
    # is what `bgroups` lets stand in for a birth stamp once the leader has exited.
    return {"pgid": child, "pid": child, "leader_start": start, "started_at": time.time(),
            "pgid_source": "setsid", "agent": agent}


def tool_groups(agent: int) -> list[dict]:
    """A row for every process group led by one of `agent`'s direct children: its tools."""
    return [r for c in children(agent) if (r := _group_row(agent, c)) is not None]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _gone(pid: int, start) -> bool:
    """Has this `claude` ended? Asked of the pid itself, never read from its absence in a
    listing that may have failed: no process wears it, or only its zombie does, or one
    born at another time does. An identity that cannot be read is not a death."""
    if not bgroups._pid_exists(pid) or _has_exited(pid):
        return True
    return bool(start) and start_time_matches(pid, start) is False


def _note_leader(row: dict) -> None:
    """Mark the row `leader_exited` once no process wears the leader's pid (or only its
    zombie does): from there `bgroups`' existence gate stands in for the birth stamp."""
    if not row.get("leader_exited") and (
            not bgroups._pid_exists(row["pgid"]) or _has_exited(row["pgid"])):
        row["leader_exited"] = True


def reap_rows(rows) -> list[int]:
    """SIGKILL each row's group that `bgroups.signalable` allows. Returns the pgids
    signalled. Never the caller's own group."""
    try:
        own = os.getpgid(0)
    except OSError:
        own = None
    killed = []
    for row in rows:
        pgid = row.get("pgid")
        if not isinstance(pgid, int) or pgid <= 1 or pgid == own:
            continue
        _note_leader(row)
        if not bgroups.signalable(row):
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pgid)
        except OSError:
            pass
    return killed


def reap_file(kernel_dir) -> list[int]:
    """Reap a kernel's recorded rows and drop the file (`bgroups.reap`, on every kernel
    exit, kill and restart). Returns the pgids signalled."""
    p = Path(kernel_dir) / FILENAME
    try:
        rows = json.loads(p.read_text())
    except (OSError, ValueError):
        rows = []
    killed = reap_rows([r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [])
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    return killed


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGSTOP)
    except OSError:
        pass


def freeze_tree(kernel: int) -> list[dict]:
    """Stop `kernel`'s process group and read its `claude` children's tool groups, for a
    caller about to SIGKILL that group (`kill_process_tree`). Once the group is stopped no
    `claude` in it can start another tool, so the rows are exact. A caller inside that
    group is not stopped (it would stop itself before the kill); its read is a snapshot."""
    try:
        if os.getpgid(0) != kernel:
            os.killpg(kernel, signal.SIGSTOP)
    except OSError:
        pass
    rows = []
    try:
        for child in children(kernel):
            if is_agent(child):
                rows += tool_groups(child)
    except Exception:                    # noqa: BLE001 — the kill must follow regardless
        pass
    return rows


# ---- the kernel's watcher ----------------------------------------------------------------

class Tracker:
    """The rows of one process's `claude` children, kept current by `tick()`."""

    def __init__(self, root: int, kernel_dir=None):
        self.root = root
        self.kernel_dir = Path(kernel_dir) if kernel_dir is not None else None
        self.lock = threading.RLock()
        #: agent pid -> {"start": its birth stamp, "groups": {pgid: row}}
        self.agents: dict[int, dict] = {}

    def empty(self) -> bool:
        with self.lock:
            return not self.agents

    def rows(self) -> list[dict]:
        with self.lock:
            return [r for a in self.agents.values() for r in a["groups"].values()]

    def tick(self) -> None:
        try:
            live = {c: proc_start_time(c) for c in children(self.root) if is_agent(c)}
            with self.lock:
                changed = False
                for pid, entry in list(self.agents.items()):
                    if pid not in live and _gone(pid, entry["start"]):
                        self._reap_agent(pid)
                        changed = True
                for pid, start in live.items():
                    changed |= self._refresh(pid, start)
                if changed:
                    self._persist()
        except Exception:                # noqa: BLE001 — a bad tick must not end the watcher
            pass

    def _refresh(self, agent: int, start) -> bool:
        """Drop rows whose group emptied, mark exited leaders, add new tool groups."""
        entry = self.agents.setdefault(agent, {"start": start, "groups": {}})
        groups = entry["groups"]
        changed = False
        for pgid, row in list(groups.items()):
            if not _group_alive(pgid):
                del groups[pgid]
                changed = True
                continue
            was = row.get("leader_exited")
            _note_leader(row)
            changed |= row.get("leader_exited") != was
        for child in children(agent):
            if child not in groups and (row := _group_row(agent, child)) is not None:
                groups[child] = row
                changed = True
        return changed

    def _reap_agent(self, agent: int) -> None:
        entry = self.agents.pop(agent, None)
        if entry is not None:
            reap_rows(entry["groups"].values())

    def stop_and_kill(self, agent: int) -> None:
        """SIGKILL a `claude` this process spawned and the tools it was running: stop it,
        read its tool groups (exact, since it can start no other), kill it, reap them.
        `agent` must be an unreaped child, so that its pid is still its own."""
        _stop(agent)
        with self.lock:
            self._refresh(agent, proc_start_time(agent))
            try:
                os.kill(agent, signal.SIGKILL)
            except OSError:
                pass
            self._reap_agent(agent)
            self._persist()

    def stop_all_and_reap(self) -> None:
        """The kernel is exiting: stop every `claude` child, read their tool groups, reap
        them, and SIGKILL the stopped children (the group kill that follows would too; this
        does not leave them stopped where it does not run)."""
        agents = [c for c in children(self.root) if is_agent(c)]
        for a in agents:
            _stop(a)
        with self.lock:
            for a in agents:
                self._refresh(a, proc_start_time(a))
            for a in list(self.agents):
                self._reap_agent(a)
            self._persist()
        for a in agents:
            try:
                os.kill(a, signal.SIGKILL)
            except OSError:
                pass

    def _persist(self) -> None:
        if self.kernel_dir is None:
            return
        p = self.kernel_dir / FILENAME
        try:
            rows = self.rows()
            if rows:
                secure_dir(p.parent)
                private_write_text(p, json.dumps(rows))
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass


_LOCK = threading.Lock()
_TRACKER: Tracker | None = None
_THREAD: threading.Thread | None = None
_WANTED_UNTIL = 0.0


def tracker() -> Tracker:
    """The kernel's one tracker: its own `claude` children, persisted under its kernel dir
    (not at all outside a bootstrapped kernel, whose dir is still the placeholder)."""
    global _TRACKER
    from .runtime.state import STATE
    kd = None if str(STATE.kernel_dir) in (".", "") else Path(STATE.kernel_dir)
    with _LOCK:
        if _TRACKER is None or _TRACKER.kernel_dir != kd:
            _TRACKER = Tracker(os.getpid(), kd)
        return _TRACKER


def watch() -> None:
    """Make sure the watcher runs: called as a `claude` is about to start. Idempotent."""
    global _THREAD, _WANTED_UNTIL
    t = tracker()
    if t.kernel_dir is None:
        return                           # no kernel to watch for (a test, a script)
    with _LOCK:
        _WANTED_UNTIL = time.monotonic() + IDLE_EXIT_S
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_run, daemon=True, name="ptc-toolgroups")
        _THREAD.start()


def _run() -> None:
    global _THREAD, _WANTED_UNTIL
    while True:
        t = tracker()
        t.tick()
        with _LOCK:
            if not t.empty():
                _WANTED_UNTIL = time.monotonic() + IDLE_EXIT_S
            elif time.monotonic() > _WANTED_UNTIL:
                _THREAD = None
                return
        time.sleep(TICK_S)


def _reset() -> None:
    """Test-only: forget the tracker; a running watcher takes up the next one."""
    global _TRACKER
    with _LOCK:
        _TRACKER = None
